#!/usr/bin/env python3
"""One-time, verified installation of the automated Cloud release pipeline on s2026.

Run by an administrator as root from a root-only copy of a merged `main` revision:

    python3 -I pipeline_install.py install --commit <40-hex main commit> --source <dir>
    python3 -I pipeline_install.py rollback --backup /etc/cloud-production/migration-backup-<UTC>

It only copies three files, never restarts a service or the runner, never reads or
writes secrets, and never touches the previous per-release policy or Compose files.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import re
import sys
import tempfile
import time
import urllib.request

REPO = "sginfoocio/abc"
WORKFLOW = ".github/workflows/deploy-cloud-production.yml"
# repository path -> (installed path, mode)
FILES = {
    "deployment/cloud/cloud_production_deploy.py": ("/usr/local/sbin/cloud-production-deploy", 0o755),
    "deployment/cloud/runner_job_gate.py": ("/usr/local/libexec/cloud-runner-job-gate.py", 0o644),
    "deployment/cloud/release-authority.json": ("/etc/cloud-production/release-authority.json", 0o600),
}
# Preserved, never modified; recorded in the backup for recovery and audit.
PRESERVED = ("/etc/cloud-production/approved-release.json",)
MAX_BYTES = 4 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def github_api(resource):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{resource}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "cloud-pipeline-install",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise InstallError("GitHub response is unexpectedly large")
    return json.loads(content)


def git_blob_sha(content):
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def target(prefix, path):
    return Path(str(prefix) + path) if str(prefix) != "/" else Path(path)


def check_source_file(path):
    info = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"{path} is not a regular file")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
        raise InstallError(f"{path} must be root-owned and not group/other writable")


def verify_origin(source, commit, fetch):
    """Every installed file must be byte-identical to the file GitHub holds for a main commit."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise InstallError("--commit must be a full 40-character SHA")
    if fetch(f"compare/{commit}...main").get("status") not in {"ahead", "identical"}:
        raise InstallError("Commit is not contained in main")
    contents = {}
    for name in FILES:
        path = Path(source) / Path(name).name
        check_source_file(path)
        content = path.read_bytes()
        remote = fetch(f"contents/{name}?ref={commit}")
        if remote.get("sha") != git_blob_sha(content):
            raise InstallError(f"{name} does not match GitHub at {commit}")
        contents[name] = content
    return contents


def validate_contents(contents, commit, fetch):
    authority = json.loads(contents["deployment/cloud/release-authority.json"])
    controller = "deployment/cloud/cloud_production_deploy.py"
    with tempfile.TemporaryDirectory() as scratch:
        try:
            _compile_and_check(contents, controller, authority, scratch)
        except py_compile.PyCompileError as error:
            raise InstallError(f"Candidate does not compile: {error.msg.strip()}") from error
    if fetch(f"contents/{WORKFLOW}?ref={commit}").get("sha") != authority["workflow_blob_sha"]:
        raise InstallError("Authority does not pin the production workflow of this commit")


def _compile_and_check(contents, controller, authority, scratch):
    candidate = Path(scratch) / "controller.py"
    candidate.write_bytes(contents[controller])
    py_compile.compile(str(candidate), cfile=str(Path(scratch) / "controller.pyc"), doraise=True)
    gate = Path(scratch) / "gate.py"
    gate.write_bytes(contents["deployment/cloud/runner_job_gate.py"])
    py_compile.compile(str(gate), cfile=str(Path(scratch) / "gate.pyc"), doraise=True)
    spec = importlib.util.spec_from_file_location("candidate_controller", candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_authority(authority)


def write_atomic(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".install-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(name, 0, 0)
        os.replace(name, path)
    except BaseException:
        if os.path.exists(name):
            os.unlink(name)
        raise


def install(source, commit, prefix="/", fetch=github_api, now=None):
    contents = verify_origin(source, commit, fetch)
    validate_contents(contents, commit, fetch)
    stamp = now or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = target(prefix, f"/etc/cloud-production/migration-backup-{stamp}")
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"commit": commit, "files": {}, "preserved": {}}
    for name, (installed, _mode) in FILES.items():
        destination = target(prefix, installed)
        if destination.exists():
            data = destination.read_bytes()
            (backup / destination.name).write_bytes(data)
            manifest["files"][installed] = {"existed": True, "sha256": sha256(data), "backup": destination.name}
        else:
            manifest["files"][installed] = {"existed": False}
    for path in PRESERVED:
        existing = target(prefix, path)
        if existing.exists():
            data = existing.read_bytes()
            (backup / existing.name).write_bytes(data)
            manifest["preserved"][path] = sha256(data)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.chmod(backup / "manifest.json", 0o600)
    for name, (installed, mode) in FILES.items():
        write_atomic(target(prefix, installed), contents[name], mode)
    return backup, {name: sha256(content) for name, content in contents.items()}


def rollback(backup, prefix="/"):
    """Restore the pre-migration controller, gate and authority from a verified backup."""
    backup = Path(backup)
    manifest = json.loads((backup / "manifest.json").read_text())
    restores = []
    for installed, info in manifest["files"].items():
        if info["existed"]:
            data = (backup / info["backup"]).read_bytes()
            if sha256(data) != info["sha256"]:
                raise InstallError(f"Backup of {installed} is corrupt")
            restores.append((installed, data))
    for path, digest in manifest["preserved"].items():
        existing = target(prefix, path)
        if not existing.exists() or sha256(existing.read_bytes()) != digest:
            raise InstallError(f"{path} changed since the migration; refusing to guess")
    for installed, data in restores:
        write_atomic(target(prefix, installed), data, dict((v[0], v[1]) for v in FILES.values())[installed])
    for installed, info in manifest["files"].items():
        if not info["existed"]:
            target(prefix, installed).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("install")
    add.add_argument("--commit", required=True)
    add.add_argument("--source", required=True)
    undo = commands.add_parser("rollback")
    undo.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            backup, digests = install(args.source, args.commit)
            print(f"installed; backup: {backup}")
            for name, digest in sorted(digests.items()):
                print(f"  {digest}  {name}")
        else:
            rollback(args.backup)
            print("restored previous controller, gate and authority")
    except (InstallError, OSError, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
