"""Gestion de la lista de clientes vigilados para la alerta de pedidos."""

from __future__ import annotations

import json
import os
from pathlib import Path

WATCHLIST_PATH_ENV = "WATCHLIST_PATH"
# Ubicación histórica (versionada en git); solo se lee si WATCHLIST_PATH aún no existe.
WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist_clientes.json"


def default_watchlist_path() -> Path:
    return Path(os.getenv(WATCHLIST_PATH_ENV, "").strip() or WATCHLIST_FILE)


def load_watchlist(path: Path | str | None = None) -> list[str]:
    """Lee los nombres de clientes vigilados desde el fichero JSON."""
    if path is None:
        path = default_watchlist_path()
        if not path.exists() and WATCHLIST_FILE.is_file():
            path = WATCHLIST_FILE
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    return list(data.get("clientes", []))


def save_watchlist(clientes: list[str], path: Path | str | None = None) -> None:
    path = Path(path) if path is not None else default_watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reemplazo atómico: el servicio automático nunca lee un JSON a medio escribir.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"clientes": clientes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def add_customer(name: str, path: Path | str | None = None) -> list[str]:
    name = name.strip()
    clientes = load_watchlist(path)
    if name and name not in clientes:
        clientes.append(name)
        save_watchlist(clientes, path)
    return clientes


def remove_customer(name: str, path: Path | str | None = None) -> list[str]:
    clientes = [c for c in load_watchlist(path) if c != name]
    save_watchlist(clientes, path)
    return clientes
