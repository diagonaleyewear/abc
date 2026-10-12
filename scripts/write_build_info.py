"""Generate immutable provenance from the build inputs, without secrets."""
from datetime import datetime
import json
import os
from pathlib import Path
import re


def write_build_info(destination: Path) -> dict:
    commit = os.environ["BUILD_COMMIT"]
    published = os.environ["BUILD_PUBLISHED"]
    build = os.environ["BUILD_ID"]
    version = os.environ.get("BUILD_VERSION") or f"sha-{commit[:12]}"
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("BUILD_COMMIT must identify the exact source revision")
    if datetime.fromisoformat(published.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("BUILD_PUBLISHED requires a timezone")
    if not build:
        raise ValueError("BUILD_ID is required")
    info = {"version": version, "commit": commit, "published": published, "build": build}
    destination.write_text(json.dumps(info), encoding="utf-8")
    return info


if __name__ == "__main__":
    write_build_info(Path("build-info.json"))
