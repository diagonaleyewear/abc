"""Artifact identity, never a guessed application version or runtime environment override."""
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parent
BUILD_FILE = ROOT / "build-info.json"


def load_build_info() -> dict[str, str]:
    if BUILD_FILE.is_file():
        info = json.loads(BUILD_FILE.read_text(encoding="utf-8"))
        if set(info) != {"version", "commit", "published", "build"}:
            raise ValueError("Metadatos del artefacto incompletos")
        if not re.fullmatch(r"[0-9a-f]{40}", info["commit"]):
            raise ValueError("Commit de artefacto invalido")
        datetime.fromisoformat(info["published"].replace("Z", "+00:00"))
        if not all(isinstance(value, str) and value for value in info.values()):
            raise ValueError("Metadatos del artefacto invalidos")
        return info
    # Local source has no deployed artifact; disclose it explicitly.
    if (ROOT / ".git").exists():
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                                capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, check=True,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return {"version": f"local-{commit[:12]}" + ("-modificado" if dirty else ""),
                "commit": commit, "published": "", "build": "Código local; no artefacto desplegado"}
    return {"version": "build sin identificar", "commit": "", "published": "", "build": "Sin información"}


def render_build_footer():
    import streamlit as st

    st.divider()
    try:
        info = load_build_info()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        st.error(f"No se puede identificar el artefacto: {type(error).__name__}")
        return
    with st.expander(f"Cloud · {info['version']}"):
        st.caption("Información de soporte · facilite estos identificadores al equipo responsable.")
        st.write(f"Build: {info['build']}")
        st.write(f"Commit: {info['commit'] or 'Sin información'}")
        st.write(f"Publicación del artefacto: {info['published'] or 'Sin información (ejecución local)'}")
        st.caption("No existe una fuente mantenida de cambios por versión en este repositorio.")
