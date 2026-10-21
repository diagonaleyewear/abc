import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import textwrap
from threading import Thread
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cloud_deploy", ROOT / "deployment/cloud/cloud_production_deploy.py",
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


RELEASE = {
    "repository": "sginfoocio/abc",
    "commit": "c" * 40,
    "controller_commit": "a" * 40,
    "image": "ghcr.io/sginfoocio/abc@sha256:" + "d" * 64,
    "previous_commit": "b" * 40,
    "release_status": "approved",
    "build_run_id": 39636988747,
    "build_attempt": 1,
    "runner_name": "cloud-production-s2026",
    "reviewers": ["sginfoocio"],
    "source_sha256": {
        "app_enhanced.py": "1" * 64,
        "app_exports.py": "2" * 64,
        "transform_luxottica_masterdata.py": "3" * 64,
    },
}


@pytest.fixture
def policy():
    return copy.deepcopy(RELEASE)


@pytest.fixture
def config():
    return {
        "name": "cloud-nas",
        "services": {
            "abcd-app": {
                "container_name": "abcd-control", "image": "previous:retained", "user": "1037:100",
                "environment": {"SECRET": "not-for-logs"},
                "volumes": [{"type": "bind", "source": "/mnt/cloud-imagenes", "target": "/nas"}],
            },
            "abcd-order-alerts": {"image": "previous:retained", "environment": {"KEEP": "exact"}},
        },
    }


def test_policy_requires_administratively_pinned_controller(policy):
    DEPLOY.validate_policy(policy)
    del policy["controller_commit"]
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_policy(policy)


@pytest.mark.parametrize("uid,mode", [
    (1000, stat.S_IFREG | 0o600),
    (0, stat.S_IFREG | 0o620),
    (0, stat.S_IFREG | 0o602),
    (0, stat.S_IFLNK | 0o777),
])
def test_root_paths_reject_nonroot_owner_writable_files_and_symlinks(monkeypatch, uid, mode):
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_uid=uid, st_mode=mode))
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.secure_path(Path("approved-policy"))


@pytest.mark.parametrize("key,value", [
    ("image", "ghcr.io/attacker/image:latest"),
    ("image", "ghcr.io/sginfoocio/abc:main"),
    ("commit", "main; arbitrary"),
    ("repository", "another/repository"),
    ("runner_name", "s2026"),
    ("build_run_id", True),
    ("source_sha256", {"unexpected.py": "a" * 64}),
    ("release_status", "baseline-only"),
])
def test_policy_rejects_untrusted_inputs(policy, key, value):
    policy[key] = value
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_policy(policy)


AUTHORITY = json.loads((ROOT / "deployment/cloud/release-authority.json").read_text())
COMMIT = "c" * 40
IMAGE = "ghcr.io/sginfoocio/abc@sha256:" + "d" * 64


def test_checked_in_authority_is_static_and_names_no_release():
    DEPLOY.validate_authority(AUTHORITY)
    assert not {"commit", "previous_commit", "image", "build_run_id", "source_sha256"} & set(AUTHORITY)
    workflow_blob = subprocess.check_output(
        ["git", "hash-object", "--path", ".github/workflows/deploy-cloud-production.yml", "--stdin"],
        input=(ROOT / ".github/workflows/deploy-cloud-production.yml").read_bytes().replace(b"\r\n", b"\n"),
        cwd=ROOT,
    ).decode().strip()
    assert AUTHORITY["workflow_blob_sha"] == workflow_blob


@pytest.mark.parametrize("key,value", [
    ("repository", "another/repository"), ("runner_name", "s2026"),
    ("image_repository", "ghcr.io/attacker/abc"), ("workflow_blob_sha", "main"),
    ("reviewers", []), ("source_files", ["app_enhanced.py"]),
    ("source_files", ["app_enhanced.py", "app_enhanced.py", "app_exports.py"]),
    ("authority_version", 2),
])
def test_authority_rejects_untrusted_values(key, value):
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_authority({**AUTHORITY, key: value})


BUILD_RUN = 39700000000


def publication(**changes):
    """GitHub facts describing one genuine successful main publication of COMMIT."""
    repo = {"full_name": "sginfoocio/abc"}
    run = {
        "id": BUILD_RUN, "run_attempt": 1, "path": ".github/workflows/deploy.yml", "event": "push",
        "head_branch": "main", "head_sha": COMMIT, "conclusion": "success",
        "repository": repo, "head_repository": repo,
    }
    job = {
        "id": 555, "run_id": BUILD_RUN, "name": "build-and-push", "conclusion": "success",
        "steps": [{"name": name, "conclusion": "success"} for name in sorted(DEPLOY.BUILD_STEPS)],
    }
    note = {
        "title": "cloud-image-identity", "annotation_level": "notice",
        "message": f"{IMAGE} {COMMIT} actions-{BUILD_RUN}-1",
    }
    facts = {"runs": [run], "runs_total": None, "jobs": [job], "jobs_total": None, "notes": [note]}
    for key, value in changes.items():
        if key in {"run", "job", "note"}:
            target = {"run": run, "job": job, "note": note}[key]
            target.update(value)
        else:
            facts[key] = value
    return facts


def resolver_api(monkeypatch, *, main_head=COMMIT, blob=None, facts=None, sources=None, head_sha=COMMIT):
    calls = []
    sources = sources or {name: name.encode() for name in AUTHORITY["source_files"]}
    facts = facts or publication()

    def api(resource, raw=False):
        calls.append(resource)
        if resource.startswith("actions/runs/123/attempts/1"):
            return {"head_sha": head_sha, "head_branch": "main"}
        if resource == "branches/main":
            return {"commit": {"sha": main_head}}
        if resource.startswith("contents/.github/workflows/"):
            return {"sha": blob or AUTHORITY["workflow_blob_sha"]}
        if resource.startswith("contents/"):
            assert raw
            return sources[resource.split("/", 1)[1].split("?")[0]]
        if resource.startswith("actions/workflows/deploy.yml/runs?"):
            assert f"head_sha={COMMIT}" in resource and "event=push" in resource and "branch=main" in resource
            total = len(facts["runs"]) if facts["runs_total"] is None else facts["runs_total"]
            return {"total_count": total, "workflow_runs": facts["runs"]}
        if resource.startswith(f"actions/runs/{BUILD_RUN}/attempts/1/jobs"):
            total = len(facts["jobs"]) if facts["jobs_total"] is None else facts["jobs_total"]
            return {"total_count": total, "jobs": facts["jobs"]}
        if resource.startswith("check-runs/555/annotations"):
            return facts["notes"]
        raise AssertionError(resource)

    monkeypatch.setattr(DEPLOY, "api", api)
    monkeypatch.setattr(DEPLOY, "inspect_container", lambda name: {"Config": {"Labels": {
        "org.opencontainers.image.revision": "b" * 40,
    }}})
    return calls


def test_resolver_derives_release_from_verified_github_facts(monkeypatch):
    resolver_api(monkeypatch)
    release = DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})
    DEPLOY.validate_policy(release)
    assert release["commit"] == release["controller_commit"] == COMMIT
    assert release["previous_commit"] == "b" * 40
    assert release["image"] == IMAGE
    assert (release["build_run_id"], release["build_attempt"]) == (39700000000, 1)
    assert release["source_sha256"] == {
        name: hashlib.sha256(name.encode()).hexdigest() for name in AUTHORITY["source_files"]
    }


@pytest.mark.parametrize("override", [
    {"main_head": "e" * 40},
    {"blob": "f" * 40},
    {"facts": publication(runs=[])},
    {"facts": publication(runs=[publication()["runs"][0]] * 2)},
    {"facts": publication(runs_total=2)},
    {"facts": publication(jobs=[])},
    {"facts": publication(jobs_total=3)},
    {"facts": publication(notes=[])},
    {"facts": publication(notes=[{"title": "other", "message": "x"}])},
    {"head_sha": "not-a-sha"},
])
def test_resolver_rejects_unverifiable_release(monkeypatch, override):
    resolver_api(monkeypatch, **override)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})


FORGERIES = [
    {"run": {"conclusion": "failure"}},
    {"run": {"event": "pull_request"}},
    {"run": {"head_branch": "feature"}},
    {"run": {"head_sha": "e" * 40}},
    {"run": {"path": ".github/workflows/other.yml"}},
    {"run": {"id": "39700000000"}},
    {"run": {"head_repository": {"full_name": "attacker/abc"}}},
    {"run": {"repository": {"full_name": "attacker/abc"}}},
    {"job": {"conclusion": "failure"}},
    {"job": {"name": "other-job"}},
    {"job": {"run_id": 1}},
    {"job": {"steps": []}},
    {"job": {"steps": [{"name": "Publish image identity annotation", "conclusion": "success"}]}},
    {"note": {"message": f"ghcr.io/attacker/abc@sha256:{'d' * 64} {COMMIT} actions-{BUILD_RUN}-1"}},
    {"note": {"message": f"{IMAGE.replace('@sha256:', ':latest@sha256:')} {COMMIT} actions-{BUILD_RUN}-1"}},
    {"note": {"message": f"{IMAGE} {'e' * 40} actions-{BUILD_RUN}-1"}},
    {"note": {"message": f"{IMAGE} {COMMIT} actions-1-1"}},
    {"note": {"message": f"{IMAGE} {COMMIT} actions-{BUILD_RUN}-2"}},
    {"note": {"message": f"{IMAGE} {COMMIT}"}},
    {"note": {"message": IMAGE}},
]


@pytest.mark.parametrize("change", FORGERIES)
def test_resolver_rejects_forged_or_inconsistent_publication(monkeypatch, change):
    resolver_api(monkeypatch, facts=publication(**change))
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})


def test_duplicate_or_conflicting_identity_annotations_are_rejected(monkeypatch):
    note = publication()["notes"][0]
    other = {**note, "message": f"{IMAGE.replace('d' * 64, 'e' * 64)} {COMMIT} actions-{BUILD_RUN}-1"}
    for notes in ([note, note], [note, other], [other, note]):
        resolver_api(monkeypatch, facts=publication(notes=notes))
        with pytest.raises(DEPLOY.DeployError):
            DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})


def test_full_annotation_page_is_treated_as_incomplete(monkeypatch):
    note = publication()["notes"][0]
    filler = [{"title": "noise", "message": "x"}] * 99
    resolver_api(monkeypatch, facts=publication(notes=filler + [note]))
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})


def test_unrelated_annotations_do_not_block_the_genuine_identity(monkeypatch):
    note = publication()["notes"][0]
    resolver_api(monkeypatch, facts=publication(notes=[{"title": "Node 20", "message": "deprecated"}, note]))
    assert DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})["image"] == IMAGE


@pytest.mark.parametrize("request_body", [
    {"run_id": 123, "attempt": 1, "image": IMAGE}, {"run_id": "123", "attempt": 1},
    {"run_id": 123, "attempt": True}, {"run_id": 123},
])
def test_resolver_accepts_only_numeric_workflow_identity(monkeypatch, request_body):
    calls = resolver_api(monkeypatch)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.resolve_release(AUTHORITY, request_body)
    assert calls == []


def test_missing_current_revision_label_blocks_release(monkeypatch):
    resolver_api(monkeypatch)
    monkeypatch.setattr(DEPLOY, "inspect_container", lambda name: {"Config": {"Labels": {}}})
    release = DEPLOY.resolve_release(AUTHORITY, {"run_id": 123, "attempt": 1})
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_policy(release)


def job_condition(workflow, job):
    """Return the raw `if:` expression of a top-level job, read from the real YAML text."""
    lines = workflow.splitlines()
    start = lines.index(f"  {job}:")
    body = []
    for line in lines[start + 1:]:
        if line.startswith("  ") and not line.startswith("   ") and line.strip():
            break
        body.append(line)
    text = "\n".join(body)
    block = text.index("    if: >-\n") + len("    if: >-\n")
    expression = []
    for line in text[block:].splitlines():
        if not line.startswith("      "):
            break
        expression.append(line.strip())
    return " ".join(expression)


def evaluate_condition(expression, context):
    """Evaluate the GitHub expression subset used by the workflow against a context dict."""
    def lookup(path):
        value = context
        for part in path.split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        return value

    python = re.sub(
        r"\b(?:github|vars)(?:\.[A-Za-z_]+)+",
        lambda match: f"lookup({match.group(0)!r})",
        expression,
    ).replace("&&", " and ").replace("||", " or ")
    return bool(eval(python, {"lookup": lookup}))


def run_event(event, **overrides):
    """Context GitHub gives the production workflow, which always runs from the default branch head."""
    context = {
        "github": {
            "repository": "sginfoocio/abc", "ref": "refs/heads/main", "sha": COMMIT,
            "event_name": event,
            "event": {"workflow_run": {
                "conclusion": "success", "event": "push", "head_branch": "main", "head_sha": COMMIT,
                "head_repository": {"full_name": "sginfoocio/abc"},
            }},
        },
        "vars": {"CLOUD_AUTO_DEPLOY": "true"},
    }
    for path, value in overrides.items():
        target = context
        *parents, leaf = path.split("__")
        for part in parents:
            target = target[part]
        if value is None:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return context


PRODUCTION_WORKFLOW = (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text()


@pytest.mark.parametrize("event,overrides,expected", [
    ("workflow_run", {}, True),
    ("workflow_run", {"vars__CLOUD_AUTO_DEPLOY": None}, False),
    ("workflow_run", {"vars__CLOUD_AUTO_DEPLOY": "false"}, False),
    ("workflow_run", {"vars__CLOUD_AUTO_DEPLOY": "TRUE"}, False),
    ("workflow_run", {"github__event__workflow_run__conclusion": "failure"}, False),
    ("workflow_run", {"github__event__workflow_run__conclusion": "cancelled"}, False),
    ("workflow_run", {"github__event__workflow_run__event": "pull_request"}, False),
    ("workflow_run", {"github__event__workflow_run__head_branch": "feature"}, False),
    ("workflow_run", {"github__event__workflow_run__head_repository": {"full_name": "fork/abc"}}, False),
    # An old run, or the first of two consecutive merges: main has moved on.
    ("workflow_run", {"github__event__workflow_run__head_sha": "e" * 40}, False),
    ("workflow_run", {"github__ref": "refs/heads/feature"}, False),
    ("workflow_run", {"github__repository": "fork/abc"}, False),
    ("workflow_dispatch", {"vars__CLOUD_AUTO_DEPLOY": None}, True),
    ("workflow_dispatch", {"github__ref": "refs/heads/feature"}, False),
    ("pull_request", {}, False),
    ("pull_request_target", {}, False),
    ("push", {}, False),
    ("schedule", {}, False),
])
def test_production_workflow_trigger_conditions_behave_as_designed(event, overrides, expected):
    condition = job_condition(PRODUCTION_WORKFLOW, "verify-release")
    assert evaluate_condition(condition, run_event(event, **overrides)) is expected


def test_only_the_verified_job_can_reach_the_protected_environment():
    lines = PRODUCTION_WORKFLOW.splitlines()
    assert lines.count("    environment: production") == 1
    deploy = PRODUCTION_WORKFLOW[PRODUCTION_WORKFLOW.index("  deploy-production:"):]
    assert "    needs: verify-release" in deploy and "    environment: production" in deploy
    assert "    if:" not in deploy
    assert "inputs" not in PRODUCTION_WORKFLOW and "github.event.workflow_run.head" not in deploy


def workflow_permissions(workflow):
    """Map of scope -> level for the top-level `permissions:` block."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "permissions:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    scopes = {}
    for line in lines[start + 1:]:
        if not line.strip() or len(line) - len(line.lstrip()) <= indent:
            break
        scope, level = (part.strip() for part in line.split(":", 1))
        scopes[scope] = level
    return scopes


def test_workflows_hold_least_privilege_permissions():
    production = workflow_permissions(PRODUCTION_WORKFLOW)
    build = workflow_permissions((ROOT / ".github/workflows/deploy.yml").read_text())
    assert production == {"contents": "read", "actions": "read"}
    assert "statuses" not in build and "checks" not in build
    assert set(build.values()) <= {"read", "write"} and build.get("contents") == "read"
    assert {scope for scope, level in build.items() if level == "write"} <= {"packages"}


def test_identity_annotation_is_emitted_only_after_verification_on_main():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    verify = workflow.index("name: Verify built artifact identity and runtime imports")
    publish = workflow.index("name: Publish image identity annotation")
    assert verify < publish
    block = workflow[publish:workflow.index("      - name:", publish + 1)]
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in block
    assert "::notice title=cloud-image-identity::" in block
    assert "GITHUB_RUN_ATTEMPT" in block and "PUBLISHED_COMMIT" in block


def test_runner_job_only_invokes_the_root_controller():
    workflow = PRODUCTION_WORKFLOW
    assert workflow.count("sudo -n -- /usr/local/sbin/cloud-production-deploy") == 1
    assert "docker " not in workflow
    assert "permissions: {}" in workflow


def preflight_script():
    start = PRODUCTION_WORKFLOW.index("          python3 - <<'PY'\n") + len("          python3 - <<'PY'\n")
    end = PRODUCTION_WORKFLOW.index("\n          PY", start)
    return textwrap.dedent(PRODUCTION_WORKFLOW[start:end])


def preflight_gh(facts, main_head=COMMIT):
    def gh_api(arguments, **kwargs):
        endpoint = arguments[-1].split("repos/sginfoocio/abc/", 1)[1]
        if endpoint == "branches/main":
            return json.dumps({"commit": {"sha": main_head}}).encode()
        if endpoint.startswith("actions/workflows/deploy.yml/runs?"):
            runs = facts["runs"]
            total = len(runs) if facts["runs_total"] is None else facts["runs_total"]
            return json.dumps({"total_count": total, "workflow_runs": runs}).encode()
        if endpoint.startswith(f"actions/runs/{BUILD_RUN}/attempts/1/jobs"):
            jobs = facts["jobs"]
            total = len(jobs) if facts["jobs_total"] is None else facts["jobs_total"]
            return json.dumps({"total_count": total, "jobs": jobs}).encode()
        if endpoint.startswith("check-runs/555/annotations"):
            return json.dumps(facts["notes"]).encode()
        raise AssertionError(endpoint)
    return gh_api


@pytest.mark.parametrize("scenario,main_head,facts", [
    ("ok", COMMIT, publication()),
    ("not_main_head", "e" * 40, publication()),
    ("no_run", COMMIT, publication(runs=[])),
    ("two_runs", COMMIT, publication(runs=[publication()["runs"][0]] * 2)),
    ("incomplete_runs", COMMIT, publication(runs_total=2)),
    ("failed_run", COMMIT, publication(run={"conclusion": "failure"})),
    ("wrong_sha", COMMIT, publication(run={"head_sha": "e" * 40})),
    ("foreign_repo", COMMIT, publication(run={"head_repository": {"full_name": "fork/abc"}})),
    ("failed_job", COMMIT, publication(job={"conclusion": "failure"})),
    ("incomplete_jobs", COMMIT, publication(jobs_total=2)),
    ("no_annotation", COMMIT, publication(notes=[])),
    ("duplicate_annotation", COMMIT, publication(notes=publication()["notes"] * 2)),
    ("annotation_other_commit", COMMIT, publication(note={"message": f"{IMAGE} {'e' * 40} actions-{BUILD_RUN}-1"})),
    ("annotation_other_run", COMMIT, publication(note={"message": f"{IMAGE} {COMMIT} actions-1-1"})),
    ("annotation_mutable_tag", COMMIT, publication(note={"message": f"ghcr.io/sginfoocio/abc:latest {COMMIT} actions-{BUILD_RUN}-1"})),
])
def test_workflow_preflight_requires_verifiable_publication(monkeypatch, tmp_path, scenario, main_head, facts):
    script = preflight_script()
    compile(script, "deploy-cloud-production-preflight", "exec")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.setattr(subprocess, "check_output", preflight_gh(facts, main_head))
    if scenario == "ok":
        exec(script, {})
        assert IMAGE in summary.read_text()
    else:
        with pytest.raises(SystemExit):
            exec(script, {})
        assert not summary.exists()


def test_workflow_preflight_fails_closed_when_github_is_unavailable(monkeypatch, tmp_path):
    def failing(arguments, **kwargs):
        raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.setattr(subprocess, "check_output", failing)
    with pytest.raises(subprocess.CalledProcessError):
        exec(preflight_script(), {})
    assert not (tmp_path / "summary.md").exists()


def api_responses(policy):
    return {
        "actions/runs/123/attempts/1": {
            "repository": {"full_name": "sginfoocio/abc"},
            "event": "workflow_dispatch", "head_branch": "main", "head_sha": policy["controller_commit"],
            "path": DEPLOY.WORKFLOW, "status": "in_progress", "run_attempt": 1,
        },
        "actions/runs/123/attempts/1/jobs?per_page=100": {
            "jobs": [{"name": DEPLOY.JOB, "status": "in_progress", "runner_name": policy["runner_name"]}],
        },
        "actions/runs/123/approvals": [{
            "state": "approved", "user": {"login": "sginfoocio"},
            "environments": [{"name": "production"}],
        }],
        "environments/production": {"protection_rules": [{
            "type": "required_reviewers", "reviewers": [{"reviewer": {"login": "sginfoocio"}}],
        }], "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
        "environments/production/deployment-branch-policies": {
            "total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}],
        },
        f"actions/runs/{policy['build_run_id']}/attempts/1": {
            "conclusion": "success", "event": "push", "head_branch": "main",
            "head_sha": policy["commit"], "path": ".github/workflows/deploy.yml",
        },
        f"compare/{policy['previous_commit']}...{policy['commit']}": {"status": "ahead"},
    }


def write_test_certificate(directory, name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / f"{name}.pem"
    key_path = directory / f"{name}.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return certificate_path, key_path


class JsonHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def route_github_requests_to(monkeypatch, address):
    actual_urlopen = urlopen

    def open_local(request, *, timeout, context):
        assert request.full_url.startswith("https://api.github.com/")
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        local_url = f"https://127.0.0.1:{address[1]}"
        local_request = Request(
            request.full_url.replace("https://api.github.com", local_url, 1),
            headers=dict(request.header_items()),
        )
        return actual_urlopen(local_request, timeout=timeout, context=context)

    monkeypatch.setattr(DEPLOY.urllib.request, "urlopen", open_local)


def start_test_https_server(certificate_path, key_path):
    server = HTTPServer(("127.0.0.1", 0), JsonHandler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_github_api_uses_verified_ca_bundle_and_hostname_check(monkeypatch, tmp_path):
    certificate, key = write_test_certificate(tmp_path, "trusted")
    monkeypatch.setattr(DEPLOY, "CA_BUNDLE", str(certificate))
    server, thread = start_test_https_server(certificate, key)
    route_github_requests_to(monkeypatch, server.server_address)
    try:
        assert DEPLOY.api("rate_limit") == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_untrusted_github_certificate_aborts_deploy_before_mutation(monkeypatch, tmp_path, policy):
    trusted_certificate, _ = write_test_certificate(tmp_path, "trusted")
    server_certificate, server_key = write_test_certificate(tmp_path, "untrusted")
    monkeypatch.setattr(DEPLOY, "CA_BUNDLE", str(trusted_certificate))
    server, thread = start_test_https_server(server_certificate, server_key)
    route_github_requests_to(monkeypatch, server.server_address)
    reads = []
    monkeypatch.setattr(DEPLOY, "read_json", lambda path: reads.append(path))
    try:
        with pytest.raises(URLError) as error:
            DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
        assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
        assert reads == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("ca_file,expected_error", [
    ("missing", FileNotFoundError),
    ("invalid", ssl.SSLError),
])
def test_invalid_ca_bundle_fails_before_github_request(
    monkeypatch, tmp_path, ca_file, expected_error,
):
    bundle = tmp_path / "ca-bundle.pem"
    if ca_file == "invalid":
        bundle.write_text("not a certificate bundle", encoding="ascii")
    monkeypatch.setattr(DEPLOY, "CA_BUNDLE", str(bundle))
    requests = []
    monkeypatch.setattr(DEPLOY.urllib.request, "urlopen", lambda *args, **kwargs: requests.append(args))

    with pytest.raises(expected_error):
        DEPLOY.api("rate_limit")
    assert requests == []


def test_gate_verifies_workflow_approval_runner_and_publication(monkeypatch, policy):
    responses = api_responses(policy)
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


@pytest.mark.parametrize("resource,key,value", [
    ("actions/runs/123/attempts/1", "event", "pull_request"),
    ("actions/runs/123/attempts/1", "head_sha", "b" * 40),
    ("actions/runs/123/attempts/1", "head_branch", "unreviewed"),
    ("actions/runs/123/attempts/1", "run_attempt", 2),
    ("actions/runs/123/attempts/1/jobs?per_page=100", "jobs", []),
    ("environments/production", "protection_rules", []),
    ("compare/current", "status", "behind"),
])
def test_gate_rejects_wrong_context(monkeypatch, policy, resource, key, value):
    responses = api_responses(policy)
    if resource == "compare/current":
        resource = f"compare/{policy['previous_commit']}...{policy['commit']}"
    responses[resource][key] = value
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


@pytest.mark.parametrize("reviews", [[], [{
    "state": "approved", "user": {"login": "unapproved-user"}, "environments": [{"name": "production"}],
}], [{
    "state": "rejected", "user": {"login": "sginfoocio"}, "environments": [{"name": "production"}],
}]])
def test_gate_requires_explicit_administrative_approval(monkeypatch, policy, reviews):
    responses = api_responses(policy)
    responses["actions/runs/123/approvals"] = reviews
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


@pytest.mark.parametrize("workflow_request", [
    {"run_id": "123", "attempt": 1}, {"run_id": 123, "attempt": True},
    {"run_id": 123, "attempt": 1, "image": "arbitrary"}, {"run_id": 123, "attempt": 0},
])
def test_gate_accepts_no_image_paths_or_commands(policy, workflow_request):
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_request(workflow_request, policy)


@pytest.mark.parametrize("compare", ["behind", "diverged", "identical"])
def test_gate_blocks_downgrade_divergence_and_redeploy(monkeypatch, policy, compare):
    responses = api_responses(policy)
    responses[f"compare/{policy['previous_commit']}...{policy['commit']}"]["status"] = compare
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


@pytest.mark.parametrize("environment,branches", [
    ({"deployment_branch_policy": None}, None),
    ({"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
     {"total_count": 2, "branch_policies": [{"name": "main", "type": "branch"}, {"name": "*", "type": "branch"}]}),
    ({"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
     {"total_count": 1, "branch_policies": [{"name": "v1", "type": "tag"}]}),
    ({"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": False}}, None),
])
def test_gate_requires_environment_restricted_to_main(monkeypatch, policy, environment, branches):
    responses = api_responses(policy)
    responses["environments/production"].update(environment)
    if branches is not None:
        responses["environments/production/deployment-branch-policies"] = branches
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


@pytest.mark.parametrize("prevent,accepted", [(False, False), (True, True)])
def test_gate_enforces_self_review_prevention_when_policy_requires_it(monkeypatch, policy, prevent, accepted):
    responses = api_responses(policy)
    responses["environments/production"]["protection_rules"][0]["prevent_self_review"] = prevent
    monkeypatch.setattr(DEPLOY, "api", responses.__getitem__)
    policy["require_prevent_self_review"] = True
    if accepted:
        DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)
    else:
        with pytest.raises(DEPLOY.DeployError):
            DEPLOY.validate_request({"run_id": 123, "attempt": 1}, policy)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self.body[:limit]


def http_error(code):
    from urllib.error import HTTPError
    return HTTPError("https://api.github.com/x", code, "error", {}, None)


def stub_urlopen(monkeypatch, outcomes):
    attempts = []

    def urlopen(request, timeout, context):
        attempts.append(request.headers)
        outcome = outcomes[min(len(attempts), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(DEPLOY.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(DEPLOY.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(DEPLOY.ssl, "create_default_context", lambda cafile: None)
    return attempts


def test_github_api_retries_one_transient_server_error_then_succeeds(monkeypatch):
    attempts = stub_urlopen(monkeypatch, [http_error(502), b'{"ok": true}'])
    assert DEPLOY.api("rate_limit") == {"ok": True}
    assert len(attempts) == 2 and attempts[0]["X-github-api-version"] == "2022-11-28"


@pytest.mark.parametrize("code", [401, 403, 404, 429])
def test_github_api_fails_closed_without_retrying_client_errors(monkeypatch, code):
    attempts = stub_urlopen(monkeypatch, [http_error(code)])
    with pytest.raises(Exception) as caught:
        DEPLOY.api("rate_limit")
    assert getattr(caught.value, "code", None) == code and len(attempts) == 1


def test_github_api_gives_up_after_a_second_server_error(monkeypatch):
    attempts = stub_urlopen(monkeypatch, [http_error(503)])
    with pytest.raises(Exception):
        DEPLOY.api("rate_limit")
    assert len(attempts) == 2


def test_github_api_rejects_oversized_and_malformed_responses(monkeypatch):
    stub_urlopen(monkeypatch, [b" " * (DEPLOY.MAX_JSON_BYTES + 1)])
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.api("actions/runs/1")
    stub_urlopen(monkeypatch, [b'{"truncated": '])
    with pytest.raises(ValueError):
        DEPLOY.api("actions/runs/1")


@pytest.mark.skipif(os.name != "posix", reason="flock and root-owned state are POSIX only")
def test_concurrent_deployments_are_mutually_exclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(DEPLOY, "STATE", tmp_path)
    monkeypatch.setattr(DEPLOY, "secure_path", lambda path: None)
    with DEPLOY.lock("transaction.lock"):
        with pytest.raises(DEPLOY.DeployError):
            with DEPLOY.lock("transaction.lock"):
                raise AssertionError("second deployment must not start")
    with DEPLOY.lock("transaction.lock"):
        pass


def test_only_app_image_changes_without_mutating_other_config(config, policy):
    original = copy.deepcopy(config)
    result = DEPLOY.with_image(config, policy["image"])
    assert config == original
    assert result["services"]["abcd-app"]["image"] == policy["image"]
    result["services"]["abcd-app"]["image"] = original["services"]["abcd-app"]["image"]
    assert result == original


@pytest.mark.parametrize("extra", [{"include": ["untrusted.yml"]}, {"configs": {"external": {}}}])
def test_external_compose_resources_are_rejected(config, extra):
    config.update(extra)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_config(config)


@pytest.mark.parametrize("field", ["env_file", "extends", "build", "label_file", "develop"])
def test_external_service_instructions_are_rejected(config, field):
    config["services"]["abcd-app"][field] = "untrusted"
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_config(config)


def test_relative_mount_and_root_uid_rejected(config):
    config["services"]["abcd-app"]["volumes"][0]["source"] = "relative"
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_config(config)
    config["services"]["abcd-app"]["volumes"][0]["source"] = "/mnt/cloud-imagenes"
    config["services"]["abcd-app"]["user"] = "0:0"
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_config(config)


def test_compose_targets_only_app(monkeypatch):
    calls = []
    monkeypatch.setattr(DEPLOY, "run", lambda arguments, **kwargs: calls.append((arguments, kwargs)))
    DEPLOY.compose()
    command, options = calls[0]
    assert command[-1] == "abcd-app"
    assert "--no-deps" in command and "--no-build" in command and "--wait" in command
    assert command[command.index("--pull") + 1] == "never"
    assert options["timeout"] > 180
    assert "down" not in command and "--remove-orphans" not in command


def test_command_failures_never_expose_compose_secrets(monkeypatch):
    monkeypatch.setattr(DEPLOY.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, b"SECRET=private", b"password=private",
    ))
    with pytest.raises(DEPLOY.DeployError) as error:
        DEPLOY.run(["/usr/bin/docker"])
    assert "private" not in str(error.value)
    assert DEPLOY.run(["/usr/bin/docker"], check=False, combined=True) == b"SECRET=privatepassword=private"


@pytest.mark.parametrize("production", [False, True])
def test_regression_uses_no_workbook_or_container_input(monkeypatch, policy, production):
    commands = []
    monkeypatch.setattr(DEPLOY, "VALIDATOR", Path("validator"))
    monkeypatch.setattr(DEPLOY, "secure_path", lambda path: None)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "synthetic validator")

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        if arguments[1] == "create":
            return b"a" * 64
        if arguments[1] in {"start", "exec"}:
            return json.dumps({
                "rows": 3, "MONTURAS": 2, "GAFAS DE SOL": 1,
                "ean_8056266003066": "MONTURAS",
                "ean_sequence_preserved": True, "preview_xlsx_csv_match": True,
            }).encode()
        return b""

    monkeypatch.setattr(DEPLOY, "run", run)
    assert DEPLOY.regression(policy, production=production)["rows"] == 3
    assert not any("data" in options for _, options in commands)
    command = commands[0][0]
    if production:
        assert command[:3] == [DEPLOY.DOCKER, "exec", "--user"]
        assert "abcd-control" in command
    else:
        assert command[:3] == [DEPLOY.DOCKER, "create", "--network"]
        assert "none" in command and "--read-only" in command
        assert "--cap-drop" in command and "--security-opt" in command
        assert commands[1][0][:3] == [DEPLOY.DOCKER, "start", "-a"]
        assert commands[-1][0][:3] == [DEPLOY.DOCKER, "rm", "--force"]


@pytest.fixture
def transaction(monkeypatch, tmp_path, policy, config):
    writes, commands, health, changes = [], [], [], []
    monkeypatch.setattr(DEPLOY, "STATE", tmp_path)
    monkeypatch.setattr(DEPLOY, "validate_request", lambda *args: None)
    monkeypatch.setattr(DEPLOY, "read_json", lambda path: copy.deepcopy(config))
    monkeypatch.setattr(DEPLOY, "atomic_json", lambda path, value: writes.append((path.name, copy.deepcopy(value))))
    monkeypatch.setattr(DEPLOY, "inspect_container", lambda name: {
        "Image": "old-id", "Config": {"Labels": {
            "org.opencontainers.image.revision": policy["previous_commit"],
            "com.docker.compose.project": "cloud-nas", "com.docker.compose.service": "abcd-app",
        }},
    })
    monkeypatch.setattr(DEPLOY, "check_health", lambda *args: health.append(args))
    monkeypatch.setattr(DEPLOY, "other_containers", lambda: {"non-target": "unchanged"})
    monkeypatch.setattr(DEPLOY, "ensure_other_services", lambda before: None)
    monkeypatch.setattr(DEPLOY, "regression", lambda *args, **kwargs: {"rows": 902})
    monkeypatch.setattr(DEPLOY.signal, "signal", lambda *args: None)
    monkeypatch.setattr(DEPLOY, "compose", lambda: changes.append("compose"))

    def command(arguments, **kwargs):
        commands.append(arguments)
        if arguments[1:3] == ["image", "inspect"]:
            return json.dumps([{
                "Id": "new-id", "Config": {"Labels": {"org.opencontainers.image.revision": policy["commit"]}},
            }]).encode()
        return b""

    monkeypatch.setattr(DEPLOY, "run", command)
    return SimpleNamespace(writes=writes, commands=commands, health=health, changes=changes)


def test_success_records_candidate_production_and_unchanged_services(transaction, policy):
    receipt = DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
    assert receipt["status"] == "deployed"
    assert receipt["other_services"] == "unchanged"
    assert receipt["rollback"] == "not_needed"
    assert transaction.changes == ["compose"]
    assert (policy["commit"], "new-id") in transaction.health
    assert not any("prune" in command for command in transaction.commands)


@pytest.mark.parametrize("failure", ["compose", "health", "regression", "logs", "interruption"])
def test_postchange_failure_rolls_back_only_app(monkeypatch, transaction, policy, failure):
    if failure in {"compose", "interruption"}:
        count = 0

        def compose():
            nonlocal count
            count += 1
            if count == 1:
                if failure == "interruption":
                    raise KeyboardInterrupt
                raise DEPLOY.DeployError("start failed")
        monkeypatch.setattr(DEPLOY, "compose", compose)
    elif failure == "health":
        def health(commit, image_id):
            if image_id == "new-id":
                raise DEPLOY.DeployError("unhealthy")
        monkeypatch.setattr(DEPLOY, "check_health", health)
    elif failure == "regression":
        def regression(policy, production=False):
            if production:
                raise DEPLOY.DeployError("bad categories")
            return {"rows": 902}
        monkeypatch.setattr(DEPLOY, "regression", regression)
    else:
        real = DEPLOY.run

        def command(arguments, **kwargs):
            return b"ERROR startup failure" if arguments[1] == "logs" else real(arguments, **kwargs)
        monkeypatch.setattr(DEPLOY, "run", command)
    receipt = DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
    assert receipt["status"] == "failed" and receipt["rollback"] == "verified"
    images = [value["services"]["abcd-app"]["image"] for name, value in transaction.writes
              if name == "compose.private.json"]
    assert images == [policy["image"], "cloud-production:rollback-" + policy["previous_commit"]]


def test_failed_rollback_never_claims_success(monkeypatch, transaction, policy):
    def compose():
        raise DEPLOY.DeployError("failure")
    monkeypatch.setattr(DEPLOY, "compose", compose)
    receipt = DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
    assert receipt["status"] == "failed"
    assert receipt["rollback"] == "failed_manual_intervention_required"


def test_candidate_failure_never_changes_live_config(monkeypatch, transaction, policy):
    def regression(*args, **kwargs):
        raise DEPLOY.DeployError("bad candidate")
    monkeypatch.setattr(DEPLOY, "regression", regression)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
    assert not any(name == "compose.private.json" for name, value in transaction.writes)
    assert transaction.changes == []


def test_missing_private_config_aborts_before_container_inspection_or_mutation(monkeypatch, policy):
    calls = []
    monkeypatch.setattr(DEPLOY, "validate_request", lambda *args: calls.append("validate_request"))

    def missing_config(path):
        calls.append("read_config")
        raise FileNotFoundError(path)

    monkeypatch.setattr(DEPLOY, "read_json", missing_config)
    monkeypatch.setattr(DEPLOY, "inspect_container", lambda *args: calls.append("inspect_container"))
    monkeypatch.setattr(DEPLOY, "atomic_json", lambda *args: calls.append("atomic_json"))
    monkeypatch.setattr(DEPLOY, "compose", lambda: calls.append("compose"))

    with pytest.raises(FileNotFoundError):
        DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})

    assert calls == ["validate_request", "read_config"]


def test_duplicate_deployment_attempt_is_rejected_before_mutation(transaction, policy, tmp_path):
    (tmp_path / "run-123-1").mkdir()
    with pytest.raises(FileExistsError):
        DEPLOY.deploy(policy, {"run_id": 123, "attempt": 1})
    assert transaction.changes == []
    assert not any(name == "compose.private.json" for name, value in transaction.writes)


def test_non_target_state_change_is_not_ignored(monkeypatch):
    monkeypatch.setattr(DEPLOY, "other_containers", lambda: {"service": "changed"})
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.ensure_other_services({"service": "old"})


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission and fsync contract")
def test_atomic_config_preserves_owner_mode_and_checks_every_ancestor(monkeypatch, tmp_path):
    path = tmp_path / "compose.private.json"
    path.write_text("{}")
    path.chmod(0o600)
    before = path.stat()
    checked = []
    monkeypatch.setattr(DEPLOY, "secure_path", lambda item: checked.append(item))
    DEPLOY.atomic_json(path, {"image": "approved"})
    after = path.stat()
    assert (before.st_uid, before.st_gid, before.st_mode) == (after.st_uid, after.st_gid, after.st_mode)
    assert path in checked and path.parent in checked
    assert json.loads(path.read_text()) == {"image": "approved"}
    assert not list(tmp_path.glob(".cloud-release-*"))


def test_workflow_has_no_arbitrary_input_checkout_or_token_in_privileged_job():
    workflow = (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text()
    assert "on:\n  workflow_dispatch:\n" in workflow
    assert "inputs:" not in workflow and "pull_request:" not in workflow
    assert "comparison" not in workflow  # ancestry is verified by the root controller
    job = workflow.split("  deploy-production:\n", 1)[1]
    assert "    environment: production\n" in job
    assert "    permissions: {}\n" in job
    assert "    runs-on: [self-hosted, linux, x64, cloud-production]\n" in job
    assert "  cancel-in-progress: false\n" in workflow
    assert "    timeout-minutes: 25\n" in job
    assert "uses:" not in job
    assert "sudo -n -- /usr/local/sbin/cloud-production-deploy" in job
    sudoers = (ROOT / "deployment/cloud/cloud-production.sudoers").read_text()
    assert 'NOPASSWD: /usr/local/sbin/cloud-production-deploy ""' in sudoers
    assert "NOPASSWD: ALL" not in sudoers
    assert "luxottica-regression.xlsx" not in (ROOT / "deployment/cloud/cloud_production_deploy.py").read_text()
    service = (ROOT / "deployment/cloud/cloud-production-deploy.service").read_text()
    assert "TimeoutStartSec=1200" in service and "TimeoutStopSec=300" in service


def test_runner_hook_rejects_other_jobs_and_pull_requests():
    spec = importlib.util.spec_from_file_location("runner_gate", ROOT / "deployment/cloud/runner_job_gate.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    environment = {
        "GITHUB_REPOSITORY": "sginfoocio/abc", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main", "GITHUB_JOB": "deploy-production",
        "GITHUB_WORKFLOW_REF":
            "sginfoocio/abc/.github/workflows/deploy-cloud-production.yml@refs/heads/main",
    }
    assert hook.allowed(environment)
    assert hook.allowed({**environment, "GITHUB_EVENT_NAME": "workflow_run"})
    for event in ("push", "pull_request", "schedule", "workflow_call"):
        assert not hook.allowed({**environment, "GITHUB_EVENT_NAME": event})
    for key in environment:
        assert not hook.allowed({**environment, key: "untrusted"})
    assert not hook.allowed({})
    wrapper = (ROOT / "deployment/cloud/runner_job_gate.sh").read_text()
    assert "exec /usr/bin/python3 -I /usr/local/libexec/cloud-runner-job-gate.py" in wrapper


def test_service_dropin_preserves_nas_dependencies_and_clears_old_condition():
    override = (ROOT / "deployment/cloud/cloud-image-services.override.conf").read_text()
    base = (ROOT / "deployment/nfs/cloud-image-services.service").read_text()
    assert "Requires=docker.service mnt-cloud\\x2dimagenes.mount" in base
    assert "BindsTo=mnt-cloud\\x2dimagenes.mount" in base
    for directive in ("Requires=", "After=", "BindsTo="):
        assert directive not in override
    assert "[Unit]\nConditionPathExists=\nConditionPathExists=/etc/cloud-production/compose.private.json\n" in override
    assert "WorkingDirectory=/etc/cloud-production\n" in override
    assert override.count("--project-directory /etc/cloud-production") == 2
    assert "up -d --no-recreate --no-build --pull never --wait --wait-timeout 180" in override
    assert "stop --timeout 40" in override
    assert "/opt/cloud-image-production" not in override
    assert "--force-recreate" not in override
    # Release deployments must still replace only the app, unlike systemd startup.
    assert "--no-recreate" not in (ROOT / "deployment/cloud/cloud_production_deploy.py").read_text()


@pytest.fixture
def transition_check():
    spec = importlib.util.spec_from_file_location(
        "transition_check", ROOT / "deployment/cloud/verify_compose_transition.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transition_checks_entire_effective_config_and_only_ignores_build(transition_check, config):
    source = copy.deepcopy(config)
    source["services"]["abcd-app"]["build"] = {"context": "/original"}
    assert transition_check.compare_configs(source, config) == ["abcd-app", "abcd-order-alerts"]
    assert source["services"]["abcd-app"]["build"] == {"context": "/original"}


@pytest.mark.parametrize("field,value", [
    ("environment", {"SECRET": "changed"}),
    ("volumes", [{"type": "bind", "source": "/different", "target": "/nas"}]),
    ("user", "0:0"), ("group_add", ["100"]), ("cap_add", ["SYS_ADMIN"]),
    ("networks", ["other-network"]), ("healthcheck", {"disable": True}),
    ("restart", "always"), ("image", "different:tag"),
])
def test_transition_rejects_any_effective_service_change(transition_check, config, field, value):
    changed = copy.deepcopy(config)
    changed["services"]["abcd-app"][field] = value
    with pytest.raises(transition_check.TransitionError):
        transition_check.compare_configs(config, changed)


def test_transition_has_read_only_compose_and_fixed_project_paths(transition_check):
    original = transition_check.compose(transition_check.ORIGINAL)
    controlled = transition_check.compose(transition_check.CONTROLLED)
    assert "--project-directory" not in original
    assert controlled[controlled.index("--project-directory") + 1] == "/etc/cloud-production"
    assert controlled[-1] == "/etc/cloud-production/compose.private.json"
    source = (ROOT / "deployment/cloud/verify_compose_transition.py").read_text()
    for mutation in ('"up"', '"stop"', '"pull"', '"rm"', '"chmod"', '"chown"'):
        assert mutation not in source


@pytest.mark.parametrize("mismatch", ["hash", "image", "running", "health", "missing", "duplicate"])
def test_transition_rejects_runtime_drift(transition_check, config, mismatch):
    service = config["services"]["abcd-app"]
    containers = [{
        "Config": {"Labels": {
            "com.docker.compose.project": "cloud-nas", "com.docker.compose.service": "abcd-app",
            "com.docker.compose.config-hash": "expected-hash",
        }},
        "Image": "expected-image", "State": {"Running": True, "Health": {"Status": "healthy"}},
    }]
    hashes = {"abcd-app": "expected-hash"}
    image_ids = {service["image"]: "expected-image"}
    transition_check.check_runtime(["abcd-app"], config, hashes, containers, image_ids)
    if mismatch == "hash":
        hashes["abcd-app"] = "different"
    elif mismatch == "image":
        image_ids[service["image"]] = "different"
    elif mismatch == "running":
        containers[0]["State"]["Running"] = False
    elif mismatch == "health":
        containers[0]["State"]["Health"]["Status"] = "unhealthy"
    elif mismatch == "missing":
        containers.clear()
    else:
        containers.append(copy.deepcopy(containers[0]))
    with pytest.raises(transition_check.TransitionError):
        transition_check.check_runtime(["abcd-app"], config, hashes, containers, image_ids)
