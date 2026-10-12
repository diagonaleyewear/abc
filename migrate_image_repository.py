"""Offline, copy-only image migration. Inventory is always the first step."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from PIL import Image

from filelock import FileLock
from repository_storage import (
    state_root, backup_root, require_local, check_image_root, check_backup_root,
    check_migration_target, check_staging_role, staging_root,
)

from graph_mail_downloader import (
    _image_model_color_key, _load_eans_by_image_key, _market_view_from_image_name,
    _model_from_image_name,
    _market_image_names,
)
from image_repository import (
    ImageRepository, VIEW_ALIASES, atomic_write, collision_name, repository_root, validate_ean,
    file_checksum as checksum, copy_verified,
    image_fingerprint,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".img"}
IMAGE_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}


def source_orders(root: Path) -> list[dict]:
    history = root / "history.sqlite3"
    if not history.exists():
        return []
    with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT run_id,order_id,snapshot,results FROM attempts").fetchall()
    return [{"source": "Kering", "order_id": str(row[1]), "run_id": row[0],
             "snapshot": json.loads(row[2]), "results": json.loads(row[3])} for row in rows]


def inventory(sources: list[tuple[str, Path]], target: Path, mapping: dict | None = None) -> dict:
    check_migration_target(target)
    target = target.resolve()
    entries: list[dict] = []
    orders: list[dict] = []
    blockers: list[str] = []
    mappings = (mapping or {}).get("files", {})
    eans_by_key = _load_eans_by_image_key()
    planned: dict[tuple[str, str], str] = {}
    content_paths: dict[tuple[str, str], str] = {}
    existing: dict[tuple[str, str], str] = {}
    # Read-only: inventory must not create a catalog or destination.
    storage = {"state_root": str(state_root(target)), "backup_root": str(backup_root(target))}
    database = Path(storage["state_root"]) / ".catalog.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            for ean, path, digest in connection.execute("SELECT ean,path,checksum FROM assets"):
                existing[(ean, Path(path).name)] = digest
                content_paths[(ean, digest)] = path
    roots = []
    for provider, source in sources:
        staged = staging_root()
        if staged is not None:
            check_staging_role(staged / "source", "source")
            absolute = source.absolute()
            if absolute.resolve() != absolute or not (
                    absolute == staged / "source" or staged / "source" in absolute.parents):
                raise ValueError("Origen de ensayo debe estar en source congelada")
        source = source.resolve()
        if not source.is_dir():
            raise ValueError(f"No existe origen: {source}")
        if source == target or source in target.parents or target in source.parents:
            raise ValueError("Inventario requiere origen y destino separados")
        for destination_root in {Path(value) for value in storage.values()}:
            if source == destination_root or source in destination_root.parents or destination_root in source.parents:
                raise ValueError("Estado y backups deben estar separados de los origenes")
        if any(source == root or source in root.parents or root in source.parents for root in roots):
            raise ValueError("Los origenes no pueden solaparse")
        roots.append(source)
        source_id = hashlib.sha256(str(source).encode()).hexdigest()
        if provider == "Kering":
            orders.extend(source_orders(source))
        paths = []
        for directory, directories, filenames in os.walk(source, followlinks=False):
            directories[:] = [name for name in directories if name != "staging"]
            paths.extend(Path(directory) / name for name in filenames)
            paths.extend(Path(directory) / name for name in directories
                         if (Path(directory) / name).is_symlink())
        for path in sorted(paths):
            if not path.is_file():
                continue
            if path.is_symlink() or source not in path.resolve().parents:
                blockers.append(f"Enlace fuera del inventario permitido: {path}")
                continue
            if path.name.endswith(("-wal", "-journal")) and path.stat().st_size:
                blockers.append(f"SQLite activo; detener escritores y checkpoint: {path}")
            if path.suffix == ".lock" or path.name.endswith("-shm"):
                continue
            relative = path.relative_to(source)
            info = mappings.get(str(path), {})
            ean = str(info.get("ean", ""))
            if not ean:
                ean = next((part for part in reversed(relative.parts[:-1])
                            if part.isascii() and part.isdigit() and len(part) <= 20), "")
            if not ean and provider == "Luxoptica":
                ean = eans_by_key.get(_image_model_color_key(path.name), "")
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS
            if is_image and not ean:
                blockers.append(f"EAN sin resolver: {path}")
            if ean:
                validate_ean(ean)
            market = str(info.get("market", next(
                (part for part in relative.parts if part in {"Farfetch", "Miinto"}), "")))
            view = str(info.get("view", ""))
            if not view and provider == "Kering" and path.stem in {"frontal", "lateral", "perspectiva", "detalle"}:
                view = path.stem
            if not view:
                view = _market_view_from_image_name(path.name) or "unknown"
                if market and path.stem.rsplit("_", 1)[-1] in VIEW_ALIASES:
                    view = VIEW_ALIASES[path.stem.rsplit("_", 1)[-1]]
            digest = checksum(path)
            valid = None
            pixels = None
            if is_image:
                try:
                    pixels = image_fingerprint(path.read_bytes())
                    valid = True
                except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
                    valid = False
                    blockers.append(f"Imagen no reutilizable; revisar o reparar antes de migrar: {path}")
            conflict = False
            destination = None
            if is_image and ean:
                key = (ean, path.name)
                previous = planned.get(key, existing.get(key))
                existing_path = target / ean / path.name
                if previous is None and existing_path.exists():
                    previous = checksum(existing_path)
                conflict = previous is not None and previous != digest
                stored_name = collision_name(path.name, digest) if conflict else path.name
                planned.setdefault(key, digest)
                destination = content_paths.setdefault((ean, digest), f"{ean}/{stored_name}")
            entries.append({
                "source": str(path), "source_root": str(source), "relative": relative.as_posix(),
                "backup": f"{source_id}/{relative.as_posix()}", "provider": provider,
                "ean": ean, "view": view, "market": market, "image": is_image,
                "name": path.name, "date": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "checksum": digest, "size": path.stat().st_size, "destination": destination,
                "valid": valid, "pixels": pixels,
                "conflict": conflict, "origin": info.get("origin", f"legacy:{path}"),
                "metadata": info.get("metadata", {"modelo": _model_from_image_name(path.name)}),
                "orders": info.get("orders", []),
            })
    body = {"version": 2, "target": str(target), "storage": storage,
            "entries": entries, "orders": orders, "blockers": blockers}
    plan_id = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "id": plan_id}


def check_plan(plan: dict) -> None:
    check_migration_target(Path(plan["target"]))
    body = {key: value for key, value in plan.items() if key != "id"}
    if plan.get("version") not in {1, 2} or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != plan["id"]:
        raise ValueError("Plan modificado; repetir inventario y simulacion")
    if plan["blockers"]:
        raise ValueError("Inventario bloqueado: " + "; ".join(plan["blockers"]))


def external_backup(entry: dict, plan: dict) -> bool:
    return entry["image"] or (
        plan["version"] >= 2 and Path(entry["name"]).suffix.lower() in IMAGE_ARCHIVE_EXTENSIONS)


def legacy_root(repository: ImageRepository, plan: dict, entry: dict) -> Path:
    root = repository.backup_root if external_backup(entry, plan) else repository.state_root
    return root / ".migration" / plan["id"] / "originals"


def plan_storage(plan: dict) -> tuple[Path, Path]:
    images = Path(plan["target"]).resolve()
    configured = plan.get("storage")
    if configured is None:
        if state_root(images) != images or backup_root(images) != images:
            raise ValueError("Plan antiguo sin raices de estado/backups; repetir inventario")
        return images, images
    state = Path(configured["state_root"]).resolve()
    backups = Path(configured["backup_root"]).resolve()
    require_local(state)
    return state, backups


def apply(plan: dict) -> dict:
    check_plan(plan)
    if any(entry["image"] and entry["valid"] is not True for entry in plan["entries"]):
        raise ValueError("Imagen no reutilizable; revisar y repetir inventario antes de migrar")
    # Reject stale plans before copying even the first file.
    for entry in plan["entries"]:
        if checksum(Path(entry["source"])) != entry["checksum"]:
            raise ValueError(f"Origen modificado; repetir inventario: {entry['source']}")
    state, backups = plan_storage(plan)
    check_backup_root(Path(plan["target"]).resolve(), backups)
    repository = ImageRepository(Path(plan["target"]), state=state, backups=backups)
    with FileLock(repository.root / ".migration.lock", timeout=120):
        for entry in plan["entries"]:
            check_backup_root(repository.root, repository.backup_root)
            backup = legacy_root(repository, plan, entry) / entry["backup"]
            copy_verified(Path(entry["source"]), backup, entry["checksum"],
                          guard=lambda: check_backup_root(repository.root, repository.backup_root))
            destination = None
            if entry["image"]:
                record, _ = repository.save(
                    entry["ean"], entry["name"], backup.read_bytes(), provider=entry["provider"],
                    origin=entry["origin"], view=entry["view"], market=entry["market"],
                    date=entry["date"], metadata=entry["metadata"],
                )
                destination = record.path.relative_to(repository.root).as_posix()
                for order_id in entry["orders"]:
                    repository.associate_order(entry["provider"], str(order_id), [entry["ean"]])
            with repository.maintenance_lock(), repository.connect() as connection:
                connection.execute("INSERT OR REPLACE INTO migrations VALUES(?,?,?,?,?)",
                    (plan["id"], entry["source"], entry["checksum"],
                     backup.relative_to(repository.backup_root if external_backup(entry, plan) else repository.state_root).as_posix(),
                     destination))
        for order in plan["orders"]:
            repository.associate_order(order["source"], order["order_id"],
                [line["ean"] for line in order["snapshot"]["lines"] if line["ean"]])
        _merge_mail_state(repository, plan)
        _merge_pending(repository)
        atomic_write(repository.state_root / ".migration" / plan["id"] / "plan.json",
                     json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"))
    return verify(plan)


def _merge_mail_state(repository: ImageRepository, plan: dict) -> None:
    with FileLock(repository.root / ".mail.lock", timeout=120):
        state_file = repository.state_root / ".mail_download_state.json"
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        ids = set(state.get("processed_message_ids", []))
        for entry in plan["entries"]:
            if entry["name"] == ".mail_download_state.json":
                old = json.loads((legacy_root(repository, plan, entry) / entry["backup"]).read_text(encoding="utf-8"))
                ids.update(old.get("processed_message_ids", []))
        state["processed_message_ids"] = sorted(ids)
        atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))


def _merge_pending(repository: ImageRepository) -> None:
    with FileLock(repository.root / ".market.lock", timeout=120):
        records = repository.records()
        pending = {}
        for record in records:
            if record.market or not _market_image_names(record.name, {"Farfetch": "check"}):
                continue
            markets = {other.market for other in records
                       if other.ean == record.ean and other.checksum == record.checksum}
            missing = [market for market in ("Farfetch", "Miinto") if market not in markets]
            if missing:
                pending[str(record.path)] = {"ean": record.ean, "missing_markets": missing}
        atomic_write(repository.state_root / ".market_pending.json",
                     json.dumps(pending, ensure_ascii=False, indent=2).encode("utf-8"))


def verify(plan: dict, repository: Path | None = None) -> dict:
    check_plan(plan)
    root = (repository or Path(plan["target"])).resolve()
    state, backups = plan_storage(plan)
    if repository is not None:
        if state != Path(plan["target"]).resolve() or backups != Path(plan["target"]).resolve():
            raise ValueError("Replica legacy no admite plan con almacenamiento separado")
        state = backups = root
    check_image_root(root)
    if repository is None:
        check_backup_root(root, backups)
    database = state / ".catalog.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Catalogo no integro")
        receipts = {row[1]: row for row in connection.execute(
            "SELECT plan_id,source,checksum,backup,destination FROM migrations WHERE plan_id=?", (plan["id"],))}
        links = set(connection.execute("SELECT source,order_id,ean FROM order_eans"))
        records = list(connection.execute("""
            SELECT a.ean,a.checksum,r.provider,r.origin,r.view,r.market,r.name,r.date,r.metadata
            FROM assets a JOIN representations r ON a.id=r.asset_id
        """))
    for entry in plan["entries"]:
        receipt = receipts.get(entry["source"])
        if not receipt or receipt[2] != entry["checksum"]:
            raise ValueError(f"Entrada sin migrar: {entry['source']}")
        for base, relative in [(backups if external_backup(entry, plan) else state, receipt[3]), (root, receipt[4])]:
            if relative is None:
                continue
            path = (base / relative).resolve()
            if base not in path.parents or checksum(path) != entry["checksum"]:
                raise ValueError(f"Verificacion fallida: {path}")
        for order_id in entry["orders"]:
            if (entry["provider"], str(order_id), entry["ean"]) not in links:
                raise ValueError("Asociacion de pedido perdida")
        if entry["image"]:
            expected = (entry["ean"], entry["checksum"], entry["provider"], entry["origin"],
                        VIEW_ALIASES.get(entry["view"], entry["view"]), entry["market"], entry["name"])
            candidates = [row for row in records if row[:7] == expected]
            if not candidates or not any(json.loads(row[8]) == entry["metadata"] and
                                        datetime.fromisoformat(row[7]) == datetime.fromisoformat(entry["date"])
                                        for row in candidates):
                raise ValueError(f"Metadatos no verificados: {entry['source']}")
    for order in plan["orders"]:
        for line in order["snapshot"]["lines"]:
            if line["ean"] and (order["source"], order["order_id"], line["ean"]) not in links:
                raise ValueError("Asociacion historica de pedido perdida")
    return {"plan": plan["id"], "verified": len(plan["entries"]), "originals_deleted": False}


def recover(plan: dict, target: Path, repository: Path | None = None) -> dict:
    check_migration_target(target, "recovery")
    verify(plan, repository)
    root = (repository or Path(plan["target"])).resolve()
    state, backups = plan_storage(plan)
    if repository is not None:
        state = backups = root
    target = target.resolve()
    source_roots = {Path(entry["source_root"]) for entry in plan["entries"]}
    if any(target == base or base in target.parents or target in base.parents for base in {root, state, backups}) or any(
        target == source or source in target.parents or target in source.parents for source in source_roots
    ):
        raise ValueError("Recuperar en directorio separado, nunca sobre originales o repositorio")
    for entry in plan["entries"]:
        backup = (backups if external_backup(entry, plan) else state) / ".migration" / plan["id"] / "originals" / entry["backup"]
        copy_verified(backup, target / entry["backup"], entry["checksum"])
    return {"recovered": len(plan["entries"]), "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("inventory", help="Inventario y simulacion; no modifica origen/destino")
    scan.add_argument("--source", action="append", required=True, help="Proveedor=ruta; repetir por origen")
    scan.add_argument("--target", type=Path, default=None)
    scan.add_argument("--mapping", type=Path)
    scan.add_argument("--output", type=Path, required=True)
    for command in ("apply", "verify", "recover"):
        sub = commands.add_parser(command)
        sub.add_argument("--plan", type=Path, required=True)
        if command != "apply":
            sub.add_argument("--repository", type=Path, help="Verificar/recuperar desde una replica NAS")
        if command == "recover":
            sub.add_argument("--target", type=Path, required=True)
    sync = commands.add_parser("sync-nas")
    sync.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            sources = []
            for value in args.source:
                provider, separator, path = value.partition("=")
                if not separator or not provider or not path:
                    raise ValueError("--source requiere Proveedor=ruta")
                sources.append((provider, Path(path)))
            mapping = json.loads(args.mapping.read_text(encoding="utf-8")) if args.mapping else None
            plan = inventory(sources, args.target or repository_root(), mapping)
            output = args.output.resolve()
            if any(output == source.resolve() or source.resolve() in output.parents for _, source in sources):
                raise ValueError("Guardar el plan fuera de los origenes")
            atomic_write(output, json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"))
            result = {"plan": plan["id"], "files": len(plan["entries"]),
                      "conflicts": sum(entry["conflict"] for entry in plan["entries"]),
                      "invalid_images": sum(entry["valid"] is False for entry in plan["entries"]),
                      "blocker_count": len(plan["blockers"]),
                      "blockers": plan["blockers"][:10], "dry_run": True}
        elif args.command == "sync-nas":
            ImageRepository().sync_nas(args.target)
            result = {"replica_verified": str(args.target)}
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            result = (apply(plan) if args.command == "apply" else verify(plan, args.repository)
                      if args.command == "verify" else recover(plan, args.target, args.repository))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("blockers") else 0
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
