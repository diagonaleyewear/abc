from __future__ import annotations

import re
import logging
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from pathlib import Path
from image_naming import kering_filename
from kering_media import identify_media
from image_repository import ImageRepository, image_fingerprint
from PIL import Image

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout, Error as BrowserError

from kering_images import (
    PORTAL_URL, InterventionRequired, LoginFailed, PortalFailure, PortalTimeout,
    ProductNotFound, SessionExpired,
)


TIMEOUT_MS = 30000
ACCESS_TOTAL_MS = 60000
ACCESS_PHASE_MS = {
    "configuracion": 1000, "chromium": 15000, "apertura_portal": 20000, "carga_formulario": 10000,
    "envio_login": 10000, "redireccion": 15000, "comprobacion_sesion": 10000,
}
LOGGER = logging.getLogger(__name__)
PRODUCT_LIMIT = 40
PRODUCT_SNAPSHOT = """() => {
    const fields = {};
    for (const label of document.querySelectorAll('.characteristics-title')) {
        fields[label.textContent.trim()] = label.nextElementSibling?.textContent.trim() || '';
    }
    const images = [...document.querySelectorAll('#imageModal .itemModal img')]
        .map(img => ({url: img.src, reference: img.alt, title: img.title,
                      label: img.getAttribute('aria-label') || '',
                      original_name: img.getAttribute('data-filename') || ''}));
    const size = [...document.querySelectorAll('span')]
        .find(el => el.children.length === 0 && /^\\s*TALLA\\s/.test(el.textContent));
    return {ean: fields.EAN || '', upc: fields.UPC || '',
            reference: images[0]?.reference || '', size: size?.textContent.trim() || '', images};
}"""


def allowed_url(url: str, media: bool = False) -> bool:
    parsed = urlsplit(url)
    hosts = {"picture.kecdn.net"} if media else {"my.keringeyewear.com"}
    return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
            and not parsed.username and not parsed.password)


def exact_product(snapshot: dict, ean: str) -> bool:
    return (ean in (snapshot["ean"], snapshot["upc"])
            and bool(re.fullmatch(r".+-\d{3}", snapshot["reference"])) and bool(snapshot["size"]))


class KeringPortal:
    def __init__(self, config: dict):
        self._config = dict(config)
        self._manager = None
        self._browser = None
        self._context = None
        self._page = None
        self.identities = {}
        self.repository = None
        self.media = {}
        self.access_events = []
        self.access_failure = None
        self._access_deadline = None
        self._phase_deadline = None
        self._phase = None
        self._navigation_network_failed = False

    def __repr__(self):
        return "<KeringPortal>"

    def _start(self):
        if self._page is not None:
            return
        self.access_events = []
        self.access_failure = None
        self._navigation_network_failed = False
        self._access_deadline = time.monotonic() + ACCESS_TOTAL_MS / 1000
        try:
            with self._access_phase("configuracion"):
                url = self._config.get("url", PORTAL_URL)
                if not allowed_url(url) or not self._config.get("username") or not self._config.get("password"):
                    raise LoginFailed() from None
            with self._access_phase("chromium"):
                self._manager = sync_playwright().start()
                self._browser = self._manager.chromium.launch(headless=True, timeout=self._remaining_ms())
                self._context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
                self._page = self._context.new_page()
                self._page.add_locator_handler(
                    self._page.locator("#onetrust-banner-sdk"), self._dismiss_consent)
                self._page.on("requestfailed", self._navigation_failed)
                self._page.on("response", self._navigation_response)
                self._page.set_default_timeout(TIMEOUT_MS)
                self._page.set_default_navigation_timeout(TIMEOUT_MS)
            with self._access_phase("apertura_portal"):
                response = self._page.goto(url, wait_until="domcontentloaded", timeout=self._remaining_ms())
                if response is not None and response.status >= 400:
                    raise PortalFailure("fallo_red") from None
                if not allowed_url(self._page.url):
                    raise LoginFailed() from None
            self._login()
        finally:
            self._access_deadline = self._phase_deadline = None

    def _remaining_ms(self):
        deadlines = [value for value in (self._access_deadline, self._phase_deadline) if value is not None]
        remaining = min(deadlines) - time.monotonic()
        if remaining <= 0:
            raise BrowserTimeout("access deadline")
        return max(1, int(remaining * 1000))

    def _dismiss_consent(self):
        timeout = self._remaining_ms() if self._access_deadline is not None else TIMEOUT_MS
        self._page.locator("#onetrust-reject-all-handler").click(timeout=timeout)

    @contextmanager
    def _access_phase(self, phase):
        started = time.monotonic()
        self._phase = phase
        self._phase_deadline = started + ACCESS_PHASE_MS[phase] / 1000
        code = "ok"
        try:
            self._remaining_ms()
            yield
            self._remaining_ms()
        except Exception as error:
            if isinstance(error, BrowserTimeout):
                try:
                    self._intervention_if_open()
                except InterventionRequired:
                    code = "intervencion_captcha_o_mfa"
                except BrowserError:
                    code = "fallo_red"
                else:
                    code = "timeout_portal"
            elif isinstance(error, BrowserError):
                code = "fallo_chromium" if phase == "chromium" else "fallo_red"
            elif isinstance(error, InterventionRequired):
                code = "intervencion_captcha_o_mfa"
            elif isinstance(error, PortalFailure):
                code = "fallo_red" if error.args == ("fallo_red",) else error.code
            else:
                code = "fallo_portal"
            if code == "intervencion_captcha_o_mfa":
                raise InterventionRequired() from None
            failure = PortalTimeout() if code == "timeout_portal" else PortalFailure()
            failure.code = code
            failure.retryable = False
            raise failure from None
        finally:
            event = {"phase": phase, "duration_ms": round((time.monotonic() - started) * 1000), "code": code}
            self.access_events.append(event)
            LOGGER.info("kering_access phase=%s duration_ms=%s code=%s",
                        phase, event["duration_ms"], code)
            if code != "ok":
                self.access_failure = event
            self._phase_deadline = None

    def _wait_access_state(self, predicate):
        while True:
            self._remaining_ms()
            self._intervention()
            if self._navigation_network_failed:
                raise PortalFailure("fallo_red") from None
            rejection = self._page.get_by_role("alert").filter(has_text=re.compile(
                r"(credenciales.*(incorrect|invalid)|contrase[nñ]a.*incorrect|"
                r"invalid.*(credentials|password)|incorrect.*password)", re.I))
            if rejection.count() and rejection.first.is_visible():
                raise LoginFailed() from None
            if predicate():
                return
            time.sleep(min(0.1, self._remaining_ms() / 1000))

    def _navigation_failed(self, request):
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self._navigation_network_failed = True

    def _navigation_response(self, response):
        if response.status >= 400:
            self._navigation_failed(response.request)

    def _intervention(self):
        selectors = 'input[autocomplete="one-time-code"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'
        if self._page.locator(selectors).filter(visible=True).count():
            raise InterventionRequired() from None
        challenge = self._page.get_by_text(re.compile(
            r"^(Introduce el c[oó]digo de verificaci[oó]n|Verifica tu identidad|Enter verification code)", re.I))
        if challenge.count() and challenge.first.is_visible():
            raise InterventionRequired() from None

    def _authenticated(self):
        self._intervention()
        logout = self._page.locator('a[href="/es/logout"]')
        search = self._page.locator(".showSearchBar").filter(visible=True)
        return logout.count() > 0 and search.count() > 0

    def _login(self):
        with self._access_phase("carga_formulario"):
            mail = self._page.get_by_placeholder("MAIL", exact=True)
            password = self._page.get_by_placeholder("CONTRASEÑA", exact=True)
            submit = self._page.get_by_role("button", name="Iniciar Sesión", exact=True)
            self._wait_access_state(lambda: self._authenticated() or (
                mail.is_visible() and password.is_visible() and submit.is_visible() and submit.is_enabled()))
        if not self._authenticated():
            with self._access_phase("envio_login"):
                mail.fill(self._config["username"], timeout=self._remaining_ms())
                password.fill(self._config["password"], timeout=self._remaining_ms())
                initial_url = self._page.url
                submit.click(timeout=self._remaining_ms(), no_wait_after=True)
            with self._access_phase("redireccion"):
                self._wait_access_state(lambda: self._page.url != initial_url or self._authenticated())
        with self._access_phase("comprobacion_sesion"):
            self._wait_access_state(self._authenticated)
            if not allowed_url(self._page.url):
                raise LoginFailed() from None

    def _check_session(self):
        if not self._authenticated():
            raise SessionExpired() from None

    def _find_product(self, ean):
        self._check_session()
        self._page.locator('.showSearchBar').filter(visible=True).first.click()
        search = self._page.locator('#hard-js-site-search-input')
        search.fill(ean)
        with self._page.expect_navigation(wait_until="domcontentloaded", timeout=TIMEOUT_MS):
            search.press("Enter")
        self._check_session()
        links = self._page.locator('.plp-products-container .product-item a[href]').evaluate_all(
            "elements => [...new Set(elements.map(el => el.href).filter(href => /\\/p\\/\\d+$/.test(href)))]")
        for url in links[:PRODUCT_LIMIT]:
            if not allowed_url(url):
                continue
            self._page.goto(url, wait_until="domcontentloaded")
            self._check_session()
            snapshot = self._page.evaluate(PRODUCT_SNAPSHOT)
            if exact_product(snapshot, ean):
                reference, color = snapshot["reference"].rsplit("-", 1)
                self.identities[ean] = {"ean": snapshot["ean"], "upc": snapshot["upc"],
                                        "model": reference, "color": color, "size": snapshot["size"]}
                return snapshot
        raise ProductNotFound() from None

    def _download(self, snapshot, pending, collected):
        repository = self.repository or ImageRepository()
        ean = getattr(self, "_ean", snapshot["ean"])
        records = repository.records(ean)
        reference, color = snapshot["reference"].rsplit("-", 1)
        for image in snapshot["images"]:
            if image["reference"] != snapshot["reference"] or not allowed_url(image["url"], media=True):
                continue
            url = image["url"]
            if url in collected:
                continue
            detection = identify_media(image)
            source_name = detection["source_name"]
            view = detection["normalized_view"]
            existing = [record for record in records if record.origin == url or
                        record.provider != "Kering" and view != "unknown"
                        and record.name == source_name and record.view == view]
            reusable = False
            for record in existing:
                try:
                    image_fingerprint(repository.verified_bytes(record))
                    reusable = True
                    break
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                    LOGGER.warning("kering_media_invalid ean=%s", ean)
            if reusable:
                if record.origin != url:
                    repository.save(ean, record.name, repository.verified_bytes(record),
                                    provider="Kering", origin=url, view=view,
                                    metadata={"modelo": reference, "view_detection": detection,
                                              "reused_from_provider": record.provider})
                if record.view == "unknown" and view != "unknown" and not record.metadata.get("view_review"):
                    repository.review_view(record.id, view, reviewer="kering-adapter",
                                           reason="Verified original filename pattern", evidence=detection)
                continue
            content = self._read_image(image["url"])
            if content is not None:
                name = kering_filename(reference, color, view, Path(source_name).suffix, source_name)
                self.media[url] = {"url": url, "name": name, "detection": detection, "view": view}
                collected[url] = content

    def _read_image(self, url):
        response = self._context.request.get(url, timeout=TIMEOUT_MS, max_redirects=0)
        try:
            if response.status in (401, 403) or 300 <= response.status < 400:
                raise SessionExpired() from None
            mime = response.headers.get("content-type", "").split(";", 1)[0]
            if not response.ok or mime not in {"image/jpeg", "image/png", "image/webp"}:
                return None
            if int(response.headers.get("content-length", "0")) > 30 * 1024 * 1024:
                return None
            content = response.body()
            return content if len(content) <= 30 * 1024 * 1024 else None
        finally:
            response.dispose()

    def fetch(self, ean: str, pending: tuple[str, ...]) -> dict[str, bytes]:
        self._ean = ean
        self.media = {}
        collected = {}
        try:
            for attempt in range(2):
                try:
                    self._start()
                    snapshot = self._find_product(ean)
                    self._download(snapshot, pending, collected)
                    return collected
                except SessionExpired:
                    self.close()
                    if attempt:
                        raise
        except BrowserTimeout:
            self._intervention_if_open()
            if collected:
                return collected
            raise PortalTimeout() from None
        except (PortalFailure, InterventionRequired) as error:
            if isinstance(error, SessionExpired):
                self.access_failure = {"phase": "comprobacion_sesion", "duration_ms": 0, "code": error.code}
                error.retryable = False
                LOGGER.info("kering_access phase=comprobacion_sesion duration_ms=0 code=%s", error.code)
            self.close()
            raise
        except Exception:
            raise PortalFailure() from None

    def _intervention_if_open(self):
        if self._page is not None:
            self._intervention()

    def check_access(self) -> dict:
        try:
            self._start()
            self._check_session()
            return self._access_result(True, "acceso_autenticado")
        except InterventionRequired:
            return self._access_result(False, "intervencion_captcha_o_mfa")
        except BrowserTimeout:
            return self._access_result(False, "timeout_portal")
        except PortalFailure as error:
            return self._access_result(False, error.code)
        except Exception:
            LOGGER.error("kering_access phase=%s code=fallo_portal", self._phase or "configuracion")
            return self._access_result(False, "fallo_portal")
        finally:
            self.close()

    def _access_result(self, ok, code):
        return {"ok": ok, "code": code, "phase": self._phase or "configuracion",
                "duration_ms": sum(event["duration_ms"] for event in self.access_events),
                "phases": list(self.access_events)}

    def close(self):
        for resource, operation in ((self._browser, "close"), (self._manager, "stop")):
            try:
                if resource is not None:
                    getattr(resource, operation)()
            except Exception:
                LOGGER.warning("kering_cleanup code=fallo_cierre")
        self._page = self._context = self._browser = self._manager = None