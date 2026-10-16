import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

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
    ("workbook_sha256", ""),
])
def test_policy_rejects_untrusted_inputs(policy, key, value):
    policy[key] = value
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.validate_policy(policy)


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
    }


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
