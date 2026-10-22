"""Offline fakes for provider connector tests: simulated Odoo, portals and package builders."""
from io import BytesIO
import itertools
from zipfile import ZipFile

from PIL import Image
from sqlalchemy import create_engine, text

from provider_images import PortalFile, PortalPackage, PortalStatus


def make_ean(body12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


EANS = [make_ean(f"84000000{n:04d}") for n in range(1, 9)]
_colors = itertools.count(1)


def photo(seed: int | None = None) -> bytes:
    seed = next(_colors) if seed is None else seed
    out = BytesIO()
    Image.new("RGB", (640, 640), (seed % 256, (seed * 7) % 256, (seed * 13) % 256)).save(out, "PNG")
    return out.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def fake_odoo(orders: dict[int, list[tuple[int, str, str | None]]]):
    """orders: partner_id -> [(order_id, order_name, barcode)]; partner 573 has child 574."""
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE res_partner(id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN DEFAULT 1,"
            " supplier_rank INTEGER DEFAULT 0, commercial_partner_id INTEGER)",
            "CREATE TABLE purchase_order(id INTEGER PRIMARY KEY, name TEXT, partner_id INTEGER, date_order TEXT, state TEXT)",
            "CREATE TABLE purchase_order_line(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,"
            " name TEXT, product_qty REAL, display_type TEXT)",
            "CREATE TABLE product_product(id INTEGER PRIMARY KEY, barcode TEXT, product_tmpl_id INTEGER)",
            "CREATE TABLE product_supplierinfo(id INTEGER PRIMARY KEY, partner_id INTEGER, product_tmpl_id INTEGER)",
        ):
            c.execute(text(ddl))
        c.execute(text("INSERT INTO res_partner VALUES (573,'Marcolin',1,1,573),(574,'Marcolin contacto',1,0,573),"
                       "(900,'Otro',1,1,900)"))
        line = 0
        for partner, rows in orders.items():
            for order_id, name, barcode in rows:
                line += 1
                c.execute(text("INSERT INTO purchase_order VALUES (:i,:n,:p,'2026-09-10 10:00:00','purchase')"),
                          {"i": order_id, "n": name, "p": partner})
                c.execute(text("INSERT INTO product_product VALUES (:i,:b,:i)"), {"i": line, "b": barcode})
                c.execute(text("INSERT INTO purchase_order_line VALUES (:l,:o,:p,'x',1,NULL)"),
                          {"l": line, "o": order_id, "p": line})
    return engine


class FakePortal:
    """Deterministic portal double. Records every call so tests can assert no duplicates."""

    def __init__(self, package=None, statuses=("pending", "available")):
        self.package = package or PortalPackage()
        self.statuses = list(statuses)
        self.submitted: list[list[str]] = []
        self.status_calls = 0
        self.submit_error: Exception | None = None

    def submit(self, eans):
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(list(eans))
        return f"REQ-{len(self.submitted)}"

    def status(self, remote_id):
        self.status_calls += 1
        state = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return PortalStatus(state)

    def download(self, remote_id):
        return self.package


def pfile(name, content=None, **kw):
    return PortalFile(name=name, content=content if content is not None else photo(), **kw)
