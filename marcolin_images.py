"""Marcolin connector (#59): deferred asset requests with state tracking.

Documented flow (not yet confirmed with the portal): Multimedia → Assets requests → Product
images + brands → EAN list → validation → confirmations → Profile → Requests → download
(hours or days later). The portal driver is NOT implemented: access method, terms,
MFA/CAPTCHA and the ZIP layout are unconfirmed. Everything else works today:
- planning without duplicating live requests (DB-enforced),
- the manual-assisted path (export EAN list, upload the ZIP the operator downloaded),
- a portal adapter contract the real driver must satisfy.
"""
from __future__ import annotations

from provider_images import (
    ProviderConnector, ProviderPortal, ProviderSettings, PortalPackage, PortalStatus, UnverifiedProviderPortal,
)

PROVIDER = "marcolin"
DEFAULTS = ProviderSettings(PROVIDER, max_eans_per_request=50, max_requests_per_day=3, pilot_cap=50)
PENDING_WORDS = {"pending", "pendiente", "requested", "solicitado", "processing", "en proceso", "in progress"}
AVAILABLE_WORDS = {"available", "disponible", "ready", "listo", "completed", "completado", "done"}
FAILED_WORDS = {"rejected", "rechazado", "failed", "error", "expired", "caducado", "cancelled", "cancelado"}


def map_portal_status(raw: str) -> PortalStatus:
    """Portal wording -> state. Unrecognised wording is 'unknown' and forces manual review."""
    word = (raw or "").strip().lower()
    if word in AVAILABLE_WORDS:
        return PortalStatus("available", word)
    if word in FAILED_WORDS:
        return PortalStatus("failed", word)
    if word in PENDING_WORDS:
        return PortalStatus("pending", word)
    return PortalStatus("unknown", word)


class MarcolinDriver(ProviderPortal):
    """Contract for the future real driver (HTTP or browser). ``requests()`` must list the
    portal's own pending/available requests so ambiguous submissions can be reconciled."""

    def requests(self) -> list[dict]:
        raise NotImplementedError


class UnverifiedMarcolinPortal(UnverifiedProviderPortal):
    pass


class MarcolinPortal:
    """Adapts a driver to the core contract, normalising status wording."""

    def __init__(self, driver):
        self.driver = driver

    def submit(self, eans: list[str]) -> str:
        return self.driver.submit(eans)

    def status(self, remote_id: str) -> PortalStatus:
        return map_portal_status(self.driver.status_text(remote_id))

    def download(self, remote_id: str) -> PortalPackage:
        return self.driver.download(remote_id)


class MarcolinConnector(ProviderConnector):
    provider = PROVIDER
    default_mode = "portal"
    defaults = DEFAULTS
