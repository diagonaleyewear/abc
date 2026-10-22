"""Sáfilo connector (#59): bulk EAN search → "add to selection" → download all selected views.

Synchronous/interactive portal (Safilo Omnia). The browser driver is NOT implemented: access
method, terms and automation restrictions are unconfirmed. This module provides the
verifiable part: chunked bulk-search lists, a driver contract, and a strict importer that
only accepts file↔EAN↔view correspondence proven by a portal manifest/metadata. Without a
manifest every file goes to review; order or name guesses are never used.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from provider_images import (
    PortalPackage, PortalStatus, ProviderConnector, ProviderSettings, read_zip_package,
)

PROVIDER = "safilo"
DEFAULTS = ProviderSettings(PROVIDER, max_eans_per_request=100, max_requests_per_day=10, pilot_cap=50,
                            allow_filename_ean=False, poll_base_seconds=0.0)


def bulk_search_text(eans: list[str]) -> str:
    """Comma-separated list expected by the portal's bulk EAN search."""
    return ",".join(eans)


@dataclass
class SelectionResult:
    selection_id: str
    found_eans: list[str] = field(default_factory=list)


class SafiloDriver:
    """Contract for the future driver (must stop with InterventionRequired on CAPTCHA/MFA)."""

    def search_and_select(self, search_text: str) -> SelectionResult:
        raise NotImplementedError

    def download_selection(self, selection_id: str) -> bytes:
        """ZIP with every view of the selected products (+ manifest if the portal provides one)."""
        raise NotImplementedError


class SafiloPortal:
    """Core-contract adapter: the selection is ready as soon as it is created."""

    def __init__(self, driver: SafiloDriver):
        self.driver = driver
        self.not_found: dict[str, list[str]] = {}

    def submit(self, eans: list[str]) -> str:
        result = self.driver.search_and_select(bulk_search_text(eans))
        self.not_found[result.selection_id] = sorted(set(eans) - set(result.found_eans))
        return result.selection_id

    def status(self, remote_id: str) -> PortalStatus:
        missing = self.not_found.get(remote_id, [])
        return PortalStatus("available", f"{len(missing)} EAN sin resultado" if missing else "")

    def download(self, remote_id: str) -> PortalPackage:
        return read_zip_package(self.driver.download_selection(remote_id))


class SafiloConnector(ProviderConnector):
    provider = PROVIDER
    default_mode = "portal"
    defaults = DEFAULTS


def strict_settings(settings: ProviderSettings) -> ProviderSettings:
    """Safilo never infers file↔EAN from file names."""
    return replace(settings, allow_filename_ean=False)
