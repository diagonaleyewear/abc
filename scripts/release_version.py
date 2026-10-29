"""Resolve the semantic version of a build from the VERSION file; never invents one.

CI usage: RELEASE_COMMIT=<sha> [RELEASE_TAG_COMMIT=<sha of an existing vX.Y.Z tag>] python scripts/release_version.py
"""
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def read_version(path: Path = ROOT / "VERSION") -> str:
    version = path.read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        raise ValueError("VERSION must be MAJOR.MINOR.PATCH without prefix or suffix")
    return version


def resolve_version(commit: str, path: Path = ROOT / "VERSION") -> str:
    """Builds between releases legitimately repeat VERSION; the commit and digest tell them apart."""
    if not SHA.fullmatch(commit):
        raise ValueError("A full 40-character commit is required")
    return read_version(path)


def check_release(commit: str, tag_commit: str = "", path: Path = ROOT / "VERSION") -> str:
    """A number is releasable once: its tag may not exist yet (or must already be on this very commit)."""
    version = resolve_version(commit, path)
    if tag_commit and tag_commit != commit:
        raise ValueError(f"v{version} is already tagged on another commit; increase VERSION before releasing")
    return version


if __name__ == "__main__":
    try:
        if "--check-release" in sys.argv[1:]:
            print(check_release(os.environ["RELEASE_COMMIT"], os.environ.get("RELEASE_TAG_COMMIT", "")))
        else:
            print(resolve_version(os.environ["RELEASE_COMMIT"]))
    except (KeyError, ValueError, OSError) as error:
        sys.exit(f"release_version: {error}")
