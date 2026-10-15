#!/usr/bin/python3 -I
"""Fixed root trust boundary. No image, path, command or Python supplied by the runner."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request


ETC = Path("/etc/cloud-production")
STATE = Path("/var/lib/cloud-production-deploy")
CONFIG = ETC / "compose.private.json"
POLICY = ETC / "approved-release.json"
VALIDATOR = ETC / "validate_luxottica.py"
WORKBOOK = ETC / "luxottica-regression.xlsx"
UNIT = "cloud-production-deploy.service"
REPO = "sginfoocio/abc"
WORKFLOW = ".github/workflows/deploy-cloud-production.yml"
JOB = "Deploy approved abcd-app"
DOCKER = "/usr/bin/docker"
SYSTEMCTL = "/usr/bin/systemctl"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}


class DeployError(RuntimeError):
    pass


def secure_path(path):
    """No symlink or non-root writable ancestor may feed a privileged operation."""
    for item in [path, *path.parents]:
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise DeployError("Untrusted deployment path")
    return path


def read_json(path):
    secure_path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, payload):
    secure_path(path.parent)
    metadata = path.stat() if path.exists() else None
    if metadata is not None:
        secure_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".cloud-release-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            if metadata is not None:
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(name):
    import fcntl

    secure_path(STATE)
    path = STATE / name
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        secure_path(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeployError("Another deployment is active") from error
        yield
    finally:
        os.close(descriptor)


def run(arguments, timeout=60, data=None, check=True, combined=False):
    result = subprocess.run(
        arguments, input=data, capture_output=True, timeout=timeout, env=ENV, cwd="/",
    )
    if check and result.returncode:
        # Compose may print expanded environment/credentials; never expose its output.
        raise DeployError("Deployment command failed")
    return result.stdout + result.stderr if combined else result.stdout


def api(resource):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{resource}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "cloud-production-deploy"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def validate_policy(policy):
    if policy.get("repository") != REPO:
        raise DeployError("Repository is not approved")
    for key in ("commit", "previous_commit", "controller_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", policy.get(key, "")):
            raise DeployError("Invalid approved commit")
    if not re.fullmatch(r"ghcr\.io/sginfoocio/abc@sha256:[0-9a-f]{64}", policy.get("image", "")):
        raise DeployError("Invalid approved image")
    for key in ("build_run_id", "build_attempt"):
        if type(policy.get(key)) is not int or policy[key] < 1:
            raise DeployError("Invalid approved publication")
    if policy.get("runner_name") != "cloud-production-s2026" or not policy.get("reviewers"):
        raise DeployError("Invalid approved deployment identity")
    if set(policy.get("source_sha256", {})) != {
        "app_enhanced.py", "app_exports.py", "transform_luxottica_masterdata.py",
    }:
        raise DeployError("Incomplete source provenance")
    for checksum in [policy.get("workbook_sha256", ""), *policy["source_sha256"].values()]:
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise DeployError("Invalid approved checksum")


def validate_request(request, policy):
    if set(request) != {"run_id", "attempt"} or any(
        type(value) is not int or value < 1 for value in request.values()
    ):
        raise DeployError("Only numeric workflow identity is accepted")
    run_id, attempt = request["run_id"], request["attempt"]
    workflow = api(f"actions/runs/{run_id}/attempts/{attempt}")
    if not (
        workflow["repository"]["full_name"] == REPO
        and workflow["event"] == "workflow_dispatch"
        and workflow["head_branch"] == "main"
        and workflow["head_sha"] == policy["controller_commit"]
        and workflow["path"] == WORKFLOW
        and workflow["status"] == "in_progress"
        and workflow["run_attempt"] == attempt
    ):
        raise DeployError("Workflow is not the reviewed production controller")
    jobs = api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")["jobs"]
    active = [job for job in jobs if job["name"] == JOB and job["status"] == "in_progress"]
    if len(active) != 1 or active[0]["runner_name"] != policy["runner_name"]:
        raise DeployError("Production job is not running on its dedicated runner")
    approvals = api(f"actions/runs/{run_id}/approvals")
    production_reviews = [
        review for review in approvals
        if any(env["name"] == "production" for env in review["environments"])
    ]
    if not production_reviews:
        raise DeployError("Missing administrative production approval")
    if any(review["state"] == "rejected" for review in production_reviews) or not any(
        review["state"] == "approved" and review["user"]["login"] in policy["reviewers"]
        for review in production_reviews
    ):
        raise DeployError("Production approval is not authorized")
    environment = api("environments/production")
    reviewer_rules = [rule for rule in environment["protection_rules"] if rule["type"] == "required_reviewers"]
    if not reviewer_rules or not any(
        reviewer["reviewer"].get("login") in policy["reviewers"]
        for rule in reviewer_rules for reviewer in rule["reviewers"]
    ):
        raise DeployError("Production environment is no longer protected")
    build = api(f"actions/runs/{policy['build_run_id']}/attempts/{policy['build_attempt']}")
    if not (
        build["conclusion"] == "success" and build["event"] == "push"
        and build["head_branch"] == "main" and build["head_sha"] == policy["commit"]
        and build["path"] == ".github/workflows/deploy.yml"
    ):
        raise DeployError("Approved publication has no successful main CI")


def validate_config(config):
    """The administrator installs an expanded, reviewed, self-contained Compose JSON."""
    if config.get("include") or config.get("secrets") or config.get("configs"):
        raise DeployError("External Compose configuration requires administrative review")
    services = config.get("services", {})
    if "abcd-app" not in services:
        raise DeployError("Missing app service")
    for service in services.values():
        if any(key in service for key in ("extends", "env_file", "build", "label_file", "develop")):
            raise DeployError("Compose must be normalized and contain no external instructions")
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict):
                raise DeployError("Compose mounts must use normalized long syntax")
            if volume["type"] == "bind" and not PurePosixPath(volume["source"]).is_absolute():
                raise DeployError("Relative mount source is forbidden")
    if services["abcd-app"].get("container_name") != "abcd-control":
        raise DeployError("Unexpected app container")
    if str(services["abcd-app"].get("user")) != "1037:100":
        raise DeployError("Unexpected production UID")


def with_image(config, image):
    updated = copy.deepcopy(config)
    updated["services"]["abcd-app"]["image"] = image
    return updated


def inspect_container(name):
    return json.loads(run([DOCKER, "inspect", name]))[0]


def other_containers():
    ids = run([DOCKER, "ps", "-aq", "--no-trunc"]).decode().split()
    if not ids:
        raise DeployError("No containers found")
    containers = json.loads(run([DOCKER, "inspect", *ids]))
    # Candidate smoke has already exited; every non-target container is protected.
    return {
        item["Name"]: {
            "id": item["Id"], "image": item["Image"],
            "running": item["State"]["Running"],
            "health": item["State"].get("Health", {}).get("Status"),
        }
        for item in containers if item["Name"] != "/abcd-control"
    }


def ensure_other_services(before):
    if other_containers() != before:
        raise DeployError("A non-target container changed state, identity or image")
    for name in ("/mustotal-api", "/mustotal-web", "/abcd-order-alerts", "/abcd-luxoptica-monitor",
                 "/abcd-control-abcd-kering-scheduler-1"):
        if name not in before or not before[name]["running"]:
            raise DeployError("A required non-target service is not running")


def compose():
    run([
        DOCKER, "compose", "--project-directory", str(ETC), "--project-name", "cloud-nas",
        "-f", str(CONFIG), "up", "-d", "--no-deps", "--no-build", "--pull", "never",
        "--wait", "--wait-timeout", "180", "abcd-app",
    ], timeout=240)


def check_health(commit, image_id):
    container = inspect_container("abcd-control")
    if (
        container["Config"]["Labels"].get("org.opencontainers.image.revision") != commit
        or container["Image"] != image_id
        or not container["State"]["Running"]
        or container["State"].get("Health", {}).get("Status") != "healthy"
    ):
        raise DeployError("Container identity or health mismatch")
    run([
        DOCKER, "exec", "--user", "1037:100", "abcd-control", "python", "-c",
        "import urllib.request; "
        "assert urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=10).read() == b'ok'",
    ], timeout=20)


def regression(policy, production=False):
    secure_path(VALIDATOR)
    secure_path(WORKBOOK)
    raw = WORKBOOK.read_bytes()
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != policy["workbook_sha256"]:
        raise DeployError("Original customer workbook checksum mismatch")
    script = VALIDATOR.read_text(encoding="utf-8")
    if production:
        arguments = [DOCKER, "exec", "-i", "--user", "1037:100", "abcd-control", "python"]
    else:
        arguments = [
            DOCKER, "create", "-i", "--network", "none", "--read-only", "--user", "1037:100",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "768m", "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--entrypoint", "python", policy["image"],
        ]
    arguments.extend(["-c", script, json.dumps(policy), "production" if production else "candidate"])
    if production:
        output = run(arguments, timeout=120, data=raw)
    else:
        container_id = run(arguments).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise DeployError("Invalid isolated candidate identity")
        try:
            output = run([DOCKER, "start", "-ai", container_id], timeout=120, data=raw)
        finally:
            # Clean up only the newly created, isolated candidate, even on timeout.
            run([DOCKER, "rm", "--force", container_id])
    result = json.loads(output)
    if result.get("rows") != 902 or result.get("ean_8056266003066") != "MONTURAS":
        raise DeployError("Incomplete real workbook regression")
    return result


def interrupted(signum, frame):
    raise DeployError("Deployment interrupted")


def deploy(policy, request):
    validate_policy(policy)
    validate_request(request, policy)
    config = read_json(CONFIG)
    validate_config(config)
    old = inspect_container("abcd-control")
    labels = old["Config"]["Labels"]
    if (
        labels.get("org.opencontainers.image.revision") != policy["previous_commit"]
        or labels.get("com.docker.compose.project") != "cloud-nas"
        or labels.get("com.docker.compose.service") != "abcd-app"
    ):
        raise DeployError("Unexpected current production release")
    check_health(policy["previous_commit"], old["Image"])
    before = other_containers()
    ensure_other_services(before)
    release = STATE / f"run-{request['run_id']}-{request['attempt']}"
    release.mkdir(mode=0o700)  # No replay/overwrite of an earlier release receipt.
    atomic_json(release / "compose.before.json", config)
    atomic_json(release / "containers.before.json", before)
    rollback = f"cloud-production:rollback-{policy['previous_commit']}"
    run([DOCKER, "image", "tag", old["Image"], rollback])
    run([DOCKER, "pull", policy["image"]], timeout=300)
    candidate = json.loads(run([DOCKER, "image", "inspect", policy["image"]]))[0]
    if candidate["Config"]["Labels"].get("org.opencontainers.image.revision") != policy["commit"]:
        raise DeployError("Pulled artifact provenance mismatch")
    candidate_result = regression(policy)
    ensure_other_services(before)
    receipt = {
        **request, "previous_commit": policy["previous_commit"], "new_commit": policy["commit"],
        "image": policy["image"], "rollback_image": rollback, "candidate_regression": candidate_result,
        "status": "starting", "rollback": "not_needed",
    }
    atomic_json(release / "receipt.json", receipt)
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # The transaction is supervised by systemd, not by the cancellable Actions process.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        atomic_json(CONFIG, with_image(config, policy["image"]))
        compose()
        check_health(policy["commit"], candidate["Id"])
        receipt["production_regression"] = regression(policy, production=True)
        ensure_other_services(before)
        logs = run([DOCKER, "logs", "--since", start, "--tail", "200", "abcd-control"], combined=True).decode(
            "utf-8", errors="replace",
        )
        if re.search(r"Traceback \(most recent call last\)|Uncaught app execution|\bERROR\b", logs):
            raise DeployError("New app startup errors detected")
        receipt.update(status="deployed", health="healthy", other_services="unchanged")
    except (Exception, KeyboardInterrupt) as error:
        # Do not include exception text: it can contain private Compose values.
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        receipt.update(status="failed", rollback="starting", failure_type=type(error).__name__)
        atomic_json(release / "receipt.json", receipt)
        try:
            # Pin the retained ID, not a mutable registry tag or the former mutable tag.
            atomic_json(CONFIG, with_image(config, rollback))
            compose()
            check_health(policy["previous_commit"], old["Image"])
            ensure_other_services(before)
            receipt.update(rollback="verified", health="healthy", other_services="unchanged")
        except (Exception, KeyboardInterrupt):
            receipt.update(rollback="failed_manual_intervention_required")
        atomic_json(release / "receipt.json", receipt)
        return receipt
    atomic_json(release / "receipt.json", receipt)
    return receipt


def worker():
    with lock("transaction.lock"):
        request = read_json(STATE / "request.json")
        try:
            result = deploy(read_json(POLICY), request)
        except Exception as error:
            print(f"Deployment worker failed: {type(error).__name__}", file=sys.stderr)
            result = {**request, "status": "failed_requires_review", "production_changed": "unknown"}
        atomic_json(STATE / "result.json", result)
        return result


def submit():
    raw = sys.stdin.buffer.read(1025)
    if len(raw) > 1024:
        raise DeployError("Oversized workflow identity")
    request = json.loads(raw)
    with lock("submission.lock"):
        policy = read_json(POLICY)
        validate_policy(policy)
        validate_request(request, policy)
        unit = run([SYSTEMCTL, "show", UNIT, "--property=ActiveState", "--value"]).decode().strip()
        if unit not in {"inactive", "failed"}:
            raise DeployError("A supervised transaction is already active")
        atomic_json(STATE / "request.json", request)
        run([SYSTEMCTL, "start", UNIT], timeout=1250, check=False)
        result = read_json(STATE / "result.json")
        if any(result.get(key) != request[key] for key in ("run_id", "attempt")):
            raise DeployError("Stale deployment result")
        return result


def main():
    if os.geteuid() != 0 or socket.gethostname() != "s2026":
        raise DeployError("Root on the approved host is required")
    secure_path(Path(__file__))
    os.umask(0o077)
    if sys.argv[1:] not in ([], ["--worker"]):
        raise DeployError("No deployment arguments are accepted")
    result = worker() if sys.argv[1:] else submit()
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "deployed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"status": "rejected", "detail": "Deployment gate failed; ask the administrator to review"}))
        sys.exit(1)
