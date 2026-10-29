import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest

import build_info
from scripts.release_version import check_release, read_version, resolve_version
from scripts.write_build_info import write_build_info

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)")
NEW, OLD = "a" * 40, "b" * 40


def history():
    return json.loads((ROOT / "release-history.json").read_text(encoding="utf-8"))["releases"]


def load_deploy():
    spec = importlib.util.spec_from_file_location("deploy_ctl", ROOT / "deployment/cloud/cloud_production_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(tmp_path, monkeypatch, commit, version):
    for key, value in {"BUILD_COMMIT": commit, "BUILD_VERSION": version, "BUILD_ID": "actions-7-1",
                       "BUILD_PUBLISHED": "2026-10-29T20:00:00Z"}.items():
        monkeypatch.setenv(key, value)
    destination = tmp_path / f"{commit[:4]}.json"
    write_build_info(destination)
    monkeypatch.setattr(build_info, "BUILD_FILE", destination)
    return build_info.load_build_info()


def test_version_file_is_real_semver_and_initial_is_not_assumed_1_0_0():
    assert SEMVER.fullmatch(read_version())
    assert read_version() == "0.1.0"


@pytest.mark.parametrize("bad", ["v1.0.0", "1.0", "01.0.0", "1.0.0-rc1", "1.0.0+x", ""])
def test_invalid_versions_are_rejected(tmp_path, bad):
    path = tmp_path / "VERSION"
    path.write_text(bad)
    with pytest.raises(ValueError):
        resolve_version(NEW, path=path)


def test_release_number_is_unique_but_ordinary_builds_are_not_blocked():
    assert resolve_version(NEW) == read_version()
    assert check_release(NEW) == read_version()
    assert check_release(NEW, tag_commit=NEW) == read_version()
    with pytest.raises(ValueError, match="increase VERSION"):
        check_release(NEW, tag_commit=OLD)
    with pytest.raises(ValueError):
        resolve_version("abc123")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_tag_detection_works_from_a_shallow_checkout_without_fetching_tags(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    for number in (1, 2):
        (origin / "f").write_text(str(number))
        git(origin, "add", "f")
        git(origin, "commit", "-q", "-m", str(number))
        if number == 1:
            git(origin, "tag", "-a", "-m", "release", "v0.1.0")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(clone)], check=True)
    assert "v0.1.0" not in git(clone, "tag")
    listing = git(clone, "ls-remote", "--tags", "origin", "refs/tags/v0.1.0", "refs/tags/v0.1.0^{}")
    peeled = [line.split()[0] for line in listing.splitlines() if line.endswith("^{}")]
    assert peeled == [git(origin, "rev-parse", "v0.1.0^{commit}").strip()]
    assert peeled[0] != git(clone, "rev-parse", "HEAD").strip()
    assert git(clone, "ls-remote", "--tags", "origin", "refs/tags/v9.9.9") == ""
    assert check_release(git(clone, "rev-parse", "HEAD").strip()) == read_version()
    with pytest.raises(ValueError):
        check_release(git(clone, "rev-parse", "HEAD").strip(), peeled[0])


def test_version_is_identical_in_version_file_build_info_oci_label_and_footer():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "BUILD_VERSION=${{ steps.provenance.outputs.version }}" in workflow
    assert "org.opencontainers.image.version=${{ steps.provenance.outputs.version }}" in workflow
    assert 'test "$EXPECTED_VERSION" = "$(tr -d \'[:space:]\' < VERSION)"' in workflow
    assert "assert info['version'] == os.environ['EXPECTED_VERSION']" in workflow
    assert "footer_text(info) ==" in workflow
    assert "sha-${COMMIT" not in workflow
    assert 'ARG BUILD_VERSION' in (ROOT / "Dockerfile").read_text()


def test_image_metadata_ties_version_sha_and_date(tmp_path, monkeypatch):
    info = build(tmp_path, monkeypatch, NEW, "0.1.0")
    assert info == {"version": "0.1.0", "commit": NEW, "published": "2026-10-29T20:00:00Z", "build": "actions-7-1"}
    assert build_info.footer_text(info) == "Cloud v0.1.0 · commit aaaaaaa"


def test_non_semver_build_version_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        build(tmp_path, monkeypatch, NEW, "v1.0.0")


def test_missing_data_is_not_invented():
    local = {"version": "local-" + NEW[:12], "commit": NEW, "published": "", "build": "Código local; no artefacto desplegado"}
    assert build_info.footer_text(local) == "Cloud · versión No disponible · commit aaaaaaa"
    details = build_info.version_details(local)
    assert details["Versión instalada"] == details["Fecha de publicación (UTC)"] == "No disponible"
    assert details["Identificador del release"] == details["Estado del despliegue"] == "No disponible"
    unknown = {"version": "build sin identificar", "commit": "", "published": "", "build": "Sin información"}
    assert build_info.footer_text(unknown) == "Cloud · versión No disponible · commit No disponible"


def test_installed_release_details_expose_no_deployment_claim_beyond_evidence(tmp_path, monkeypatch):
    details = build_info.version_details(build(tmp_path, monkeypatch, NEW, "0.1.0"))
    assert details["Identificador del release"] == "actions-7-1"
    assert "no se consulta" in details["Estado del despliegue"]
    assert build_info.CHANGELOG_URL.endswith("/CHANGELOG.md")


def test_rollback_is_identified_by_the_running_image_and_controller_checks(tmp_path, monkeypatch):
    new = build(tmp_path, monkeypatch, NEW, "0.2.0")
    old = build(tmp_path, monkeypatch, OLD, "0.1.0")
    assert build_info.footer_text(old) == "Cloud v0.1.0 · commit bbbbbbb" != build_info.footer_text(new)
    deploy = load_deploy()
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: b"")
    monkeypatch.setattr(deploy, "inspect_container", lambda name: {
        "Config": {"Labels": {"org.opencontainers.image.revision": OLD}}, "Image": "old-id",
        "State": {"Running": True, "Health": {"Status": "healthy"}}})
    deploy.check_health(old["commit"], "old-id")
    with pytest.raises(deploy.DeployError):
        deploy.check_health(new["commit"], "old-id")


def test_release_history_has_no_invented_or_unproven_releases():
    releases = history()
    versions = [item["version"] for item in releases]
    assert len(versions) == len(set(versions))
    for item in releases:
        assert SEMVER.fullmatch(item["version"])
        assert item["status"] in {"prepared", "published", "failed", "rolled_back"}
        if item["status"] in {"published", "failed", "rolled_back"}:
            assert re.fullmatch(r"[0-9a-f]{40}", item["commit"])
        if item["status"] == "published":
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"])
            assert item["run_id"] and item["published"]
        if item["status"] == "rolled_back":
            assert re.fullmatch(r"[0-9a-f]{40}", item["rolled_back_to"])
    assert read_version() in versions
    assert next(i for i in releases if i["version"] == read_version())["status"] == "prepared"


def test_changelog_matches_version_and_does_not_claim_marcolin_deployed():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(read_version())}\]", text, re.M)
    assert "Exportación Excel de solicitudes Marcolin" in text and "PR #81" in text
    assert "no se marca como desplegado" in text


def test_workflow_keeps_immutable_digest_and_tags_never_deploy():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    on_block = workflow.split("\njobs:")[0]
    assert "tags:" not in on_block
    assert "release_version.py" in workflow
    assert "BUILD_VERSION=${{ steps.provenance.outputs.version }}" in workflow
    assert '@${{ steps.image.outputs.digest }}"' in workflow
    assert "ghcr[.]io/sginfoocio/abc@sha256" in (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text()
    deploy = load_deploy()
    assert all(f"- name: {step}" in workflow for step in deploy.BUILD_STEPS)
    assert "cloud-image-identity::${REGISTRY}/${IMAGE_NAME}@${PUBLISHED_DIGEST} ${PUBLISHED_COMMIT} actions-" in workflow
    assert ":latest" not in (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text()
