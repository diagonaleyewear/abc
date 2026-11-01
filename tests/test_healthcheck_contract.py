"""Contrato del healthcheck de abcd-app (#14): el comando efectivo existe en la imagen y falla si Streamlit no responde."""
from __future__ import annotations

from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEALTH_URL = "http://localhost:8501/_stcore/health"
# Ejecutables que la imagen final garantiza: python de la imagen base más lo instalado con apt-get.
APT_PROVIDES = {"postgresql-client": {"psql", "pg_isready"}, "tzdata": set()}


def final_stage_lines() -> list[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    stages = re.split(r"(?im)^FROM\s", text)
    return stages[-1].splitlines()


def dockerfile_health_command() -> list[str]:
    for line in final_stage_lines():
        if line.upper().startswith("HEALTHCHECK"):
            command = re.split(r"\sCMD\s", line, maxsplit=1)[1]
            return shlex.split(command)
    raise AssertionError("La imagen final no define HEALTHCHECK")


def installed_executables() -> set[str]:
    executables = {"python", "sh"}
    for line in final_stage_lines():
        if "apt-get install" in line:
            for package in re.findall(r"[a-z][a-z0-9.+-]+", line.split("apt-get install", 1)[1]):
                executables |= APT_PROVIDES.get(package, {package})
    return executables


def compose_services(name: str) -> dict[str, list[str]]:
    """Líneas de cada servicio (sin dependencia de PyYAML, que no está en los requisitos bloqueados)."""
    services: dict[str, list[str]] = {}
    current = None
    in_services = False
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
        elif in_services and re.match(r"^\S", line) and not line.startswith("#"):
            in_services = False
        match = re.match(r"^  ([\w-]+):\s*$", line) if in_services else None
        if match:
            current = match[1]
            services[current] = []
        elif current and in_services:
            services[current].append(line)
    return services


def explicit_health_executables(lines: list[str]) -> list[str]:
    executables = []
    for index, line in enumerate(lines):
        match = re.match(r'^\s+test:\s*\[\s*"CMD(?:-SHELL)?",\s*"([^"]+)"', line)
        if match:
            executables.append(shlex.split(match[1])[0] if "CMD-SHELL" in line else match[1])
        elif re.match(r"^\s+test:", line):
            raise AssertionError(f"Formato de healthcheck no reconocido: {line.strip()}")
    return executables


def test_dockerfile_healthcheck_uses_only_executables_in_final_image() -> None:
    command = dockerfile_health_command()
    assert command[0] == "python"
    assert HEALTH_URL in command[2]
    assert "curl" not in installed_executables() and "curl" not in " ".join(command)


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_never_overrides_healthcheck_with_missing_binaries(name) -> None:
    services = compose_services(name)
    assert "abcd-app" in services
    for service, lines in services.items():
        for executable in explicit_health_executables(lines):
            assert executable in installed_executables(), f"{service}: '{executable}' no existe en la imagen final"


def test_app_service_inherits_dockerfile_healthcheck() -> None:
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert not any("healthcheck:" in line for line in compose_services(name)["abcd-app"]), (
            f"{name} sobrescribe el healthcheck de abcd-app; debe heredar el del Dockerfile"
        )


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_healthcheck(port: int) -> int:
    command = dockerfile_health_command()
    code = command[2].replace("localhost:8501", f"127.0.0.1:{port}")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30).returncode


class Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def serve(status: int):
    handler = type("H", (Handler,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_healthcheck_command_passes_only_when_endpoint_answers_ok() -> None:
    healthy = serve(200)
    try:
        assert run_healthcheck(healthy.server_port) == 0
    finally:
        healthy.shutdown()
    broken = serve(503)
    try:
        assert run_healthcheck(broken.server_port) != 0
    finally:
        broken.shutdown()
    assert run_healthcheck(free_port()) != 0


@pytest.mark.skipif(importlib.util.find_spec("streamlit") is None, reason="streamlit no instalado")
def test_healthcheck_follows_real_streamlit_lifecycle(tmp_path) -> None:
    app = tmp_path / "app.py"
    app.write_text("import streamlit as st\nst.write('ok')\n", encoding="utf-8")
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(app), "--server.headless", "true",
         "--server.port", str(port), "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
                    if response.read() == b"ok":
                        break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("Streamlit no llegó a responder")
        assert run_healthcheck(port) == 0
    finally:
        process.terminate()
        process.wait(timeout=30)
    assert run_healthcheck(port) != 0
