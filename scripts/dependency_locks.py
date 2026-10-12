from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def compile_lock(source: str, target: Path, constraints: tuple[str, ...]) -> None:
    arguments = ["pip", "compile", source]
    for constraint in constraints:
        arguments.extend(["--constraint", constraint])
    arguments.extend(["--no-annotate", "--python-version", "3.11",
                      "--python-platform", "x86_64-unknown-linux-gnu"])
    command = "uv " + " ".join(arguments)
    subprocess.run([sys.executable, "-m", "uv", *arguments,
                    "--custom-compile-command", command, "--quiet", "-o", str(target)],
                   cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Regenerate locks without upgrading pinned dependencies")
    write = parser.parse_args().write
    valid = True
    with tempfile.TemporaryDirectory() as temporary:
        for source, constraints in (
            ("requirements.in", ("requirements.txt",)),
            ("requirements-dev.in", ("requirements-dev.txt", "requirements.txt")),
        ):
            filename = source.replace(".in", ".txt")
            committed = ROOT / filename
            generated = committed if write else Path(temporary) / filename
            compile_lock(source, generated, constraints)
            if not write and committed.read_bytes() != generated.read_bytes():
                valid = False
                sys.stdout.writelines(difflib.unified_diff(
                    committed.read_text(encoding="utf-8").splitlines(keepends=True),
                    generated.read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=filename, tofile=f"generated/{filename}",
                ))
    if valid:
        print("Dependency locks regenerated" if write else "Dependency locks verified")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())