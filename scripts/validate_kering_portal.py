from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import socket
import ssl
import subprocess
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kering_images import ConfigStore, ImageStore, data_root, process_ean, configured_orders, batch_lock, execute_run
from kering_portal import KeringPortal
from kering_jobs import db_engine, prepare_selection, make_loader
from playwright.sync_api import sync_playwright


class MeasuredPortal(KeringPortal):
    def __init__(self, config):
        super().__init__(config)
        self.lookups = 0
        self.downloads = 0

    def _find_product(self, ean):
        self.lookups += 1
        return super()._find_product(ean)

    def _read_image(self, url):
        self.downloads += 1
        return super()._read_image(url)


def diagnose_environment():
    events = []
    def measure(phase, operation):
        started = time.monotonic()
        try:
            operation()
            code = "ok"
        except subprocess.TimeoutExpired:
            code = "timeout_dns"
        except ssl.SSLError:
            code = "fallo_tls"
        except OSError:
            code = "fallo_chromium" if phase == "chromium" else "fallo_red"
        except Exception:
            code = "fallo_chromium" if phase == "chromium" else "fallo_diagnostico"
        event = {"phase": phase, "duration_ms": round((time.monotonic() - started) * 1000), "code": code}
        events.append(event)
        return code == "ok"
    for host in ("my.keringeyewear.com", "picture.kecdn.net"):
        def dns(host=host):
            result = subprocess.run(
                [sys.executable, "-c", "import socket,sys; socket.getaddrinfo(sys.argv[1],443)", host],
                capture_output=True, timeout=10, check=False)
            if result.returncode:
                raise OSError()
        if not measure(f"dns:{host}", dns):
            continue
        def tcp(host=host):
            with socket.create_connection((host, 443), timeout=10):
                pass
        measure(f"tcp:{host}", tcp)
        def tls(host=host):
            context = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as connection:
                with context.wrap_socket(connection, server_hostname=host):
                    pass
        measure(f"tls:{host}", tls)
    def chromium():
        with sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True, timeout=15000)
            try:
                page = browser.new_page()
                page.set_content('<div id="kering-probe">ready</div>', timeout=5000)
                page.locator("#kering-probe").wait_for(state="visible", timeout=5000)
            finally:
                browser.close()
    measure("chromium", chromium)
    return {"passed": all(event["code"] == "ok" for event in events), "phases": events}


def validate_one_order(config_store, order_id):
    store = ImageStore(data_root())
    engine = db_engine()
    try:
        with batch_lock(store):
            config = config_store.load()
            if config.get("auto_enabled", False):
                return {"passed": False, "code": "automatizacion_activa"}
            if order_id not in config.get("test_order_ids", []):
                return {"passed": False, "code": "pedido_no_autorizado"}
            start = date.fromisoformat(config["cutoff_date"])
            end = datetime.now(ZoneInfo("Europe/Madrid")).date()
            orders = configured_orders(engine, config, start, end, [order_id])
            if len(orders) != 1:
                return {"passed": False, "code": "pedido_fuera_de_alcance"}
            prepare_selection(store, config, orders, start, end)
            probe = MeasuredPortal(config).check_access()
            if not probe["ok"]:
                return {"passed": False, "access": probe}
            portal = MeasuredPortal(config)
            try:
                run_id = store.create_run(orders, "validacion-servidor", config["supplier_id"], start, end)
                loader = make_loader(store, config_store, lambda: engine, start, end, config["supplier_id"])
                execute_run(store, run_id, loader, portal)
                with store.connect() as connection:
                    attempt = connection.execute("SELECT status,error,results FROM attempts WHERE run_id=?", (run_id,)).fetchone()
                    history = connection.execute(
                        "SELECT results FROM attempts WHERE order_id=? ORDER BY attempt DESC LIMIT 20",
                        (order_id,)).fetchall()
                evidence = order_image_evidence(store, orders[0], json.loads(attempt["results"]), history)
                return {"passed": attempt["status"] == "Completo" and all(
                            item["valid_three_views"] and item["identity_verified"] for item in evidence),
                        "order_id": order_id, "order_date": orders[0]["date_order"],
                        "cutoff_date": config["cutoff_date"], "access": probe, "images": evidence,
                        "status": attempt["status"], "code": attempt["error"],
                        "lookups": portal.lookups, "downloads": portal.downloads,
                        "phases": portal.access_events}
            finally:
                portal.close()
    finally:
        engine.dispose()


def order_image_evidence(store, order, results, history):
    evidence = []
    for ean in sorted({line["ean"] for line in order["lines"]}):
        valid = store.valid_views(ean)
        identity = next((json.loads(row["results"]).get(ean, {}).get("identity")
                         for row in history if json.loads(row["results"]).get(ean, {}).get("identity")), {})
        verified = ean in (identity.get("ean"), identity.get("upc")) and all(
            identity.get(field) for field in ("model", "color", "size"))
        views = []
        for view, path in sorted(valid.items()):
            with Image.open(path) as image:
                views.append({"view": view, "width": image.width, "height": image.height})
        evidence.append({"ean": ean, "views": views,
                         "valid_three_views": {"frontal", "perspectiva", "lateral"} <= set(valid),
                         "identity_verified": bool(verified),
                         "associated_with_order": ean in results,
                         "outcomes": results.get(ean, {}).get("views", {}),
                         "code": results.get(ean, {}).get("reason", "")})
    return evidence


def validate(config, ean, root):
    probe = MeasuredPortal(config).check_access()
    if not probe["ok"]:
        return {"access": probe, "passed": False}
    portal = MeasuredPortal(config)
    try:
        complete_store = ImageStore(root / "complete")
        first = process_ean(complete_store, portal, ean)
        before = (portal.lookups, portal.downloads)
        repeated = process_ean(complete_store, portal, ean)
        repeated_calls = (portal.lookups - before[0], portal.downloads - before[1])
        partial_store = ImageStore(root / "partial")
        for view, path in complete_store.valid_views(ean).items():
            if view in {"frontal", "perspectiva"}:
                partial_store.save_view(ean, view, path.read_bytes())
        before = (portal.lookups, portal.downloads)
        partial = process_ean(partial_store, portal, ean)
        partial_calls = (portal.lookups - before[0], portal.downloads - before[1])
        return {
            "access": probe, "ean": ean, "first": first, "repeated": repeated,
            "repeated_calls": repeated_calls, "partial_repository_retry": partial,
            "partial_calls": partial_calls,
            "passed": {"frontal", "perspectiva", "lateral"} <= complete_store.valid_views(ean).keys()
                and repeated_calls == (0, 0)
                and {"frontal", "perspectiva", "lateral"} <= partial_store.valid_views(ean).keys(),
        }
    finally:
        portal.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--ean")
    modes.add_argument("--diagnostics", action="store_true")
    modes.add_argument("--access-only", action="store_true")
    modes.add_argument("--order-id", type=int)
    parser.add_argument("--allow-network", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.allow_network:
        parser.error("La validacion real requiere --allow-network; no ejecutar desde CI")
    try:
        if arguments.diagnostics:
            report = diagnose_environment()
        elif arguments.order_id is not None:
            report = validate_one_order(ConfigStore(data_root()), arguments.order_id)
        else:
            config = ConfigStore(data_root()).load()
            if arguments.access_only:
                access = MeasuredPortal(config).check_access()
                report = {"passed": access["ok"], "access": access}
            else:
                with tempfile.TemporaryDirectory(prefix="kering-real-validation-") as temporary:
                    report = validate(config, arguments.ean, Path(temporary))
    except Exception:
        report = {"passed": False, "code": "validacion_real_fallida"}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())