"""Offline review of prior plans/manifests; no provider, SQLite or image writes."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from graph_mail_downloader import _normalize_image_key, _image_model_color_key


def build_review(plan: dict, manifests: list[Path]) -> dict:
    matches: dict[tuple[str, str], dict[str, set[str]]] = {}
    for path in manifests:
        for product in json.loads(path.read_text(encoding="utf-8")).get("products", []):
            key = (_normalize_image_key(product.get("modelo", ""), remove_initial_zero=True),
                   _normalize_image_key(product.get("color", "")))
            ean = str(product.get("ean", "")).strip()
            if all(key) and ean:
                matches.setdefault(key, {}).setdefault(ean, set()).add(str(path))
    ambiguous = []
    for key, candidates in sorted(matches.items()):
        if len(candidates) > 1:
            affected = [{field: entry.get(field) for field in ("source", "name", "ean", "checksum")}
                        for entry in plan["entries"] if entry["image"] and
                        _image_model_color_key(entry["name"]) == key]
            ambiguous.append({
                "id": f"MAP-{len(ambiguous) + 1:04d}", "model_color": list(key),
                "candidates": {ean: sorted(paths) for ean, paths in sorted(candidates.items())},
                "prior_matching_images": affected,
                "treatment": "Preserve all candidates; request authoritative model/color/EAN evidence. "
                             "No automatic choice. Block affected unresolved images.",
            })
    unresolved, invalid, unknown = [], [], []
    for entry in plan["entries"]:
        if not entry["image"]:
            continue
        details = {key: entry.get(key) for key in ("source", "provider", "name", "checksum", "size", "ean")}
        if not entry["ean"]:
            unresolved.append({**details, "id": f"EAN-{len(unresolved) + 1:04d}",
                               "treatment": "Retain original and archive association; obtain verified mapping "
                                            "from provider manifest/product. Do not invent EAN; apply blocked."})
        if entry["valid"] is False:
            invalid.append({**details, "id": f"FILE-{len(invalid) + 1:04d}",
                            "treatment": "Retain bytes/checksum; diagnose decoding/format/size/limits offline. "
                                         "Reacquire verified original if authorized; retain both versions. "
                                         "Do not repair/rename original silently; apply blocked."})
        if entry.get("view") == "unknown" and entry["valid"] is True:
            unknown.append({**details, "id": f"VIEW-{len(unknown) + 1:04d}",
                            "treatment": "Valid unknown images remain exportable; canonical views pending. "
                                         "Require verified image signals; never infer by count/position."})
    summary = {"plan_id": plan["id"], "prior_inventory_not_fresh_scan": True,
               "manifest_files_read_now": len(manifests), "ambiguous_manifest_keys_now": len(ambiguous),
               "unresolved_prior_images": len(unresolved), "nonreusable_prior_images": len(invalid),
               "unknown_prior_views": len(unknown), "prior_blockers": len(plan["blockers"]),
               "unknown_by_provider": dict(Counter(row["provider"] for row in unknown))}
    summary["prior_images_matching_ambiguous_keys"] = sum(
        len(row["prior_matching_images"]) for row in ambiguous)
    return {"summary": summary, "ambiguous_mappings": ambiguous, "unresolved_images": unresolved,
            "nonreusable_images": invalid, "unknown_views": unknown,
            "original_blockers": plan["blockers"],
            "limits": "Current manifests are not a frozen source; prior file validity is not revalidated."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    body = {key: value for key, value in plan.items() if key != "id"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != plan["id"]:
        parser.error("Prior plan signature mismatch; do not trust report")
    manifests = sorted({path for root in args.manifests
                        for path in root.glob("upc-products-images-request-*.manifest.json")})
    report = build_review(plan, manifests)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
