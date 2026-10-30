"""The 14 reference competitors and what Vytria must provide for each one to obtain verified prices.

Facts come from the checks recorded in ``docs/Precios/cobertura_competidores.json`` (2026-10-30).
No competitor has a verified authorised connector: a public URL or a permissive robots.txt is not
an authorisation for automated queries.
"""
from __future__ import annotations

CHECKED_ON = "2026-10-30"

COMPETITORS = (
    ("Óptica Óptima", "https://optica-optima.com/"),
    ("Óptica Bassol", "https://www.opticabassol.com/"),
    ("Blickers", "https://www.blickers.com/es-es/"),
    ("Visual-Click", "https://visual-click.com/"),
    ("Edel-Optics", "https://www.edel-optics.es/"),
    ("Amevista", "https://www.amevista.com/es/"),
    ("Mia Burton", "https://miaburton.com/es"),
    ("Optical H", "https://opticalh.com/"),
    ("Domanci", "https://domanci.com/es/"),
    ("Óptica Universitaria", "https://www.opticauniversitaria.es/"),
    ("ConGafasDeSol", "https://www.congafasdesol.com/"),
    ("Céline Opticien Lunetier", "https://www.celine-opticien-lunetier.com/es"),
    ("Visio Factory", "https://www.visiofactory.es/"),
    ("Otticait Eyewear", "https://www.otticait.com/es/"),
)

_ROBOTS = ("robots.txt prohíbe la búsqueda interna; sin buscador no se pueden localizar fichas.")
_FEED = ("Autorización escrita del competidor y, si existe, la URL de su feed/API/CSV (con credenciales "
         "si las exige), o el contacto comercial que pueda concederla.")
_FICHA = ("Confirmar que los términos de uso permiten consultar fichas de forma automatizada, y facilitar "
          "las URL exactas de las fichas (o su patrón) de los EAN a comparar.")

# name -> (restrictions, what Vytria must provide)
REQUIREMENTS = {
    "Óptica Óptima": (_ROBOTS, _FEED),
    "Óptica Bassol": (_ROBOTS + " Respuesta 429 en la comprobación inicial.", _FEED),
    "Blickers": ("Sin regla de búsqueda en robots.txt; términos de uso no revisados.",
                 _FICHA + " Confirmar además que publica EAN o código de color/calibre."),
    "Visual-Click": (_ROBOTS + " Respuesta 429 (límite de consultas).", _FEED),
    "Edel-Optics": ("La búsqueda devolvió 403 (bloqueo de acceso); no se reintenta ni se elude.",
                    "Autorización escrita de Edel-Optics y alta de la IP/agente de Vytria en su lista de "
                    "accesos, o la URL de su feed/API de afiliados."),
    "Amevista": ("Sin regla de búsqueda en robots.txt; términos de uso no revisados.", _FICHA),
    "Mia Burton": (_ROBOTS, _FEED),
    "Optical H": ("Sin regla de búsqueda en robots.txt; respuesta 429 en la comprobación inicial.", _FICHA),
    "Domanci": (_ROBOTS, _FEED),
    "Óptica Universitaria": (_ROBOTS, _FEED),
    "ConGafasDeSol": (_ROBOTS, _FEED),
    "Céline Opticien Lunetier": ("Sin regla de búsqueda en robots.txt; respuesta 429 en la portada.", _FICHA),
    "Visio Factory": ("Las fichas publican EAN (JSON-LD gtin13) y precio con IVA, pero robots.txt prohíbe la "
                      "búsqueda y el patrón de URL no es deducible.",
                      "Autorización para consultar fichas de forma automatizada y la lista de URL exactas de "
                      "las fichas de los EAN a comparar."),
    "Otticait Eyewear": (_ROBOTS, _FEED),
}

assert len(COMPETITORS) == len(REQUIREMENTS) == 14

_NONE = ("Ninguno permitido localizado", "no_authorized_connector")
_FICHAS = ("Fichas públicas (no evaluadas)", "pending_authorization")
# name -> (method, integration status) as checked on CHECKED_ON; never 'operational' without proof.
METHOD_STATUS = {
    **{name: _NONE for name in ("Óptica Óptima", "Óptica Bassol", "Visual-Click", "Mia Burton", "Domanci",
                                "Óptica Universitaria", "ConGafasDeSol", "Otticait Eyewear")},
    **{name: _FICHAS for name in ("Blickers", "Amevista", "Optical H", "Céline Opticien Lunetier")},
    "Edel-Optics": ("Búsqueda pública", "blocked"),
    "Visio Factory": ("Fichas públicas con URL conocida (JSON-LD schema.org Product con gtin13)", "manual_verified"),
}


def requirement_for(name: str) -> tuple[str, str]:
    return REQUIREMENTS.get(name, ("", "Indicar el mecanismo autorizado para este competidor."))
