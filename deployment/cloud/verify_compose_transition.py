#!/usr/bin/python3 -I
"""Administrative read-only gate before switching the Cloud systemd configuration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys


ORIGINAL = "/opt/cloud-image-production/active/compose.private.json"
CONTROLLED = "/etc/cloud-production/compose.private.json"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}


class TransitionError(RuntimeError):
    pass


def command(arguments):
    result = subprocess.run(
        arguments, cwd="/opt/cloud-image-production", env=ENV, timeout=60,
        check=False, capture_output=True,
    )
    if result.returncode:
        raise TransitionError("Read-only Compose/Docker check failed; no configuration values logged")
    return result.stdout


def compose(path):
    arguments = ["/usr/bin/docker", "compose"]
    if path == CONTROLLED:
        arguments += ["--project-directory", "/etc/cloud-production"]
    return [*arguments, "--project-name", "cloud-nas", "-f", path]


def compare_configs(original, controlled):
    expected = copy.deepcopy(original)
    actual = copy.deepcopy(controlled)
    # Only unused build instructions may be removed from the reviewed snapshot.
    for config in (expected, actual):
        for service in config.get("services", {}).values():
            service.pop("build", None)
    if expected != actual:
        raise TransitionError("Effective configuration differs; installation must stop")
    return sorted(actual["services"])


def hashes(path):
    lines = command([*compose(path), "config", "--hash", "*"]).decode().splitlines()
    result = {}
    for line in lines:
        service, checksum = line.split()
        if service in result:
            raise TransitionError("Duplicate Compose service hash")
        result[service] = checksum
    return result


def check_runtime(services, config, expected_hashes, containers, image_ids):
    if set(expected_hashes) != set(services):
        raise TransitionError("Incomplete service hash set")
    by_service = {}
    for container in containers:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != "cloud-nas":
            raise TransitionError("Unexpected Compose project")
        service = labels["com.docker.compose.service"]
        if service in by_service:
            raise TransitionError("Multiple containers for a Cloud service require review")
        by_service[service] = container
    if set(by_service) != set(services):
        raise TransitionError("Existing containers do not match the full Cloud service set")
    for service in services:
        container = by_service[service]
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.config-hash") != expected_hashes[service]:
            raise TransitionError("Runtime Compose hash differs; review before installation")
        if container["Image"] != image_ids[config["services"][service]["image"]]:
            raise TransitionError("Runtime image differs from the controlled configuration")
        if not container["State"]["Running"]:
            raise TransitionError("A Cloud service is not running")
        if container["State"].get("Health", {}).get("Status", "healthy") != "healthy":
            raise TransitionError("A Cloud service is not healthy")


def verify():
    original = json.loads(command([*compose(ORIGINAL), "config", "--format", "json"]))
    controlled = json.loads(command([*compose(CONTROLLED), "config", "--format", "json"]))
    services = compare_configs(original, controlled)
    if json.loads(Path(CONTROLLED).read_text()).get("services") is None:
        raise TransitionError("Controlled configuration is missing")
    if any("build" in service for service in controlled["services"].values()):
        raise TransitionError("Remove reviewed unused build instructions from controlled snapshot")
    old_hashes, new_hashes = hashes(ORIGINAL), hashes(CONTROLLED)
    if old_hashes != new_hashes:
        raise TransitionError("Project-directory transition changes effective Compose hashes")
    ids = command([
        "/usr/bin/docker", "ps", "-aq", "--no-trunc",
        "--filter", "label=com.docker.compose.project=cloud-nas",
    ]).decode().split()
    if not ids:
        raise TransitionError("No existing Cloud containers")
    containers = json.loads(command(["/usr/bin/docker", "inspect", *ids]))
    image_ids = {}
    for service in controlled["services"].values():
        image = service["image"]
        if image not in image_ids:
            image_ids[image] = json.loads(command(["/usr/bin/docker", "image", "inspect", image]))[0]["Id"]
    check_runtime(services, controlled, new_hashes, containers, image_ids)
    return {
        "status": "equivalent", "service_count": len(services),
        "effective_configuration_equal": True, "compose_hashes_equal": True,
        "runtime_hashes_and_images_match": True, "containers_changed": False,
    }


if __name__ == "__main__":
    try:
        if os.geteuid() != 0 or sys.argv[1:]:
            raise TransitionError("Administrative read-only invocation with no arguments required")
        print(json.dumps(verify()))
    except Exception:
        print(json.dumps({
            "status": "rejected",
            "detail": "Private configuration/runtime equivalence is unverified; do not install the drop-in",
        }))
        sys.exit(1)
