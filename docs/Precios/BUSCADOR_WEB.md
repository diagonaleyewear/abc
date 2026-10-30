# Buscador web de precios de competidores

Consulta fichas públicas de producto de los 14 competidores, comprueba que son el producto exacto y registra el
precio con el sistema existente (`import_offers`, histórico, gráficos). Código: `price_web.py`,
`price_web_extract.py`, `price_web_fetch.py`. **No hay planificador activo, ni servicio Docker nuevo, ni cron.**

## Qué hace y qué no

| Hace | No hace |
|---|---|
| Lee `robots.txt` (1 petición) y solo consulta si lo permite | Eludir 403/429/CAPTCHA, rotar IP, usar proxies |
| Fichas conocidas → sitemap público → buscador interno (si está configurado y permitido) | Contratar un buscador externo (gancho preparado, sin proveedor) |
| Extrae JSON-LD > metadatos > selectores configurados | Navegador automatizado |
| Pausa entre peticiones, máximo de peticiones y reintento solo por error de conexión | Insistir en un sitio bloqueado: queda `blocked` hasta intervención humana |

## Reglas de fiabilidad

- **Coincidencia**: EAN publicado idéntico, o modelo + color + calibre sin EAN → *exacta*. Color o calibre distinto,
  EAN distinto o ficha con varias ofertas → *probable* o *descartada*; nunca entra en comparaciones.
- **IVA**: solo «incluido» si la ficha lo declara, si publica el precio sin IVA y la proporción es un tipo español
  (1,21 / 1,10 / 1,04), o si el competidor tiene «IVA incluido» verificado. Si no, la oferta se guarda pero no compara.
- **Mercado y moneda**: se toman de cada ficha (`<html lang="es-ES">`, `priceCurrency`) o del competidor si una persona
  los confirmó; nunca del dominio. Sin ellos la oferta no se registra.
- **Precio**: el precio de venta efectivo; el tachado/anterior se guarda aparte.
- **Sin stock** o precio caducado: se muestran, no comparan, y no se recupera un precio antiguo.
- **Bloqueo** (401/403/407/429/CAPTCHA): se detiene la ejecución, el competidor queda `blocked` y no se vuelve a consultar.

## Estados de un conector

`implementado` (código y configuración) → `verificado` (al menos una oferta real, exacta y comparable) →
`operativo` (verificado + condiciones de uso revisadas por una persona con nota + `robots.txt` permitido).
Solo `operativo` puede asociarse al planificador (`WebFichaConnector.verify`).

## Uso

Interfaz: **Comparador de precios → Comparativa → «Buscar precios en la competencia»** (EAN, marca+modelo+color,
modelo+color o referencia; «Almacenados» no consulta webs; «Consultar webs» exige 1–3 competidores y ≤5 productos) y
**Competidores → «Consulta web de fichas públicas»** (robots.txt, métodos, plantilla de búsqueda, selectores, IVA,
pausa, límites, revisión de condiciones, fichas conocidas).

Comandos (dentro del contenedor actual; `PRICE_COMPARATOR_DB_PATH` apunta a la base):

```text
python price_web.py status
python price_web.py check-access --competitor "Visio Factory"
python price_web.py add-url --competitor "Visio Factory" --ean 8056597875349 --url https://www.visiofactory.es/...
python price_web.py search --ean 8056597875349 --competitor "Visio Factory" [--dry-run]
```

Límites del comando: 5 EAN y 3 competidores por ejecución. Código de salida 0 = correcto, 1 = bloqueo o error, 2 = uso.
Para probar sin tocar producción: `PRICE_COMPARATOR_DB_PATH=/tmp/prueba.sqlite3`.

Detener / recuperar: no hay proceso residente. Si un sitio queda `blocked`, revise la causa y solo entonces ejecute
`check-access` de nuevo. Una consulta manual y el planificador comparten el bloqueo por competidor
(`<db>.competitor-<id>.lock`); si hay otra ejecución, la nueva se rechaza.

## Integración con el planificador

`price_scheduler.py` incluye el conector `web_ficha` (fuente: URL base del competidor). Sigue desactivado: habilitarlo
exige `terms_reviewed` con nota, `robots.txt` permitido y una oferta real previa. Frecuencia, pausas y límites son los
del propio planificador (ver `PLANIFICADOR_COMPETIDORES.md`).

## Estado real (2026-10-30, base temporal)

- `robots.txt` permite la raíz en los 14 sitios; ninguno tiene las condiciones de uso revisadas → ninguno es operativo.
- Visiofactory: 3 fichas Prada con JSON-LD, EAN idéntico, IVA 21 % demostrado y mercado ES declarado por la página.
  239 € / 187 € (sin stock) / 219 €.
- Resto de competidores: acceso comprobado, fichas no localizadas todavía (sin fichas conocidas ni sitemap probado).
