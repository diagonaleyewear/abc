"""Regenerate the Biblioteca de imágenes thumbnails from the original photographs.

Only derived files in the local thumbnail cache are written. Originals are opened read-only and are
never modified. The cache is content-addressed (original checksum + profile), so the run is idempotent,
shares one file between records with the same original and can be resumed at any point.

    python scripts/regenerate_image_thumbnails.py report                    # read-only plan
    python scripts/regenerate_image_thumbnails.py regenerate --execute      # writes derived files
    python scripts/regenerate_image_thumbnails.py regenerate --execute --resume
    python scripts/regenerate_image_thumbnails.py purge-legacy --execute    # optional, see below
    python scripts/regenerate_image_thumbnails.py verify --per-provider 20  # read-only check

Without --execute every command is a dry run. `report` samples originals to estimate extra space and
time; it never writes to the cache. `purge-legacy` removes the old 360x240 entries of catalogued
originals, which other screens regenerate on demand with the legacy generator.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

import image_thumbnails as thumbnails  # noqa: E402
from image_repository import ImageRepository  # noqa: E402

PROFILE = {"dimensions": thumbnails.UI_DIMENSIONS, "version": thumbnails.UI_VERSION,
           "background": thumbnails.UI_BACKGROUND}


def _records(repository, provider: str | None):
    records = repository.catalog_records()
    if provider:
        records = [r for r in records if (r.provider or "").casefold() == provider.casefold()]
    return records


def _key(record) -> str:
    return thumbnails.cache_key(record, PROFILE["dimensions"], "JPEG", PROFILE["version"], PROFILE["background"])


def _classify(cache, records):
    """Split by original checksum so records sharing one original are counted once."""
    index = cache.snapshot()
    done, pending, seen = [], [], set()
    for record in records:
        key = _key(record)
        if key in seen:
            continue
        seen.add(key)
        entry = index.get(key)
        if entry and (cache.root / f"{key}.jpg").is_file() and (cache.root / f"{key}.jpg").stat().st_size == entry["size"]:
            done.append(record)
        else:
            pending.append(record)
    return done, pending


def report(repository, cache, args) -> int:
    records = _records(repository, args.provider)
    done, pending = _classify(cache, records)
    sample = pending[:: max(1, len(pending) // args.sample)][: args.sample] if pending else []
    sizes, seconds, failed = [], [], 0
    for record in sample:
        started = time.perf_counter()
        try:
            content = repository.verified_bytes(record)
            sizes.append(len(thumbnails.generate(content, PROFILE["dimensions"], "JPEG", PROFILE["background"])))
            seconds.append(time.perf_counter() - started)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            failed += 1
    average_size = sum(sizes) / len(sizes) if sizes else 0
    average_time = sum(seconds) / len(seconds) if seconds else 0
    free = __import__("shutil").disk_usage(cache.root if cache.root.exists() else cache.root.parent).free
    extra = int(average_size * len(pending))
    result = {
        "catalog_records": len(records), "distinct_originals": len(done) + len(pending),
        "already_regenerated": len(done), "to_regenerate": len(pending),
        "sampled": len(sizes), "sample_failures": failed,
        "average_thumbnail_bytes": int(average_size), "extra_bytes_estimate": extra,
        "seconds_per_thumbnail_sample": round(average_time, 3),
        "minutes_estimate_excluding_index_writes": round(average_time * len(pending) / 60, 1),
        "cache_capacity_bytes": cache.capacity,
        "cache_used_bytes": sum(e["size"] for e in cache.snapshot().values()),
        "free_bytes": free, "reserve_bytes": cache.reserve,
    }
    result["fits_capacity"] = result["cache_used_bytes"] + extra <= cache.capacity
    result["fits_free_space"] = free >= cache.reserve + extra
    print(json.dumps(result, indent=2))
    return 0


def _log(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def regenerate(repository, cache, args) -> int:
    if not args.execute:
        return report(repository, cache, args)
    records = _records(repository, args.provider)
    state_path = Path(args.state) if args.state else cache.root / "regeneration-state.json"
    error_log = Path(args.error_log) if args.error_log else cache.root / "regeneration-errors.jsonl"
    last_id = 0
    if args.resume and state_path.is_file():
        last_id = json.loads(state_path.read_text(encoding="utf-8")).get("last_id", 0)
    _, pending = _classify(cache, records)
    pending = [r for r in pending if r.id > last_id]
    if args.limit:
        pending = pending[: args.limit]
    cache.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = errors = 0
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start:start + args.batch_size]
        proofs = thumbnails.validate_visible(repository, batch)
        with cache.batch(batch):
            for record in batch:
                try:
                    cache.get(record, proofs[record.id], dimensions=PROFILE["dimensions"],
                              version=PROFILE["version"], background=PROFILE["background"])
                    created += 1
                except (OSError, ValueError, RuntimeError, SyntaxError, Image.DecompressionBombError) as error:
                    errors += 1
                    _log(error_log, {"id": record.id, "ean": record.ean, "provider": record.provider,
                                     "error": str(error)})
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last_id": batch[-1].id}), encoding="utf-8")
        os.replace(temporary, state_path)
        print(f"{min(start + len(batch), len(pending))}/{len(pending)} generated={created} errors={errors}",
              flush=True)
    print(json.dumps({"generated": created, "errors": errors, "error_log": str(error_log)}))
    return 1 if errors else 0


def purge_legacy(repository, cache, args) -> int:
    records = _records(repository, args.provider)
    legacy = {thumbnails.cache_key(r) for r in records}
    present = legacy & cache.snapshot().keys()
    size = sum(cache.snapshot()[key]["size"] for key in present)
    print(json.dumps({"legacy_entries": len(present), "bytes": size, "executed": args.execute}))
    if args.execute and present:
        print(json.dumps({"freed_bytes": cache.purge(present)}))
    return 0


def verify(repository, cache, args) -> int:
    records = _records(repository, args.provider)
    by_provider: dict[str, list] = {}
    for record in records:
        by_provider.setdefault(record.provider or "Sin proveedor", []).append(record)
    failures = checked = 0
    for provider, items in sorted(by_provider.items()):
        for record in items[:: max(1, len(items) // args.per_provider)][: args.per_provider]:
            checked += 1
            before = record.path.stat()
            problem = ""
            path = cache.root / f"{_key(record)}.jpg"
            try:
                content = path.read_bytes()
                with Image.open(BytesIO(content)) as thumb, Image.open(record.path) as original:
                    width, height = thumb.size
                    if width > PROFILE["dimensions"][0] or height > PROFILE["dimensions"][1]:
                        problem = f"excede {PROFILE['dimensions']}: {thumb.size}"
                    elif abs(width / height - original.width / original.height) > 0.02:
                        problem = f"proporción alterada {thumb.size} vs {original.size}"
                    elif thumb.format != "JPEG":
                        problem = f"formato {thumb.format}"
            except (OSError, SyntaxError) as error:
                problem = str(error)
            after = record.path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                problem = "ORIGINAL MODIFICADO"
            if problem:
                failures += 1
                print(f"FALLO {provider} id={record.id} ean={record.ean}: {problem}")
    print(json.dumps({"checked": checked, "failures": failures, "providers": sorted(by_provider)}))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("report", "regenerate", "purge-legacy", "verify"))
    parser.add_argument("--execute", action="store_true", help="write derived files (default: dry run)")
    parser.add_argument("--provider", help="limit to one provider, e.g. Kering")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=0, help="maximum thumbnails in this run")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--state", help="resume state file (default: inside the cache root)")
    parser.add_argument("--error-log", help="JSONL error log (default: inside the cache root)")
    parser.add_argument("--sample", type=int, default=30, help="originals sampled by the report")
    parser.add_argument("--per-provider", type=int, default=20, help="records verified per provider")
    args = parser.parse_args(argv)
    os.umask(0o077)
    repository = ImageRepository()
    cache = thumbnails.ThumbnailCache(repository)
    return {"report": report, "regenerate": regenerate, "purge-legacy": purge_legacy,
            "verify": verify}[args.command](repository, cache, args)


if __name__ == "__main__":
    raise SystemExit(main())
