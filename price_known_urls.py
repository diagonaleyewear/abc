"""Idempotent import of the product pages (fichas) already located by hand at the 14 competitors.

Nothing here queries a website, creates offers, enables a connector or touches the scheduler: every page is
stored as a candidate (``pending_verification``) that a later, authorised check must confirm. Existing rows are
never overwritten. Manual commands only::

    python price_known_urls.py                      # dry run (default): prints the planned operations
    python price_known_urls.py --apply --backup-dir /app/data/backups
    python price_known_urls.py --revert             # removes only the rows this import added and left pending
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse
from typing import Iterable

from price_comparator import PriceComparatorStore, resolve_database_path
from price_web import WebSearchError, _same_site, validate_competitor_url

IMPORT_SOURCE = "known_urls"
IMPORT_NOTE = "Ficha candidata localizada manualmente (2026-10-30); sin comprobar. No es una oferta."
CURRENT = ("verified", "probable", "pending_verification")


@dataclass(frozen=True)
class KnownUrl:
    competitor: str  # label only; the competitor is identified by the domain of the URL
    ean: str
    model: str  # model the page is supposed to show; used to refuse a mismatching catalogue product
    color: str
    url: str


PRADA_27ZS = ("8056597875349", "PR 27ZS", "16K08Z")
PRADA_26ZS = ("8056597875226", "PR 26ZS", "16K08Z")

KNOWN_URLS: tuple[KnownUrl, ...] = tuple(KnownUrl(name, *product, url) for name, product, url in (
    ("Visio Factory", PRADA_27ZS, "https://www.visiofactory.es/gafas-de-sol/todas-las-solares/pr27zs-16k08z-54-21.html"),
    ("Óptica Óptima", PRADA_27ZS, "https://optica-optima.com/gafas-de-sol/prada-pr-27zs-16k08z.html"),
    ("Domanci", PRADA_27ZS, "https://domanci.com/es/gafas-de-sol/prada-pr-27zs-16k08z-54.html"),
    ("Amevista", PRADA_27ZS, "https://www.amevista.com/es/prada-0pr-27zs-16k08z"),
    ("Edel-Optics", PRADA_27ZS, "https://www.edel-optics.es/PR-27ZS-16K08Z-de-Prada.html?selectedSize=1907958"),
    ("Otticait Eyewear", PRADA_27ZS, "https://www.otticait.com/en/prada-pr-27zs-16k08z"),
    ("ConGafasDeSol", PRADA_27ZS, "https://www.congafasdesol.com/prada-pr-27zs-16k08z.html"),
    ("Blickers", PRADA_27ZS, "https://www.blickers.com/en-es/glasses/prada-pr-27zs-c54/5649966192263168/"),
    ("Óptica Bassol", PRADA_27ZS, "https://www.opticabassol.com/en/products/prada-pr-27zs-16k08z-54"),
    ("Visual-Click", PRADA_27ZS, "https://visual-click.com/products/prada-pr-27zs-16k08z-54"),
    ("Optical H", PRADA_27ZS, "https://opticalh.com/en/products/prada-pr-27zs-16k08z-54-21-sunglasses-62083"),
    ("Óptica Universitaria", PRADA_27ZS,
     "https://www.opticauniversitaria.es/gafas-de-sol/prada/prada-pr-27zs-16k08z/312245.html"),
    ("Céline Opticien Lunetier", PRADA_27ZS,
     "https://www.celine-opticien-lunetier.com/es/products/gafas-de-sol-prada-symbole-pr-27zs"),
    # Mia Burton only has the PR 26ZS page: it belongs to the 26ZS EAN, never to the 27ZS one.
    ("Mia Burton", PRADA_26ZS, "https://miaburton.com/es/gafas-de-sol/prada/pr-26zs-16k08z"),
))


@dataclass(frozen=True)
class Operation:
    competitor: str
    competitor_id: str
    ean: str
    url: str
    action: str  # add | exists | skip
    reason: str = ""


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _model_core(model: str) -> str:
    return _compact(model).removeprefix("pr")


def _readonly(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def plan(db_path: Path, entries: Iterable[KnownUrl] = KNOWN_URLS) -> list[Operation]:
    """Decide what would be done. Reads the database only (read-only connection, no schema creation)."""
    operations: list[Operation] = []
    connection = _readonly(db_path)
    try:
        competitors = [dict(r) for r in connection.execute("SELECT id,name,domain FROM competitors")]
        products = {r["ean"]: dict(r) for r in connection.execute("SELECT ean,model,color FROM products")}
        registered = [dict(r) for r in connection.execute("SELECT competitor_id,ean,url,status FROM competitor_product_urls")]
    finally:
        connection.close()
    for entry in entries:
        matches = [c for c in competitors if c["domain"] and _same_site(entry.url, c["domain"])]
        if len(matches) != 1:
            operations.append(Operation(entry.competitor, "", entry.ean, entry.url, "skip",
                                        "competidor no registrado" if not matches else "dominio ambiguo"))
            continue
        competitor = matches[0]
        base = (competitor["name"], competitor["id"], entry.ean, entry.url)
        try:
            clean = validate_competitor_url(entry.url, competitor["domain"])
        except WebSearchError as exc:
            operations.append(Operation(*base, "skip", str(exc)))
            continue
        product = products.get(entry.ean)
        if product is None:
            operations.append(Operation(*base, "skip", "el EAN no está en el catálogo"))
        elif _model_core(entry.model) not in _compact(product["model"]) or \
                _compact(entry.color) not in _compact(product["color"]):
            operations.append(Operation(*base, "skip", f"el catálogo ({product['model']} {product['color']}) "
                                                       f"no coincide con {entry.model} {entry.color}"))
        elif _model_core(entry.model) not in _compact(urlparse(clean).path):
            # only a negative check: a matching slug proves nothing, a contradicting one is refused
            operations.append(Operation(*base, "skip", f"la URL no corresponde al modelo {entry.model}"))
        else:
            mine = [r for r in registered if r["competitor_id"] == competitor["id"] and r["ean"] == entry.ean]
            if any(r["url"] == clean for r in mine):
                operations.append(Operation(*base, "exists", "ya registrada; no se modifica"))
            elif any(r["status"] in CURRENT for r in mine):
                operations.append(Operation(*base, "skip", "ya hay otra ficha vigente; no se sobrescribe"))
            else:
                operations.append(Operation(competitor["name"], competitor["id"], entry.ean, clean, "add"))
    return operations


def backup_database(db_path: Path, directory: Path) -> tuple[Path, str]:
    """Consistent copy (SQLite online backup, WAL-safe) verified with integrity_check. Returns (file, sha256)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"price_comparator.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    source = _readonly(db_path)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
    finally:
        source.close()
    if result != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"La copia no supera integrity_check: {result}")
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


def apply(db_path: Path, operations: list[Operation]) -> dict[str, int]:
    """Insert the planned candidates. Primary key + INSERT OR IGNORE make a repeated run a no-op."""
    store = PriceComparatorStore(db_path)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    with store._connect() as connection, connection:
        for op in operations:
            if op.action != "add":
                continue
            added += connection.execute(
                """INSERT OR IGNORE INTO competitor_product_urls(competitor_id,ean,url,match_status,status,verified_at,
                       source,note,submitted_at) VALUES(?,?,?,'manual_review','pending_verification',?,?,?,?)""",
                (op.competitor_id, op.ean, op.url, stamp, IMPORT_SOURCE, IMPORT_NOTE, stamp)).rowcount
        connection.execute("INSERT OR IGNORE INTO competitor_web_config(competitor_id) SELECT id FROM competitors")
    return {"added": added, "exists": sum(o.action == "exists" for o in operations),
            "skipped": sum(o.action == "skip" for o in operations)}


def revert(db_path: Path) -> int:
    """Remove only the pages this import added that are still untouched candidates."""
    store = PriceComparatorStore(db_path)
    with store._connect() as connection, connection:
        return connection.execute(
            "DELETE FROM competitor_product_urls WHERE source=? AND status='pending_verification' AND last_price IS NULL",
            (IMPORT_SOURCE,)).rowcount


def audit(db_path: Path) -> list[str]:
    """Read-only facts about what the database holds, to tell 'URLs absent' from 'URLs not displayed'."""
    connection = _readonly(db_path)
    try:
        one = lambda sql: connection.execute(sql).fetchone()[0]  # noqa: E731
        lines = [f"competitors={one('SELECT COUNT(*) FROM competitors')} "
                 f"web_config={one('SELECT COUNT(*) FROM competitor_web_config')} "
                 f"schedule={one('SELECT COUNT(*) FROM competitor_schedule')} "
                 f"schedule_enabled={one('SELECT COUNT(*) FROM competitor_schedule WHERE enabled=1')}",
                 f"products={one('SELECT COUNT(*) FROM products')} offers={one('SELECT COUNT(*) FROM offers')} "
                 f"pending_jobs={one('SELECT COUNT(*) FROM scheduler_jobs')}"]
        lines += [f"product_urls status={r[0]} source={r[1]}: {r[2]}" for r in connection.execute(
            "SELECT status,source,COUNT(*) FROM competitor_product_urls GROUP BY status,source")]
        if not one("SELECT COUNT(*) FROM competitor_product_urls"):
            lines.append("competitor_product_urls: VACÍA (las URLs no están cargadas)")
        return lines
    finally:
        connection.close()


def integrity(db_path: Path) -> str:
    connection = _readonly(db_path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa las fichas conocidas como candidatas (sin consultas web).")
    parser.add_argument("--db", type=Path, default=None, help="Base SQLite; por defecto la del comparador")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Muestra las operaciones sin escribir (por defecto)")
    mode.add_argument("--apply", action="store_true", help="Aplica; exige --backup-dir")
    mode.add_argument("--revert", action="store_true", help="Elimina las candidatas importadas aún sin tocar")
    mode.add_argument("--audit", action="store_true", help="Solo lectura: cuenta lo que hay en las tablas")
    parser.add_argument("--backup-dir", type=Path, help="Directorio donde guardar la copia previa de SQLite")
    args = parser.parse_args(argv)
    db_path = args.db or resolve_database_path()
    if not Path(db_path).is_file():
        print(f"No existe la base de datos: {db_path}")
        return 2
    if args.audit:
        print("\n".join(audit(db_path)), f"\nintegrity_check: {integrity(db_path)}", sep="")
        return 0
    if args.revert:
        if not args.backup_dir:
            print("--revert exige --backup-dir (copia previa)")
            return 2
        target, digest = backup_database(db_path, args.backup_dir)
        print(f"Copia previa: {target} sha256={digest}")
        print(f"Fichas candidatas retiradas: {revert(db_path)}")
        return 0
    operations = plan(db_path)
    for op in operations:
        print(f"{op.action.upper():6} | {op.competitor or '-'} | {op.ean} | {op.url}" + (f" | {op.reason}" if op.reason else ""))
    if not args.apply:
        print("Simulación: no se ha escrito nada. Use --apply --backup-dir <dir> para aplicar.")
        return 0
    if not args.backup_dir:
        print("--apply exige --backup-dir: se hace una copia consistente antes de escribir")
        return 2
    check = integrity(db_path)
    if check != "ok":
        print(f"integrity_check previo incorrecto: {check}; no se aplica")
        return 1
    target, digest = backup_database(db_path, args.backup_dir)
    print(f"Copia previa verificada: {target} sha256={digest}")
    print(apply(db_path, operations), f"integrity_check posterior: {integrity(db_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
