"""Read-only Marcolin validation against the real Odoo plus pilot batch proposal (#59).

Uses Cloud's existing connection (kering_jobs.db_engine). Never prints or stores credentials, never
writes to Odoo, never saves a supplier association and never creates portal requests. The proposal
is written as a file for human review; approving it is a separate, explicit step in Cloud.
Run it where Cloud can reach Odoo:  python scripts/marcolin_odoo_pilot.py --partner 573 --size 8
Exit codes: 0 report written, 2 Odoo unavailable (blocker recorded), 3 partner 573 is not Marcolin.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_repository import ImageRepository  # noqa: E402
from marcolin_images import DEFAULTS  # noqa: E402
from provider_images import ean_valid  # noqa: E402
from provider_odoo_orders import (  # noqa: E402
    DEFAULT_MAX_LINES, ELIGIBLE, OdooUnavailable, build_preview, pick_pilot, read_order_lines,
)

FAMILY = """SELECT contact.id, contact.name, contact.active, contact.supplier_rank, contact.commercial_partner_id
 FROM res_partner contact JOIN res_partner selected ON selected.id = :partner
 WHERE COALESCE(contact.commercial_partner_id, contact.id) = COALESCE(selected.commercial_partner_id, selected.id)
 ORDER BY contact.id"""
REASONS = {"Ausentes": "sin ninguna vista en la biblioteca", "Parciales": "vistas incompletas en la biblioteca"}


def verify_partner(engine, partner_id: int) -> dict:
    """Read-only identity check. A name match is reported, never saved as an association."""
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if engine.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL statement_timeout = 20000"))
                family = [dict(r) for r in connection.execute(text(FAMILY), {"partner": partner_id}).mappings()]
                states = {r[0]: r[1] for r in connection.execute(text(
                    "SELECT state, COUNT(*) FROM purchase_order WHERE partner_id IN "
                    "(SELECT id FROM res_partner WHERE COALESCE(commercial_partner_id, id) = "
                    "(SELECT COALESCE(commercial_partner_id, id) FROM res_partner WHERE id = :partner)) "
                    "GROUP BY state"), {"partner": partner_id})}
            finally:
                transaction.rollback()
    except Exception as error:  # report the class only: messages may embed connection details
        raise OdooUnavailable(error.__class__.__name__) from error
    main = next((p for p in family if p["id"] == partner_id), None)
    return {"found": main is not None, "name": main["name"] if main else None,
            "is_marcolin_by_name": bool(main and "marcolin" in (main["name"] or "").lower()),
            "family": family, "orders_by_state": states}


def build_report(engine, repository, partner_id: int, start: date, end: date, size: int, max_lines: int) -> dict:
    identity = verify_partner(engine, partner_id)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "partner_id": partner_id,
              "period": [start.isoformat(), end.isoformat()], "identity": identity,
              "association_saved": False, "requests_created": 0}
    if not identity["found"] or not identity["is_marcolin_by_name"]:
        report["blocker"] = "partner_no_coincide_con_marcolin"
        return report
    lines, truncated = read_order_lines(engine, [partner_id], start, end, max_lines=max_lines)
    preview = build_preview(lines, repository, DEFAULTS, max_refs=0)
    rows = preview["rows"]
    picks = pick_pilot(rows, size)
    by_ean = {r["EAN"]: r for r in rows if r["Estado"] == ELIGIBLE}
    report.update({
        "lines_read": len(lines), "truncated": truncated, "summary": preview["summary"],
        "distinct_valid_ean": len({r["EAN"] for r in rows if r["EAN"] and ean_valid(r["EAN"])}),
        "images": {k: sum(1 for r in rows if r["Imágenes"] == k) for k in ("Completas", "Parciales", "Ausentes")},
        "proposal": [{"EAN": e, "Referencia": by_ean[e]["Referencia"], "Pedido": by_ean[e]["Pedido"],
                      "Fecha": by_ean[e]["Fecha"], "Imágenes": by_ean[e]["Imágenes"],
                      "Proveedor": by_ean[e]["Proveedor"], "Motivo": REASONS[by_ean[e]["Imágenes"]]} for e in picks],
        "status": "propuesta_pendiente_de_aprobacion",
    })
    if len(picks) < min(5, size):
        report["warning"] = "menos de 5 EAN elegibles en el periodo: ampliar fechas"
    return report


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"marcolin_pilot_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if report.get("proposal"):
        with path.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report["proposal"][0]))
            writer.writeheader()
            writer.writerows(report["proposal"])
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partner", type=int, default=573)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--out", type=Path,
                        default=Path(os.getenv("PROVIDER_DATA_ROOT", "masterdata_data/providers")) / "proposals")
    args = parser.parse_args(argv)
    end = date.today()
    try:
        from kering_jobs import db_engine  # noqa: PLC0415
        report = build_report(db_engine(), ImageRepository(), args.partner, end - timedelta(days=args.days), end,
                              args.size, args.max_lines)
    except Exception as error:  # includes missing DB_* configuration or an unreachable host
        report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "partner_id": args.partner, "blocker": "odoo_no_disponible",
                  "error_class": error.__class__.__name__, "association_saved": False, "requests_created": 0}
    path = write_report(report, args.out)
    print(json.dumps({k: v for k, v in report.items() if k != "proposal"}, indent=2, ensure_ascii=False))
    print(f"Informe: {path}")
    if report.get("blocker") == "odoo_no_disponible":
        return 2
    return 3 if report.get("blocker") else 0


if __name__ == "__main__":
    raise SystemExit(main())
