"""Streamlit view for the offline Adyen reconciliation workflow."""

import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict
from decimal import Decimal

import streamlit as st

from adyen_reconciliation import ReportError, concept, decimal_text, process_reports


RESULT_KEY = "adyen_reconciliation_result"
SOURCE_KEY = "adyen_reconciliation_sources"


def clear_results() -> None:
    st.session_state.pop(RESULT_KEY, None)
    st.session_state.pop(SOURCE_KEY, None)


def _upload_signature(upload):
    if upload is None:
        return None
    return upload.name, hashlib.sha256(upload.getvalue()).hexdigest()


def render_reconciliation_page() -> None:
    if st.session_state.get("auth_role") != "admin":
        clear_results()
        st.error("Este apartado requiere un usuario administrador.")
        st.stop()

    st.title("Conciliación Odoo")
    adyen = st.file_uploader(
        "Informe de liquidación Adyen", type=["xls", "xlsx", "csv"],
        key="adyen_settlement_upload", on_change=clear_results,
    )
    odoo = st.file_uploader(
        "Facturas y abonos Odoo (opcional)", type=["xls", "xlsx", "csv"],
        key="adyen_odoo_upload", on_change=clear_results,
    )
    adyen_bytes = adyen.getvalue() if adyen is not None else None
    odoo_bytes = odoo.getvalue() if odoo is not None else None
    sources = tuple(_upload_signature(upload) for upload in (adyen, odoo))
    if sources != st.session_state.get(SOURCE_KEY):
        clear_results()
        st.session_state[SOURCE_KEY] = sources

    if st.button("Procesar", type="primary"):
        st.session_state.pop(RESULT_KEY, None)
        if adyen is None:
            st.error("Sube un informe Adyen antes de procesar.")
        else:
            try:
                st.session_state[RESULT_KEY] = process_reports(
                    adyen_bytes, adyen.name, odoo_bytes, odoo.name if odoo is not None else "",
                )
            except ReportError as exc:
                st.error(str(exc))

    render_import_guidance()
    result = st.session_state.get(RESULT_KEY)
    if result is not None:
        render_result(result, odoo is not None, sources)


def render_import_guidance() -> None:
    st.info(
        "En Odoo selecciona el diario de la pasarela Adyen, no el diario del banco receptor. "
        "Importa cada CSV en el diario de la cuenta y moneda de liquidación indicadas en su nombre. "
        "Si hay EUR y USD, usa diarios compatibles con cada moneda; no importes ambos como EUR. "
        "MerchantPayout es una salida negativa de la pasarela."
    )
    st.write("Mapeo: Fecha → Fecha, Concepto → Etiqueta/Concepto, Importe → Importe.")
    st.warning(
        "Ejecuta Probar antes de Importar. La versión de Odoo de esta instalación no está "
        "identificada y la importación no se ha validado en ella. Comprueba el importador "
        "de movimientos y el diario en una copia de pruebas antes de usarlo en producción."
    )
    st.caption(
        "Las facturas exportadas no demuestran que estén cobradas. Una candidata no es una "
        "asociación confirmada. No se escribe en Odoo ni se concilia automáticamente."
    )

def render_result(result, has_odoo: bool, sources: tuple) -> None:
    if result.errors:
        st.error("Hay errores por fila. No se generan archivos de importación hasta corregirlos.")
        st.dataframe([asdict(error) for error in result.errors], hide_index=True, use_container_width=True)

    st.subheader("Movimientos")
    preview = []
    for movement, match in zip(result.movements, result.movement_review, strict=True):
        preview.append({
            "Fila": movement.row, "Cuenta": movement.account, "Lote": movement.batch,
            "Fecha": movement.date, "Tipo": movement.kind, "Moneda liquidación": movement.currency,
            "Importe neto": decimal_text(movement.net),
            "Bruto original": decimal_text(movement.gross) if movement.gross is not None else "",
            "Moneda original": movement.original_currency,
            "Gastos informados (no sumar)": "; ".join(
                f"{name}: {decimal_text(amount)} {movement.currency}" for name, amount in movement.expenses
            ),
            "Estado documento": match["Estado"], "Concepto": concept(movement, match["Documento confirmado"]),
        })
    st.dataframe(preview, hide_index=True, use_container_width=True)
    st.subheader("Totales por moneda")
    totals = defaultdict(lambda: {"count": 0, "net": Decimal(0)})
    for movement in result.movements:
        totals[movement.currency]["count"] += 1
        totals[movement.currency]["net"] += movement.net
    st.dataframe([
        {"Moneda": currency, "Movimientos": values["count"], "Neto": decimal_text(values["net"])}
        for currency, values in totals.items()
    ], hide_index=True, use_container_width=True)
    st.dataframe(result.totals(), hide_index=True, use_container_width=True)
    if result.errors:
        st.caption("Los totales anteriores solo incluyen las filas válidas; no son un resultado importable.")

    st.subheader("Revisión de documentos")
    if has_odoo:
        counts = Counter(item["Estado"] for item in result.document_review)
        st.write({
            "Documentos": len(result.documents),
            "Facturas (total positivo)": sum(doc.amount > 0 for doc in result.documents),
            "Abonos (total negativo)": sum(doc.amount < 0 for doc in result.documents),
            "Documentos de total cero": sum(doc.amount == 0 for doc in result.documents),
            **counts,
            "Movimientos Adyen sin documento": sum(
                item["Estado"] == "sin_coincidencia" for item in result.movement_review
            ),
        })
        st.dataframe(result.document_review, hide_index=True, use_container_width=True)
    else:
        st.caption("Sin exportado Odoo: los movimientos quedan pendientes de revisión documental.")
    st.dataframe(result.movement_review, hide_index=True, use_container_width=True)

    if result.errors or not result.exports:
        return
    st.subheader("Descargas")
    download_id = hashlib.sha256(repr(sources).encode()).hexdigest()[:16]
    for filename, content in result.exports.items():
        st.download_button(
            filename, content, file_name=filename, mime="text/csv",
            key=f"adyen_download_{download_id}_{filename}",
        )
    st.download_button(
        "Descargar revisión", result.review_csv(), file_name="revision.csv", mime="text/csv",
        key=f"adyen_review_{download_id}",
    )
    st.download_button(
        "Descargar todos (ZIP)", result.zip_bytes(), file_name="conciliacion_adyen.zip",
        mime="application/zip", key=f"adyen_zip_{download_id}",
    )