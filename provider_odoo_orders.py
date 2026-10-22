"""Optional, read-only EAN selection from real Odoo purchase orders for supplier image requests (#59).

Cloud never needs this module: EANs can always be typed or imported. Odoo only shows where an
EAN came from; it is never evidence that a downloaded image belongs to that EAN.
Nothing here writes to Odoo or contacts a portal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from kering_images import AccessNotConfirmed, utc_interval
from provider_images import PROVIDERS, ean_valid

LINKS_FILE = "provider_odoo_links.json"
SELECTION_LOG = "provider_odoo_selections.jsonl"
CONFIRMED_STATES = ("purchase", "done")  # confirmed / locked; draft, sent and cancel are excluded
DEFAULT_MAX_LINES = 2000
HARD_MAX_LINES = 10000
STATEMENT_TIMEOUT_MS = 20000

ELIGIBLE, DUPLICATE, REVIEW, COMPLETE = "Elegible", "Duplicado", "Requiere revisión", "Ya completa"
IMAGES_COMPLETE, IMAGES_PARTIAL, IMAGES_NONE = "Completas", "Parciales", "Ausentes"


class OdooUnavailable(Exception):
    """Odoo failed or timed out; the rest of Cloud keeps working."""
    code = "odoo_no_disponible"


class AssociationNotConfigured(AccessNotConfirmed):
    pass


@dataclass(frozen=True)
class OrderLine:
    ean: str
    reference: str
    product: str
    supplier: str
    order: str
    order_id: int
    date: str


# --- explicit Odoo supplier <-> portal association ----------------------------------------

def load_links(root: Path) -> dict:
    path = Path(root) / LINKS_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def confirmed_partner_ids(root: Path, provider: str) -> tuple[int, ...]:
    link = load_links(root).get(provider) or {}
    if not link.get("confirmed") or not link.get("partner_ids"):
        raise AssociationNotConfigured("asociacion_proveedor_portal_no_configurada")
    return tuple(int(p) for p in link["partner_ids"])


def save_link(root: Path, provider: str, partner_ids, *, reviewer: str, brands=(), note: str = "") -> dict:
    """Persist a reviewed association. Never called implicitly; needs an explicit reviewer."""
    if provider not in PROVIDERS:
        raise ValueError("proveedor_desconocido")
    ids = sorted({int(p) for p in partner_ids})
    if not ids:
        raise ValueError("sin_proveedores_odoo")
    if not str(reviewer).strip():
        raise ValueError("revisor_obligatorio")
    links = load_links(root)
    links[provider] = {"partner_ids": ids, "brands": [str(b).strip() for b in brands if str(b).strip()],
                       "confirmed": True, "reviewer": str(reviewer).strip(), "note": note,
                       "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = Path(root) / LINKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return links[provider]


def _read_only(connection, statement, parameters):
    transaction = connection.begin()
    try:
        if connection.engine.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        return connection.execute(statement, parameters).mappings().all()
    finally:
        transaction.rollback()


def propose_partners(engine, name_fragment: str, limit: int = 25) -> list[dict]:
    """Candidate Odoo suppliers by name. A proposal only: the operator must confirm it (save_link)."""
    fragment = (name_fragment or "").strip().lower()
    if len(fragment) < 3:
        raise ValueError("fragmento_demasiado_corto")
    query = text("""
        SELECT p.id, p.name, COALESCE(p.commercial_partner_id, p.id) AS commercial_id,
               (SELECT COUNT(*) FROM purchase_order po WHERE po.partner_id = p.id AND po.state IN ('purchase','done')) AS orders
        FROM res_partner p WHERE p.supplier_rank > 0 AND LOWER(p.name) LIKE :n ORDER BY p.id LIMIT :limit""")
    try:
        with engine.connect() as connection:
            rows = _read_only(connection, query, {"n": f"%{fragment}%", "limit": min(int(limit), 100)})
    except SQLAlchemyError as error:
        raise OdooUnavailable(str(error.__class__.__name__)) from error
    return [dict(r) for r in rows]


# --- purchase-order lines ----------------------------------------------------------------

def read_order_lines(engine, partner_ids, start: date, end: date, *, max_lines: int = DEFAULT_MAX_LINES,
                     include_commercial_contacts: bool = True) -> tuple[list[OrderLine], bool]:
    """Read-only, bounded read of lines of confirmed/done orders. Returns (lines, truncated)."""
    max_lines = max(1, min(int(max_lines), HARD_MAX_LINES))
    lower, upper = utc_interval(start, end)
    supplier_filter = "po.partner_id IN :partners"
    if include_commercial_contacts:
        supplier_filter = """po.partner_id IN (
            SELECT contact.id FROM res_partner contact
            JOIN res_partner selected ON selected.id IN :partners
            WHERE COALESCE(contact.commercial_partner_id, contact.id)
                = COALESCE(selected.commercial_partner_id, selected.id))"""
    query = text(f"""
        SELECT po.id AS order_id, po.name AS order_name, po.date_order, partner.name AS supplier,
               pp.barcode, pp.default_code, pol.name AS line_name
        FROM purchase_order po
        JOIN res_partner partner ON partner.id = po.partner_id
        JOIN purchase_order_line pol ON pol.order_id = po.id
          AND COALESCE(pol.display_type, '') = '' AND pol.product_id IS NOT NULL
        JOIN product_product pp ON pp.id = pol.product_id
        WHERE {supplier_filter} AND po.state IN :states
          AND po.date_order >= :lower AND po.date_order < :upper
        ORDER BY po.date_order DESC, po.id DESC, pol.id
        LIMIT :limit""").bindparams(bindparam("partners", expanding=True), bindparam("states", expanding=True))
    parameters = {"partners": [int(p) for p in partner_ids], "states": list(CONFIRMED_STATES),
                  "lower": lower, "upper": upper, "limit": max_lines + 1}
    try:
        with engine.connect() as connection:
            rows = _read_only(connection, query, parameters)
    except SQLAlchemyError as error:
        raise OdooUnavailable(str(error.__class__.__name__)) from error
    truncated = len(rows) > max_lines
    lines = [OrderLine((r["barcode"] or "").strip(), (r["default_code"] or "").strip(), r["line_name"] or "",
                       r["supplier"] or "", r["order_name"] or "", int(r["order_id"]), str(r["date_order"])[:10])
             for r in rows[:max_lines]]
    return lines, truncated


def _images_state(repository, ean: str, required) -> str:
    pending = repository.pending_views(ean, tuple(required))
    if not pending:
        return IMAGES_COMPLETE
    return IMAGES_NONE if len(pending) == len(tuple(required)) else IMAGES_PARTIAL


def build_preview(lines, repository, settings, live_eans=frozenset(), *, max_refs: int = 200) -> dict:
    """One row per distinct EAN (most recent order first) plus review rows for lines without a usable EAN."""
    rows, seen, summary = [], set(), {"sin_ean": 0, "ean_invalido": 0, "duplicados": 0}
    for line in lines:
        base = {"EAN": line.ean, "Referencia": line.reference, "Producto": line.product,
                "Proveedor": line.supplier, "Pedido": line.order, "Fecha": line.date}
        if not line.ean:
            summary["sin_ean"] += 1
            rows.append({**base, "Imágenes": "", "Estado": REVIEW, "Motivo": "sin EAN en Odoo"})
        elif not ean_valid(line.ean):
            summary["ean_invalido"] += 1
            rows.append({**base, "Imágenes": "", "Estado": REVIEW, "Motivo": "EAN inválido"})
        elif line.ean in seen:
            summary["duplicados"] += 1
            rows.append({**base, "Imágenes": "", "Estado": DUPLICATE, "Motivo": "EAN repetido en otra línea"})
        elif line.ean in live_eans:
            seen.add(line.ean)
            rows.append({**base, "Imágenes": _images_state(repository, line.ean, settings.required_views),
                         "Estado": DUPLICATE, "Motivo": "solicitud en curso"})
        else:
            seen.add(line.ean)
            images = _images_state(repository, line.ean, settings.required_views)
            rows.append({**base, "Imágenes": images,
                         "Estado": COMPLETE if images == IMAGES_COMPLETE else ELIGIBLE, "Motivo": ""})
    eligible = [r for r in rows if r["Estado"] == ELIGIBLE]
    return {"rows": rows[:max_refs] if max_refs else rows, "summary": summary, "eligible": len(eligible),
            "shown_truncated": len(rows) > max_refs}


def pick_pilot(rows, size: int = 8) -> list[str]:
    """Proposal for a small real batch: eligible, no images first, then partial, most recent orders first.

    Partner membership is already guaranteed by the confirmed association used to read the orders.
    """
    size = max(1, min(int(size), 10))
    rank = {IMAGES_NONE: 0, IMAGES_PARTIAL: 1}
    eligible = [r for r in rows if r["Estado"] == ELIGIBLE and r["Imágenes"] in rank]
    eligible.sort(key=lambda r: (rank[r["Imágenes"]], -int(r["Fecha"].replace("-", "") or 0)))
    return [r["EAN"] for r in eligible[:size]]


def demand_preview(engine, connector, root: Path, start: date, end: date, *, max_lines=DEFAULT_MAX_LINES,
                   max_refs: int = 200) -> dict:
    """Odoo -> preview rows for the chosen portal. Raises AssociationNotConfigured / OdooUnavailable."""
    service = connector.service
    partners = confirmed_partner_ids(root, service.settings.provider)
    lines, truncated = read_order_lines(engine, partners, start, end, max_lines=max_lines)
    live = service.store.live_eans(service.settings.provider)
    preview = build_preview(lines, service.repository, service.settings, live, max_refs=max_refs)
    return {**preview, "lines_read": len(lines), "truncated": truncated, "partners": list(partners)}


def create_requests(connector, root: Path, rows, selected_eans, *, approved_by: str) -> dict:
    """Plan the operator-approved EANs through the common planner. Never contacts a portal.

    Only EANs that are Eligible in the shown preview can be planned, whatever was ticked.
    """
    if not str(approved_by).strip():
        raise ValueError("aprobacion_obligatoria")
    by_ean = {r["EAN"]: r for r in rows if r["Estado"] == ELIGIBLE}
    chosen = [e for e in dict.fromkeys(selected_eans) if e in by_ean]
    rejected = [e for e in dict.fromkeys(selected_eans) if e not in by_ean]
    result = connector.plan_eans(chosen, limit=len(chosen)) if chosen else {
        "created": [], "already_live": [], "invalid": [], "selected": 0}
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": connector.service.settings.provider,
             "approved_by": str(approved_by).strip(), "source": "odoo_purchase_orders",
             "eans": [{"ean": e, "order": by_ean[e]["Pedido"], "reference": by_ean[e]["Referencia"]} for e in chosen]}
    if chosen:
        path = Path(root) / SELECTION_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {**result, "not_eligible": rejected}
