import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
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


@pytest.fixture
def policy():
    result = json.loads((ROOT / "deployment/cloud/approved-release.json").read_text())
    result["controller_commit"] = "a" * 40
    result["commit"] = "c" * 40
    result["release_status"] = "approved"
    return result


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


def test_checked_in_policy_pins_the_published_forward_release():
    release = json.loads((ROOT / "deployment/cloud/approved-release.json").read_text())
    assert release["previous_commit"] == "c03674a048b26cdb2f27fb262cc2633ebadbd295"
    assert release["commit"] == "64276b40d2bfa42bc7ff349a4390dadd62638eb0"
    assert release["image"] == (
        "ghcr.io/sginfoocio/abc@sha256:"
        "04ac4836361e59a59fb2f354f1dc05ce5aa71bbc427a0f603a017d87f29a01ec"
    )
    assert release["build_run_id"] == 39065951026
    assert release["build_attempt"] == 1
    assert release["release_status"] == "approved"
    assert release["source_sha256"] == {
        "app_enhanced.py": "040fc49d6a7da97e7815bc82075da7510f51a951d72ca89c2f6e42c31be55e4a",
        "app_exports.py": "05fb1557f5cdfe68f1890bd0443fb193d53f2858b0fbfd06174f08e054f60704",
        "transform_luxottica_masterdata.py": "4e435ddadb6362f7ef7f2cd245b54c5fb439ddae903f1a4bdd70788d8256d609",
    }
    DEPLOY.validate_policy({**release, "controller_commit": "a" * 40})


@pytest.mark.parametrize("comparison_status,build_sha", [
    ("ahead", "64276b40d2bfa42bc7ff349a4390dadd62638eb0"),
    ("behind", "64276b40d2bfa42bc7ff349a4390dadd62638eb0"),
    ("ahead", "b" * 40),
])
def test_workflow_preflight_checks_release_ancestry_and_publication(
    monkeypatch, tmp_path, comparison_status, build_sha,
):
    workflow = (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text()
    start = workflow.index("          python3 - <<'PY'\n") + len("          python3 - <<'PY'\n")
    end = workflow.index("\n          PY", start)
    script = textwrap.dedent(workflow[start:end])
    compile(script, "deploy-cloud-production-preflight", "exec")
    monkeypatch.chdir(ROOT)
    calls = []

    def gh_api(arguments, **kwargs):
        endpoint = arguments[-1]
        calls.append(endpoint)
        if "/compare/" in endpoint:
            return json.dumps({"status": comparison_status}).encode()
        return json.dumps({
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_sha": build_sha,
            "path": ".github/workflows/deploy.yml",
            "run_attempt": 1,
        }).encode()

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(subprocess, "check_output", gh_api)
    if comparison_status != "ahead":
        with pytest.raises(SystemExit, match="not a forward update"):
            exec(script, {})
    elif build_sha != "64276b40d2bfa42bc7ff349a4390dadd62638eb0":
        with pytest.raises(SystemExit, match="no matching successful main publication"):
            exec(script, {})
    else:
        exec(script, {})
        assert "04ac4836361e59a59fb2f354f1dc05ce5aa71bbc427a0f603a017d87f29a01ec" in summary.read_text()
    assert len(calls) == (1 if comparison_status != "ahead" else 2)


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
        }]},
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
    ("compare/c03674a048b26cdb2f27fb262cc2633ebadbd295..." + "c" * 40, "status", "behind"),
])
def test_gate_rejects_wrong_context(monkeypatch, policy, resource, key, value):
    responses = api_responses(policy)
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
    assert 'policy["release_status"] != "approved"' in workflow
    assert 'comparison["status"] != "ahead"' in workflow
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
