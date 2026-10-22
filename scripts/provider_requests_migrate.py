"""Explicit, opt-in migration of the provider request store to the shared UPC/EAN identity (#59).

Dry run by default: reports the schema state and live requests that collide (a UPC-12 and its EAN-13 form).
`--apply` makes a consistent backup first and then only adds a virtual column and a unique live index; nothing is
deleted or rewritten. It refuses to apply while conflicts exist: cancel one of the conflicting live requests
through the normal state machine and run it again. It is never invoked by the application or by deployment.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_images import migrate_request_store  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="directory holding provider_requests.sqlite")
    parser.add_argument("--apply", action="store_true", help="apply the migration (default: dry run)")
    args = parser.parse_args(argv)
    result = migrate_request_store(Path(args.root), apply=args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["conflicts"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
