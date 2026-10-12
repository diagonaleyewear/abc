import io
import csv
import zipfile
from decimal import Decimal

import pytest
from openpyxl import Workbook

from adyen_reconciliation import ReportError, money, process_reports, read_table


def test_csv_preserves_source_rows_and_decimal():
    rows = read_table(b"Type;Net Credit (NC)\nSettled;12.35\n\nFee;0\n", "report.csv")
    assert [row.row for row in rows] == [2, 4]
    assert money(rows[0].values["Net Credit (NC)"]) == Decimal("12.35")


def test_xlsx_dates_and_empty_cells():
    from datetime import datetime

    workbook = Workbook()
    workbook.active.append(["Date", "Amount"])
    workbook.active.append([datetime(2026, 1, 2), None])
    output = io.BytesIO()
    workbook.save(output)
    assert read_table(output.getvalue(), "report.xlsx")[0].values == {
        "Date": "2026-01-02T00:00:00", "Amount": ""
    }


@pytest.mark.parametrize("filename", ["bad.xls", "bad.xlsx", "bad.csv", "bad.pdf"])
def test_invalid_files(filename):
    with pytest.raises(ReportError):
        read_table(b"not a workbook", filename)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1,000.00", "garbage", ""])
def test_invalid_amounts(value):
    with pytest.raises(ValueError):
        money(value)


HEADERS = [
    "Merchant Account", "Batch Number", "Booking Date", "Type", "Currency", "Gross",
    "Net Credit (NC)", "Net Debit (NC)", "Gross Credit (GC)", "Gross Debit (GC)",
    "Psp Reference", "Merchant Reference", "Commission (NC)", "Markup (NC)",
]
ODOO_HEADERS = [
    "Número", "Nombre del socio a mostrar en la factura.", "Fecha de Factura/Recibo",
    "Total en divisa firmado", "Moneda", "Referencia PSP", "Referencia de pedido",
]


def csv_data(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def movement(kind="Settled", credit="9.50", debit="", gross_credit="10", gross_debit="", **changes):
    values = dict(zip(HEADERS, [
        "synthetic-shop", "100", "2026-01-02T23:45:00-05:00", kind, "EUR", "EUR",
        credit, debit, gross_credit, gross_debit, "synthetic-psp", "synthetic-order", "0.50", "",
    ]))
    values.update(changes)
    return [values[header] for header in HEADERS]


def document(number="INV-SYNTHETIC", amount="10", currency="EUR", psp="", order=""):
    return [number, "Synthetic partner", "2026-01-02", amount, currency, psp, order]


def process(rows, docs=None):
    return process_reports(
        csv_data(HEADERS, rows), "synthetic.csv",
        csv_data(ODOO_HEADERS, docs) if docs is not None else None, "synthetic-odoo.csv",
    )


def exported_rows(result):
    return [list(csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
            for content in result.exports.values()]


def test_signs_fees_refund_same_psp_and_no_timezone_shift():
    rows = [
        movement(),
        movement("Refunded", "", "10.25", "", "10", **{"Commission (NC)": "0.25"}),
        movement("Fee", "", "0.75", "", "", **{"Commission (NC)": "0.75"}),
        movement("InvoiceDeduction", "", "1.50", "", ""),
        movement("MerchantPayout", "", "20", "", ""),
    ]
    result = process(rows)
    assert not result.errors
    assert [item.net for item in result.movements] == list(map(Decimal, ["9.50", "-10.25", "-0.75", "-1.50", "-20"]))
    exported = exported_rows(result)[0]
    assert len(exported) == 5
    assert all(item["Fecha"] == "2026-01-02" for item in exported)
    assert exported[2]["Importe"] == "-0.75"
    assert "Commission (NC)=0.75 EUR" in exported[2]["Concepto"]
    assert "fila 6" in exported[4]["Concepto"]
    assert next(iter(result.exports.values())).startswith(b"\xef\xbb\xbfFecha;Concepto;Importe\r\n")


def test_foreign_currency_uses_original_for_match_but_net_for_export():
    result = process([movement(**{"Gross": "GBP", "Net Credit (NC)": "11.75"})], [document(currency="GBP")])
    assert result.movement_review[0]["Estado"] == "candidata"
    exported = exported_rows(result)[0][0]
    assert exported["Importe"] == "11.75"
    assert "Original 10.00 GBP" in exported["Concepto"]
    assert "INV-SYNTHETIC" not in exported["Concepto"]
    wrong_currency = process([movement(**{"Gross": "GBP"})], [document(currency="EUR")])
    assert wrong_currency.movement_review[0]["Estado"] == "sin_coincidencia"


@pytest.mark.parametrize("field,value", [("psp", "synthetic-psp"), ("order", "synthetic-order")])
def test_only_shared_reference_confirms_document(field, value):
    result = process([movement()], [document(**{field: value})])
    assert result.movement_review[0]["Estado"] == "confirmada"
    assert "Documento INV-SYNTHETIC" in exported_rows(result)[0][0]["Concepto"]
    assert process([movement()], [document(amount="11", **{field: value})]).movement_review[0]["Estado"] == "sin_coincidencia"


@pytest.mark.parametrize("rows,docs", [
    ([movement(), movement()], [document()]),
    ([movement()], [document("INV-1"), document("INV-2")]),
    ([movement(), movement()], [document(psp="synthetic-psp")]),
])
def test_repeated_amounts_or_references_are_ambiguous_and_rows_preserved(rows, docs):
    result = process(rows, docs)
    assert {item["Estado"] for item in result.movement_review} == {"ambigua"}
    assert {item["Estado"] for item in result.document_review} == {"ambigua"}
    assert sum(map(len, exported_rows(result))) == len(rows)
    assert all("Documento INV" not in item["Concepto"] for group in exported_rows(result) for item in group)


def test_refund_and_settlement_shared_psp_match_separate_signed_documents():
    result = process([
        movement(), movement("Refunded", "", "10", "", "10"),
    ], [document(psp="synthetic-psp"), document("REF-SYNTHETIC", "-10", psp="synthetic-psp")])
    assert [item["Estado"] for item in result.movement_review] == ["confirmada", "confirmada"]
    assert len(result.movements) == 2


def test_exports_separate_account_batch_currency_and_zip_contains_review():
    result = process([
        movement(), movement(**{"Currency": "USD"}), movement(**{"Batch Number": "101"}),
        movement(**{"Merchant Account": "other-shop"}),
    ])
    assert len(result.exports) == 4
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes())) as archive:
        assert set(archive.namelist()) == {*result.exports, "revision.csv"}
        assert archive.read("revision.csv").startswith(b"\xef\xbb\xbf")
    assert all(len(rows) == 1 for rows in exported_rows(result))


@pytest.mark.parametrize("changes", [
    {"Booking Date": "not a date"}, {"Type": "Unknown"}, {"Currency": "EURO"},
    {"Net Credit (NC)": "NaN"}, {"Net Credit (NC)": "", "Net Debit (NC)": ""},
    {"Merchant Account": ""}, {"Gross": ""}, {"Gross Credit (GC)": ""},
])
def test_row_errors_block_all_exports_instead_of_silent_partial_import(changes):
    result = process([movement(), movement(**changes)])
    assert len(result.movements) == 1
    assert result.errors[0].row == 3
    assert result.errors[0].source == "Adyen"
    assert result.exports == {}
    with pytest.raises(ReportError):
        result.zip_bytes()


def test_invalid_odoo_document_blocks_export_and_identifies_row():
    result = process([movement()], [document(amount="NaN")])
    assert result.errors[0].row == 2
    assert result.errors[0].source == "Odoo"
    assert not result.exports


def test_missing_columns_and_header_only_are_rejected():
    with pytest.raises(ReportError, match="Faltan columnas"):
        process_reports(b"Type;Currency\nSettled;EUR\n", "report.csv")
    with pytest.raises(ReportError, match="no contiene movimientos"):
        process([])


def test_decimal_totals_have_no_float_residue():
    result = process([
        movement(credit="0.10"), movement(credit="0.20"),
        movement("MerchantPayout", "", "0.30", "", ""),
    ])
    assert result.totals()[0]["Neto"] == "0.00"
    assert result.totals()[0]["MerchantPayout"] == "-0.30"


def test_real_legacy_xls_reader_with_synthetic_data():
    import xlwt

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Synthetic")
    for row_index, row in enumerate([HEADERS, movement()]):
        for column_index, value in enumerate(row):
            sheet.write(row_index, column_index, value)
    output = io.BytesIO()
    workbook.save(output)
    result = process_reports(output.getvalue(), "synthetic.xls")
    assert len(result.movements) == 1
    assert result.movements[0].net == Decimal("9.50")


def test_clear_upload_results_removes_previous_download_data(monkeypatch):
    from types import SimpleNamespace
    import adyen_reconciliation_ui as ui

    state = {ui.RESULT_KEY: process([movement()]), ui.SOURCE_KEY: "old", "auth_role": "admin"}
    monkeypatch.setattr(ui, "st", SimpleNamespace(session_state=state))
    ui.clear_results()
    assert state == {"auth_role": "admin"}


def test_streamlit_rejects_non_admin_and_process_requires_upload():
    from streamlit.testing.v1 import AppTest

    script = "from adyen_reconciliation_ui import render_reconciliation_page\nrender_reconciliation_page()"
    denied = AppTest.from_string(script).run()
    assert not denied.exception
    assert "administrador" in denied.error[0].value
    assert not denied.button
    allowed = AppTest.from_string(script)
    allowed.session_state["auth_role"] = "admin"
    allowed.run()
    assert not allowed.exception
    allowed.button[0].click().run()
    assert "Sube un informe" in allowed.error[0].value


def test_streamlit_process_preview_downloads_and_content_change_clear_results():
    from streamlit.testing.v1 import AppTest

    content = csv_data(HEADERS, [movement()])
    script = (
        "import streamlit as st\n"
        "from types import SimpleNamespace\n"
        "from adyen_reconciliation_ui import render_reconciliation_page\n"
        f"content = st.session_state.get('test_content', {content!r})\n"
        "st.file_uploader = lambda label, **kwargs: "
        "SimpleNamespace(name='synthetic.csv', getvalue=lambda: content) if 'Adyen' in label else None\n"
        "render_reconciliation_page()\n"
    )
    app = AppTest.from_string(script)
    app.session_state["auth_role"] = "admin"
    app.run()
    app.button[0].click().run()
    assert not app.exception
    assert len(app.dataframe) == 4
    assert len(app.get("download_button")) == 3
    app.session_state["test_content"] = csv_data(HEADERS, [movement(credit="8")])
    app.run()
    assert not app.exception
    assert not app.get("download_button")
    assert not app.dataframe


def test_document_counts_are_not_movement_counts_for_repeated_amounts():
    from collections import Counter

    rows, docs = [], []
    for index in range(51):
        amount = str(1000 + index)
        rows.append(movement(gross_credit=amount))
        docs.append(document(f"INV-UNIQUE-{index}", amount))
    for index in range(12):
        amount = str(2000 + index)
        rows.extend(movement(gross_credit=amount) for _ in range(5 if index < 6 else 4))
        docs.extend(document(f"INV-AMBIGUOUS-{index}-{copy}", amount) for copy in range(2))
    docs.extend(document(f"UNMATCHED-{index}", str(3000 + index)) for index in range(11))
    docs.extend(document(f"REF-UNMATCHED-{index}", str(-3000 - index)) for index in range(11))
    rows.extend(movement(gross_credit=str(4000 + index)) for index in range(20))
    result = process(rows, docs)
    assert len(result.documents) == 97
    assert sum(doc.amount > 0 for doc in result.documents) == 86
    assert sum(doc.amount < 0 for doc in result.documents) == 11
    assert Counter(item["Estado"] for item in result.document_review) == {
        "candidata": 51, "ambigua": 24, "sin_coincidencia": 22,
    }
    assert sum(item["Estado"] == "sin_coincidencia" for item in result.movement_review) == 20
    assert sum(map(len, exported_rows(result))) == 125


def test_six_original_gbp_payments_are_preserved_in_eur_export():
    result = process([movement(**{"Gross": "GBP", "Net Credit (NC)": "11.75"}) for _ in range(6)])
    assert len(result.movements) == 6
    assert len(result.exports) == 1
    assert result.totals()[0]["Moneda"] == "EUR"
    assert all("Original 10.00 GBP" in row["Concepto"] for row in exported_rows(result)[0])


def test_invalid_csv_row_length_identifies_source_row():
    with pytest.raises(ReportError, match="Fila 2"):
        read_table(b"Type;Amount\nSettled;10;extra\n", "bad.csv")