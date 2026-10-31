import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

import cloud_design as design

ROOT = Path(__file__).resolve().parents[1]
# Pantallas ya migradas: la cabecera de página sale del sistema de diseño.
MIGRATED = (
    "app_enhanced.py",
    "app_pages/alerts.py",
    "app_pages/luxoptica.py",
    "adyen_reconciliation_ui.py",
    "kering_images_ui.py",
)
# `st.title` sigue permitido en el login y en la barra lateral (no son cabeceras de página).
ALLOWED_TITLE_LINES = {
    'st.title(APP_TITLE)',
    'st.title("Configuración")',
}


def test_migrated_pages_use_the_shared_header():
    offenders = []
    for name in MIGRATED:
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            code = line.strip()
            if re.match(r"st\.title\(", code) and code not in ALLOWED_TITLE_LINES:
                offenders.append(f"{name}:{number}")
    assert not offenders, f"Usa cloud_design.page_title en lugar de st.title: {offenders}"


def test_page_title_renders_escaped_h1_with_styles_and_optional_parts():
    app = AppTest.from_string(
        "import cloud_design as d\n"
        "d.page_title('Tabla <b>X</b>', 'Subtítulo', (('3', 'filas'),))"
    )
    app.run()
    assert not app.exception
    markup = " ".join(item.value for item in app.get("html"))
    assert "<style>" in markup
    assert '<h1 class="cd-header__title">Tabla &lt;b&gt;X&lt;/b&gt;</h1>' in markup
    assert "Subtítulo" in markup and "<strong>3</strong>filas" in markup
    assert design.page_header_html("Solo título").count("cd-header__sub") == 0
