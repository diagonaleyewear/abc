"""Commercial analytics for the price comparator: tables, KPIs, history series and charts.

Everything is computed from data already stored; nothing here queries Odoo or any competitor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from cloud_design import BRAND_GREEN
from price_comparator import PriceComparatorStore, comparable_offers

WINDOWS = {"7 días": 7, "30 días": 30, "90 días": 90, "12 meses": 365}
POSITION_BEST = "Vytria con el mejor precio"
POSITION_HIGHER = "Vytria más caro"
POSITION_NONE = "Sin comparación"
POSITIONS = (POSITION_BEST, POSITION_HIGHER, POSITION_NONE)
STATE_ALL = "Todos"
STATE_OPTIONS = (STATE_ALL, POSITION_BEST, POSITION_HIGHER, POSITION_NONE)
STATE_CURRENT = "Vigente"
STATE_LAST_KNOWN = "Último conocido"
STATE_EXPIRED = "Caducado"
LAST_KNOWN_FACTOR = 2  # beyond expiry*2 a price is considered expired, not just not-recent


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def price_state(checked_at: str | None, expiry_days: int, now: datetime | None = None) -> str:
    """Current: checked within the expiry. Last known: older, still the latest stored. Expired: much older."""
    if not checked_at:
        return STATE_EXPIRED
    age = (now or _utc()) - datetime.fromisoformat(checked_at)
    if age <= timedelta(days=expiry_days):
        return STATE_CURRENT
    return STATE_LAST_KNOWN if age <= timedelta(days=expiry_days * LAST_KNOWN_FACTOR) else STATE_EXPIRED


def _effective_eur(frame: pd.DataFrame) -> pd.Series:
    price = frame["promotional_price"].where(frame["promotional_price"].notna(), frame["price"])
    return price * frame["fx_to_eur"]


def position_of(difference_eur: float | None) -> str:
    if difference_eur is None or pd.isna(difference_eur):
        return POSITION_NONE
    return POSITION_BEST if difference_eur <= 0 else POSITION_HIGHER


def commercial_table(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame(columns=["brand", "model", "color", "ean", "own_price_eur", "best_competitor",
                                     "competitor_min_eur", "difference_eur", "difference_pct",
                                     "last_checked_at", "position", "own_price_status"])
    table = summary.assign(model=summary["commercial_name"])
    table["position"] = table["difference_eur"].map(position_of)
    return table.reindex(columns=["brand", "model", "color", "ean", "own_price_eur", "best_competitor",
                                  "competitor_min_eur", "difference_eur", "difference_pct",
                                  "difference_vs_competitor_pct", "last_checked_at", "position",
                                  "own_price_status"])


def filter_table(table: pd.DataFrame, offers: pd.DataFrame, *, brands=(), competitors=(),
                 availability=(), positions=()) -> pd.DataFrame:
    result = table
    if brands:
        result = result[result["brand"].isin(brands)]
    if positions:
        result = result[result["position"].isin(positions)]
    if competitors or availability:
        scoped = offers
        if competitors:
            scoped = scoped[scoped["competitor_name"].isin(competitors)]
        if availability:
            scoped = scoped[scoped["availability"].isin(availability)]
        result = result[result["ean"].isin(scoped["ean"].unique())]
    return result


def kpis(summary: pd.DataFrame, recent_change_count: int) -> dict[str, float | int | None]:
    own = int(summary["own_price_eur"].notna().sum()) if not summary.empty else 0
    compared = summary[summary["difference_eur"].notna()] if not summary.empty else summary
    comparable = len(compared)
    best = int((compared["difference_eur"] <= 0).sum()) if not summary.empty else 0
    expensive = int((compared["difference_eur"] > 0).sum()) if not summary.empty else 0
    mean_gap = (float(compared["difference_vs_competitor_pct"].mean())
                if len(compared) and "difference_vs_competitor_pct" in compared else None)
    return {
        "vytria_confirmed": own,
        "comparable": comparable,
        # None (not 0) when nothing is comparable: "0 %" would claim Vytria never has the best price.
        "best_price_pct": (best / len(compared) * 100) if len(compared) else None,
        "compared": len(compared),
        "vytria_more_expensive": expensive,
        "vytria_more_expensive_pct": expensive / len(compared) * 100 if len(compared) else None,
        "vytria_cheaper": int((compared["difference_eur"] < 0).sum()) if len(compared) else 0,
        "no_information": max(0, len(summary) - comparable),
        "mean_gap_vs_best_pct": mean_gap,
        "recent_changes": recent_change_count,
    }


POSITIONING_COLUMNS = ["competitor", "references", "vytria_mean_eur", "competitor_mean_eur", "difference_pct",
                       "cheaper_pct", "last_update", "signal"]
SIGNAL_BELOW = "Claramente por debajo de Vytria"
SIGNAL_ABOVE = "Claramente por encima de Vytria"
SIGNAL_INLINE = "En línea"


def comparable_pairs(summary: pd.DataFrame, offers: pd.DataFrame) -> pd.DataFrame:
    """One row per (EAN, competitor) that can be compared with the validated Vytria price.

    Only exact, available, same-market, tax-included offers with an EUR rate take part; each competitor
    contributes its latest valid offer per EAN. ``difference_pct`` = (competitor - Vytria) / Vytria * 100,
    so a negative value means the competitor is cheaper.
    """
    columns = ["ean", "competitor_id", "competitor", "url", "own_eur", "competitor_eur",
               "difference_pct", "checked_at"]
    if summary.empty or offers.empty:
        return pd.DataFrame(columns=columns)
    own = summary[summary["own_price_eur"].notna() & (summary["own_price_eur"] > 0)][["ean", "market", "own_price_eur"]]
    valid = comparable_offers(offers)
    joined = valid.merge(own, on=["ean", "market"], how="inner")
    if joined.empty:
        return pd.DataFrame(columns=columns)
    price = joined["promotional_price"].where(joined["promotional_price"].notna(), joined["price"])
    joined = joined.assign(competitor_eur=price * joined["fx_to_eur"])
    latest = joined.sort_values(["captured_at", "id"]).groupby(["ean", "competitor_id"]).tail(1)
    result = pd.DataFrame({
        "ean": latest["ean"], "competitor_id": latest["competitor_id"], "competitor": latest["competitor_name"],
        "url": latest.get("url", pd.Series("", index=latest.index)), "own_eur": latest["own_price_eur"],
        "competitor_eur": latest["competitor_eur"], "checked_at": latest["checked_at"],
    })
    result["difference_pct"] = (result["competitor_eur"] - result["own_eur"]) / result["own_eur"] * 100
    return result[columns].reset_index(drop=True)


def competitor_positioning(summary: pd.DataFrame, offers: pd.DataFrame, threshold_pct: float = 5.0) -> pd.DataFrame:
    """Average price gap of each competitor against Vytria over the same references, sorted ascending."""
    pairs = comparable_pairs(summary, offers)
    if pairs.empty:
        return pd.DataFrame(columns=POSITIONING_COLUMNS)
    rows = []
    for name, group in pairs.groupby("competitor"):
        gap = float(group["difference_pct"].mean())
        rows.append({
            "competitor": name, "references": int(group["ean"].nunique()),
            "vytria_mean_eur": float(group["own_eur"].mean()),
            "competitor_mean_eur": float(group["competitor_eur"].mean()),
            "difference_pct": gap,
            "cheaper_pct": float((group["difference_pct"] < 0).mean() * 100),
            "last_update": group["checked_at"].max(),
            "signal": SIGNAL_BELOW if gap <= -threshold_pct else SIGNAL_ABOVE if gap >= threshold_pct
            else SIGNAL_INLINE,
        })
    return pd.DataFrame(rows, columns=POSITIONING_COLUMNS).sort_values("difference_pct", ignore_index=True)


def mean_gap_pct(pairs: pd.DataFrame) -> float | None:
    return float(pairs["difference_pct"].mean()) if not pairs.empty else None


def dashboard_scope(summary: pd.DataFrame, offers: pd.DataFrame, *, brands=(), competitors=(),
                    query: str = "", states=()) -> pd.DataFrame:
    """Recalculate the best valid competitor price after applying the dashboard's shared filters."""
    scoped = summary.copy()
    if brands:
        scoped = scoped[scoped["brand"].isin(brands)]
    if query.strip():
        term = query.strip().casefold()
        fields = [name for name in ("ean", "brand", "model", "commercial_name", "color", "internal_reference")
                  if name in scoped.columns]
        text = scoped[fields].fillna("").astype(str).agg(" ".join, axis=1).str.casefold()
        scoped = scoped[text.str.contains(term, regex=False)]
    scoped_offers = offers
    if competitors:
        scoped_offers = scoped_offers[scoped_offers["competitor_name"].isin(competitors)]
    pairs = comparable_pairs(scoped, scoped_offers)
    if scoped.empty:
        return scoped

    recalculated = scoped.copy()
    for column in ("competitor_min_eur", "competitor_mean_eur", "competitor_max_eur", "difference_eur",
                   "difference_pct", "difference_vs_competitor_pct", "position"):
        recalculated[column] = None
    recalculated["best_competitor"] = ""
    recalculated["best_url"] = ""
    recalculated["last_checked_at"] = ""
    recalculated["competitor_count"] = 0

    for ean, group in pairs.groupby("ean"):
        index = recalculated.index[recalculated["ean"] == ean]
        if not len(index):
            continue
        prices = group.sort_values(["competitor_eur", "competitor", "competitor_id"])
        best = prices.iloc[0]
        own = float(best["own_eur"])
        minimum = float(best["competitor_eur"])
        difference = own - minimum
        recalculated.loc[index, "competitor_min_eur"] = minimum
        recalculated.loc[index, "competitor_mean_eur"] = float(group["competitor_eur"].mean())
        recalculated.loc[index, "competitor_max_eur"] = float(group["competitor_eur"].max())
        recalculated.loc[index, "difference_eur"] = difference
        recalculated.loc[index, "difference_pct"] = difference / own * 100 if own else None
        recalculated.loc[index, "difference_vs_competitor_pct"] = difference / minimum * 100 if minimum else None
        recalculated.loc[index, "position"] = 1 + int((group["competitor_eur"] < own).sum())
        recalculated.loc[index, "best_competitor"] = best["competitor"]
        recalculated.loc[index, "best_url"] = best["url"]
        recalculated.loc[index, "last_checked_at"] = best["checked_at"]
        recalculated.loc[index, "competitor_count"] = int(group["competitor_id"].nunique())

    result = commercial_table(recalculated)
    result["position_rank"] = scoped["position"].values
    result["reference_model"] = scoped["model"].values if "model" in scoped.columns else ""
    result["color"] = scoped["color"].values if "color" in scoped.columns else ""
    result["commercial_name"] = scoped["commercial_name"].values if "commercial_name" in scoped.columns else ""
    if "commercial_name" in scoped.columns:
        result["model"] = scoped["commercial_name"].where(
            scoped["commercial_name"].fillna("").astype(bool), scoped.get("model", ""))
    for column in ("market", "own_price_status", "confirmed_offer_count"):
        if column in scoped.columns:
            result[column] = scoped[column].values
    result["competitor_count"] = recalculated["competitor_count"].values
    if states and STATE_ALL not in states:
        result = result[result["position"].isin(states)]
    return result.reset_index(drop=True)


def attention_table(summary: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Products priced above their best current, verified, comparable competitor offer."""
    table = commercial_table(summary)
    if table.empty:
        return table
    table = table[table["difference_eur"] > 0]
    return table.sort_values(["difference_vs_competitor_pct", "difference_eur"],
                             ascending=False, ignore_index=True).head(n)


def positioning_counts(summary: pd.DataFrame) -> pd.DataFrame:
    counts = summary.get("position", pd.Series(dtype=str)).value_counts()
    total = len(summary)
    return pd.DataFrame([
        {"position": POSITION_BEST, "count": int(counts.get(POSITION_BEST, 0)),
         "share_pct": float(counts.get(POSITION_BEST, 0) / total * 100) if total else 0.0},
        {"position": POSITION_HIGHER, "count": int(counts.get(POSITION_HIGHER, 0)),
         "share_pct": float(counts.get(POSITION_HIGHER, 0) / total * 100) if total else 0.0},
        {"position": POSITION_NONE, "count": int(counts.get(POSITION_NONE, 0)),
         "share_pct": float(counts.get(POSITION_NONE, 0) / total * 100) if total else 0.0},
    ])


def _offer_frame(store: PriceComparatorStore, ean: str | None = None) -> pd.DataFrame:
    query = """
        SELECT o.id,o.ean,o.competitor_id,c.name AS competitor_name,o.market,o.price,o.promotional_price,
               o.fx_to_eur,o.availability,o.match_status,o.captured_at,k.last_ok_at,k.last_offer_id
        FROM offers o JOIN competitors c ON c.id=o.competitor_id
        LEFT JOIN offer_checks k ON k.last_offer_id=o.id
        WHERE o.match_status='exact_match' AND o.price_includes_tax=1"""
    params: tuple = ()
    if ean:
        query += " AND o.ean=?"
        params = (ean,)
    with store._connect() as connection:
        frame = pd.read_sql_query(query + " ORDER BY o.captured_at,o.id", connection, params=params)
    frame["eur"] = _effective_eur(frame)
    return frame.dropna(subset=["eur"])


def _vytria_frame(store: PriceComparatorStore, ean: str | None = None) -> pd.DataFrame:
    query = """SELECT id,ean,market,price,currency,price_includes_tax,recorded_at FROM vytria_price_history
               WHERE validation_status='validated' AND currency='EUR' AND price_includes_tax=1"""
    params: tuple = ()
    if ean:
        query += " AND ean=?"
        params = (ean,)
    with store._connect() as connection:
        return pd.read_sql_query(query + " ORDER BY recorded_at,id", connection, params=params)


def recent_changes(store: PriceComparatorStore, days: int = 7, threshold_pct: float | None = None) -> pd.DataFrame:
    """Price moves at or above the threshold between consecutive stored observations (competitors and Vytria)."""
    threshold = threshold_pct if threshold_pct is not None else float(store.get_setting("alert_threshold_pct"))
    since = (_utc() - timedelta(days=days)).isoformat()
    rows = []
    offers = _offer_frame(store)
    for (ean, competitor), group in offers.groupby(["ean", "competitor_name"]):
        values = list(group.sort_values(["captured_at", "id"]).itertuples())
        for previous, current in zip(values[:-1], values[1:]):
            if current.captured_at >= since and previous.eur:
                change = (current.eur - previous.eur) / previous.eur * 100
                if abs(change) >= threshold:
                    rows.append({"ean": ean, "source": competitor, "before": previous.eur, "after": current.eur,
                                 "change_pct": change, "detected_at": current.captured_at})
    vytria = _vytria_frame(store)
    for ean, group in vytria.groupby("ean"):
        values = list(group.itertuples())
        for previous, current in zip(values[:-1], values[1:]):
            if current.recorded_at >= since and previous.price:
                change = (current.price - previous.price) / previous.price * 100
                if abs(change) >= threshold:
                    rows.append({"ean": ean, "source": "Vytria", "before": previous.price, "after": current.price,
                                 "change_pct": change, "detected_at": current.recorded_at})
    columns = ["ean", "source", "before", "after", "change_pct", "detected_at"]
    return pd.DataFrame(rows, columns=columns).sort_values("detected_at", ascending=False, ignore_index=True)


def price_series(store: PriceComparatorStore, ean: str, days: int, competitors=None,
                 now: datetime | None = None, market: str | None = None) -> pd.DataFrame:
    """Stored observations only. `kind` is 'change' (a new price was recorded) or 'check' (same price re-confirmed)."""
    end = now or _utc()
    since = (end - timedelta(days=days)).isoformat()
    rows = []
    own = _vytria_frame(store, ean)
    if market is None and not own.empty:
        market = own.sort_values(["recorded_at", "id"]).iloc[-1]["market"]
    if market:
        own = own[own["market"] == market]
    for item in own.itertuples():
        rows.append({"series": "Vytria", "at": item.recorded_at, "eur": item.price, "kind": "change"})
    offers = _offer_frame(store, ean)
    if market:
        offers = offers[offers["market"] == market]
    if competitors is not None:
        offers = offers[offers["competitor_name"].isin(competitors)]
    for item in offers.itertuples():
        available = item.availability == "available"
        rows.append({"series": item.competitor_name, "at": item.captured_at,
                     "eur": item.eur if available else float("nan"),
                     "kind": "change" if available else "unavailable"})
        if available and item.last_offer_id == item.id and item.last_ok_at and item.last_ok_at > item.captured_at:
            rows.append({"series": item.competitor_name, "at": item.last_ok_at, "eur": item.eur, "kind": "check"})
    frame = pd.DataFrame(rows, columns=["series", "at", "eur", "kind"]).sort_values(["series", "at"], ignore_index=True)
    if frame.empty:
        return frame
    # Keep the last observation before the window so the step line starts with the price in force.
    keep = []
    for _, group in frame.groupby("series"):
        inside = group[group["at"] >= since]
        before = group[group["at"] < since].tail(1)
        keep.append(pd.concat([before, inside]))
    return pd.concat(keep, ignore_index=True)


def competitiveness_series(store: PriceComparatorStore, days: int, now: datetime | None = None, *,
                           expiry_days: int | None = None, eans=None, competitors=None) -> pd.DataFrame:
    """Daily share of fresh, same-market compared references where Vytria matched or beat the competition."""
    end = now or _utc()
    expiry = expiry_days or store.price_expiry_days()
    vytria = _vytria_frame(store)
    offers = _offer_frame(store)
    if eans is not None:
        selected = set(eans)
        vytria = vytria[vytria["ean"].isin(selected)]
        offers = offers[offers["ean"].isin(selected)]
    if competitors is not None:
        offers = offers[offers["competitor_name"].isin(competitors)]
    if vytria.empty or offers.empty:
        return pd.DataFrame(columns=["day", "best_price_pct", "compared"])
    rows = []
    for offset in range(days, -1, -1):
        day = (end - timedelta(days=offset)).date()
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
        cutoff_dt = min(day_end, end)
        cutoff = cutoff_dt.isoformat()
        own = vytria[vytria["recorded_at"] <= cutoff].sort_values(["recorded_at", "id"]) \
            .groupby(["ean", "market"]).tail(1).copy()
        own["recorded_dt"] = pd.to_datetime(own["recorded_at"], utc=True)
        own = own[(cutoff_dt - own["recorded_dt"]) <= timedelta(days=expiry)]
        latest = offers[offers["captured_at"] <= cutoff].sort_values(["captured_at", "id"]) \
            .groupby(["ean", "competitor_id", "market"]).tail(1)
        # A later recheck extends freshness only from its actual date; future checks never leak backwards.
        latest = latest.copy()
        latest["captured_dt"] = pd.to_datetime(latest["captured_at"], utc=True)
        last_ok = pd.to_datetime(latest["last_ok_at"], utc=True, errors="coerce")
        latest["checked_dt"] = last_ok.where(last_ok.notna() & (last_ok <= cutoff_dt), latest["captured_dt"])
        latest = latest[(cutoff_dt - latest["checked_dt"]) <= timedelta(days=expiry)]
        latest = latest[latest["availability"] == "available"]
        minimum = latest.groupby(["ean", "market"])["eur"].min()
        own_prices = own.set_index(["ean", "market"])["price"]
        joined = pd.concat([own_prices.rename("own"), minimum.rename("min")], axis=1, join="inner").dropna()
        if len(joined):
            rows.append({"day": day, "best_price_pct": float((joined["own"] <= joined["min"]).mean() * 100),
                         "compared": len(joined)})
    return pd.DataFrame(rows, columns=["day", "best_price_pct", "compared"])


def top_differences(summary: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    table = commercial_table(summary)
    table = table[table["difference_eur"].notna()]
    return table.reindex(table["difference_eur"].abs().sort_values(ascending=False).index).head(n)


# Okabe-Ito colour-blind-safe palette; Vytria keeps the corporate green.
SERIES_COLOURS = ("#E69F00", "#0072B2", "#D55E00", "#CC79A7", "#56B4E9", "#7A5C00", "#000000", "#F0E442",
                  "#8C564B", "#6A3D9A", "#B15928", "#1F78B4", "#E31A1C")


def _colour(name: str, order: list[str]) -> str:
    if name == "Vytria":
        return BRAND_GREEN
    others = [item for item in order if item != "Vytria"]
    return SERIES_COLOURS[others.index(name) % len(SERIES_COLOURS)]


def price_figure(series: pd.DataFrame, title: str = ""):
    import plotly.graph_objects as go

    figure = go.Figure()
    names = sorted(series["series"].unique())
    for name, group in series.groupby("series"):
        group = group.sort_values("at")
        changes = group[group["kind"] == "change"]
        colour = _colour(name, names)
        # Step line: a price holds until the next recorded change. Gaps are not interpolated.
        figure.add_trace(go.Scatter(x=group["at"], y=group["eur"], name=name, mode="lines",
                                    line_shape="hv", connectgaps=False, legendgroup=name,
                                    line={"width": 4 if name == "Vytria" else 2, "color": colour},
                                    hoverinfo="skip"))
        # Markers also make a series with a single observation visible.
        figure.add_trace(go.Scatter(x=changes["at"], y=changes["eur"], name=f"{name} · cambio", mode="markers",
                                    marker={"size": 10, "symbol": "circle", "color": colour}, showlegend=False,
                                    legendgroup=name,
                                    hovertemplate="%{x|%d/%m/%Y %H:%M}<br>%{y:.2f} €<extra>" + name + "</extra>"))
        checks = group[group["kind"] == "check"]
        if not checks.empty:
            figure.add_trace(go.Scatter(x=checks["at"], y=checks["eur"], name=f"{name} · comprobación",
                                        mode="markers", marker={"size": 8, "symbol": "x", "color": colour},
                                        showlegend=False, legendgroup=name,
                                        hovertemplate="%{x|%d/%m/%Y %H:%M}<br>%{y:.2f} €<extra>" + name
                                        + " (comprobado)</extra>"))
    figure.update_layout(title=title, yaxis_title="Precio con IVA (€)", xaxis_title="Fecha (UTC)",
                         hovermode="closest", margin={"l": 10, "r": 10, "t": 40, "b": 10},
                         legend={"orientation": "h"}, height=460)
    figure.update_xaxes(tickformat="%d/%m/%Y", hoverformat="%d/%m/%Y %H:%M")
    figure.update_yaxes(ticksuffix=" €", rangemode="normal")
    return figure


def positioning_figure(positioning: pd.DataFrame):
    """Horizontal bars of the mean gap vs Vytria; negative = competitor cheaper."""
    import plotly.graph_objects as go

    data = positioning.sort_values("difference_pct", ascending=False)
    colours = ["#C2410C" if value < 0 else BRAND_GREEN for value in data["difference_pct"]]
    figure = go.Figure(go.Bar(
        x=data["difference_pct"], y=data["competitor"], orientation="h", marker={"color": colours},
        text=[f"{value:+.1f} %".replace(".", ",") for value in data["difference_pct"]], textposition="auto",
        customdata=data[["references", "vytria_mean_eur", "competitor_mean_eur"]],
        hovertemplate="%{y}: %{x:+.1f} %<br>Referencias comparadas: %{customdata[0]}<br>"
                      "Media Vytria: %{customdata[1]:.2f} €<br>Media competidor: %{customdata[2]:.2f} €"
                      "<extra></extra>"))
    figure.update_layout(xaxis_title="Diferencia media del competidor frente a Vytria (%)", yaxis_title="",
                         margin={"l": 10, "r": 10, "t": 20, "b": 10},
                         height=max(240, 60 + 48 * len(data)))
    figure.update_xaxes(ticksuffix=" %", zeroline=True, zerolinewidth=2)
    return figure


def competitiveness_figure(series: pd.DataFrame):
    import plotly.graph_objects as go

    figure = go.Figure(go.Scatter(x=series["day"], y=series["best_price_pct"], mode="lines+markers",
                                  line_shape="hv", name="Vytria igual o más barata",
                                  customdata=series[["compared"]],
                                  hovertemplate="%{x|%d/%m/%Y}: %{y:.1f} %<br>"
                                                "Productos comparados: %{customdata[0]}<extra></extra>"))
    figure.update_layout(yaxis_title="% de referencias", yaxis_range=[0, 100], xaxis_title="Día (UTC)",
                         margin={"l": 10, "r": 10, "t": 20, "b": 10})
    return figure


def positioning_distribution_figure(summary: pd.DataFrame):
    import plotly.graph_objects as go

    data = positioning_counts(summary)
    colours = {POSITION_BEST: BRAND_GREEN, POSITION_HIGHER: "#C2410C", POSITION_NONE: "#94A3B8"}
    labels = [f"{row.count} ({row.share_pct:.1f} %)".replace(".", ",") for row in data.itertuples()]
    figure = go.Figure(go.Bar(
        x=data["count"], y=data["position"], orientation="h",
        marker={"color": [colours[value] for value in data["position"]]},
        text=labels, textposition="auto",
        customdata=data[["share_pct"]],
        hovertemplate="%{y}: %{x} productos (%{customdata[0]:.1f} %)<extra></extra>"))
    figure.update_layout(xaxis_title="Productos del catálogo filtrado", yaxis_title="",
                         margin={"l": 10, "r": 10, "t": 10, "b": 10}, height=200)
    figure.update_xaxes(dtick=1, rangemode="tozero")
    return figure
