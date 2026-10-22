"""NuORDER connector (#59): capability verdict and manual-assisted import.

Verified from NuORDER's public help desk (API integration article): the official API uses
OAuth 1.0 and is *brand-initiated and tied to a brand portal*; NuORDER does not push data and
the documentation describes brands feeding their ERP data in. No retailer-side image API is
documented, so none is assumed. Web automation of app.next.nuorder.com is not confirmed
(terms, MFA/CAPTCHA), so no automation is implemented. The connector therefore runs only in
``manual`` mode: the operator exports the EAN list, downloads in the portal, and uploads the
ZIP; matching/provenance/versions are the shared core's. Which Odoo partners map to the
portal groups (Dita, Cutler and Gross, Akoni/Balmain, Linda Farrow/Jacquemus) comes from
``provider_config.json`` after the Odoo audit; never from brand names. A portal adapter must
be added only after NuORDER/the brands confirm access.
"""
from __future__ import annotations

from provider_images import ProviderConnector, ProviderSettings, UnverifiedProviderPortal
from kering_images import AccessNotConfirmed

PROVIDER = "nuorder"
DEFAULTS = ProviderSettings(PROVIDER, max_eans_per_request=200, max_requests_per_day=0, pilot_cap=50)
CAPABILITIES = {
    "official_api": "brand-initiated OAuth 1.0, per brand portal; no retailer image API documented",
    "web_automation": "unconfirmed (terms of use, MFA/CAPTCHA not verified)",
    "bulk_download_by_ean": "unconfirmed; requires authorised manual exploration",
    "mode": "manual-assisted import",
}


class NuorderPortal(UnverifiedProviderPortal):
    def submit(self, eans):
        raise AccessNotConfirmed("nuorder_acceso_minorista_no_confirmado")


class NuorderConnector(ProviderConnector):
    provider = PROVIDER
    default_mode = "manual"
    defaults = DEFAULTS

    def detect(self, start, end, *, mode=None, limit=None):
        if mode not in (None, "manual"):
            raise AccessNotConfirmed("nuorder_solo_modo_manual")
        return super().detect(start, end, mode="manual", limit=limit)
