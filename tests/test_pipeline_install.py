import importlib.util
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "deployment/cloud"
SPEC = importlib.util.spec_from_file_location("pipeline_install", CLOUD / "pipeline_install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)
COMMIT = "c" * 40
AUTHORITY = json.loads((CLOUD / "release-authority.json").read_text())


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    for name in INSTALL.FILES:
        shutil.copy(ROOT / name, directory / Path(name).name)
    return directory


def make_fetch(source, *, compare="ahead", workflow_blob=None, tamper=None):
    def fetch(resource):
        if resource == f"compare/{COMMIT}...main":
            return {"status": compare}
        if resource.startswith(f"contents/{INSTALL.WORKFLOW}"):
            return {"sha": workflow_blob or AUTHORITY["workflow_blob_sha"]}
        for name in INSTALL.FILES:
            if resource == f"contents/{name}?ref={COMMIT}":
                content = (source / Path(name).name).read_bytes()
                return {"sha": INSTALL.git_blob_sha(b"tampered" if tamper == name else content)}
        raise AssertionError(resource)
    return fetch


@pytest.fixture
def prefix(tmp_path):
    root = tmp_path / "root"
    (root / "etc/cloud-production").mkdir(parents=True)
    (root / "usr/local/sbin").mkdir(parents=True)
    (root / "usr/local/libexec").mkdir(parents=True)
    (root / "etc/cloud-production/approved-release.json").write_text('{"legacy": true}')
    (root / "usr/local/sbin/cloud-production-deploy").write_text("old controller")
    (root / "usr/local/libexec/cloud-runner-job-gate.py").write_text("old gate")
    return root


def installed(prefix, path):
    return Path(str(prefix) + path)


def test_install_copies_verified_files_and_preserves_legacy_policy(source, prefix):
    backup, digests = INSTALL.install(source, COMMIT, prefix, make_fetch(source), now="20260101T000000Z")
    for name, (path, _mode) in INSTALL.FILES.items():
        assert installed(prefix, path).read_bytes() == (ROOT / name).read_bytes()
        assert digests[name] == INSTALL.sha256((ROOT / name).read_bytes())
    assert installed(prefix, "/etc/cloud-production/approved-release.json").read_text() == '{"legacy": true}'
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["commit"] == COMMIT
    assert (backup / "cloud-production-deploy").read_text() == "old controller"
    assert (backup / "approved-release.json").read_text() == '{"legacy": true}'
    assert manifest["files"]["/etc/cloud-production/release-authority.json"] == {"existed": False}


@pytest.mark.parametrize("kwargs", [
    {"compare": "behind"}, {"compare": "diverged"},
    {"workflow_blob": "f" * 40},
    {"tamper": "deployment/cloud/cloud_production_deploy.py"},
    {"tamper": "deployment/cloud/release-authority.json"},
])
def test_install_refuses_unverified_origin_and_changes_nothing(source, prefix, kwargs):
    with pytest.raises(INSTALL.InstallError):
        INSTALL.install(source, COMMIT, prefix, make_fetch(source, **kwargs))
    assert installed(prefix, "/usr/local/sbin/cloud-production-deploy").read_text() == "old controller"
    assert not installed(prefix, "/etc/cloud-production/release-authority.json").exists()
    assert not list((prefix / "etc/cloud-production").glob("migration-backup-*"))


def test_install_rejects_locally_modified_source_and_bad_commit(source, prefix):
    (source / "release-authority.json").write_text(
        (source / "release-authority.json").read_text().replace("sginfoocio/abc", "attacker/abc", 1)
    )
    with pytest.raises(INSTALL.InstallError):
        INSTALL.install(source, COMMIT, prefix, make_fetch(source, tamper="deployment/cloud/release-authority.json"))
    with pytest.raises(INSTALL.InstallError):
        INSTALL.install(source, "main", prefix, make_fetch(source))


def test_install_rejects_uncompilable_controller(source, prefix):
    (source / "cloud_production_deploy.py").write_text("def broken(:\n")
    with pytest.raises(INSTALL.InstallError):
        INSTALL.install(source, COMMIT, prefix, make_fetch(source))
    assert installed(prefix, "/usr/local/sbin/cloud-production-deploy").read_text() == "old controller"


def test_rollback_restores_previous_files_and_removes_new_authority(source, prefix):
    backup, _ = INSTALL.install(source, COMMIT, prefix, make_fetch(source), now="20260101T000000Z")
    INSTALL.rollback(backup, prefix)
    assert installed(prefix, "/usr/local/sbin/cloud-production-deploy").read_text() == "old controller"
    assert installed(prefix, "/usr/local/libexec/cloud-runner-job-gate.py").read_text() == "old gate"
    assert not installed(prefix, "/etc/cloud-production/release-authority.json").exists()
    assert installed(prefix, "/etc/cloud-production/approved-release.json").read_text() == '{"legacy": true}'


def test_rollback_refuses_corrupt_backup_or_changed_legacy_policy(source, prefix):
    backup, _ = INSTALL.install(source, COMMIT, prefix, make_fetch(source), now="20260101T000000Z")
    (backup / "cloud-production-deploy").write_text("corrupted")
    with pytest.raises(INSTALL.InstallError):
        INSTALL.rollback(backup, prefix)
    (backup / "cloud-production-deploy").write_text("old controller")
    installed(prefix, "/etc/cloud-production/approved-release.json").write_text('{"legacy": false}')
    with pytest.raises(INSTALL.InstallError):
        INSTALL.rollback(backup, prefix)
    assert installed(prefix, "/etc/cloud-production/release-authority.json").exists()


def test_installer_never_touches_services_or_secrets():
    text = (CLOUD / "pipeline_install.py").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", '"')))
    for forbidden in ("subprocess", "docker", "systemctl", "compose.private", "sudoers", "os.system"):
        assert forbidden not in code
