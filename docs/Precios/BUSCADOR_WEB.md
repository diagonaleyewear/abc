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

- **Coincidencia**: EAN publicado idéntico verificado con marca, modelo y código de color confirmados → *exacta*;
  el calibre vacío o distinto se registra como información, no bloquea. Sin EAN verificado se exige marca +
  modelo + código de color + calibre coincidentes. EAN distinto, modelo/color incorrectos o identificación
  ambigua → *probable* o *descartada*. Las denominaciones comerciales no sustituyen códigos de fabricante.
  [Regla y trazabilidad](../PRICE_IDENTITY.md).
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

**Comprobar robots.txt** es una operacion de configuracion independiente del
piloto: admite cualquier competidor registrado aun con piloto guardado o
activo. Solo lee `/robots.txt` del origen HTTPS validado de esa tienda, una
peticion sin reintentos/redirecciones. No descarga fichas, no autoriza
consultas comerciales ni levanta bloqueos anteriores. Resultado y fecha
quedan guardados. Las consultas de productos mantienen su barrera del
piloto. [Detalles, riesgos y pruebas](../PRICE_ROBOTS_CONFIGURATION.md).

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

Detener / recuperar: no hay proceso residente. Si un sitio queda `blocked`, no se reintenta con
`check-access`: revise la causa y siga el procedimiento autorizado de configuracion antes de
comprobar de nuevo. Una consulta manual y el planificador comparten el bloqueo por competidor
(`<db>.competitor-<id>.lock`); si hay otra ejecución, la nueva se rechaza.

## Integración con el planificador

`price_scheduler.py` incluye el conector `web_ficha` (fuente: URL base del competidor). Sigue desactivado: habilitarlo
exige `terms_reviewed` con nota, `robots.txt` permitido y una oferta real previa. Frecuencia, pausas y límites son los
del propio planificador (ver `PLANIFICADOR_COMPETIDORES.md`).

## Colaboración manual: facilitar la URL de una ficha

Si la búsqueda (EAN, marca + modelo + color, sitemap/búsqueda interna permitidos) no localiza el producto, la
interfaz muestra «No hemos localizado este producto en [competidor]. ¿Puedes facilitarnos la URL?» con un campo y el
botón «Verificar y guardar». Se pide una sola vez por producto y competidor.

1. Validación sin red: HTTPS, dominio del competidor (no vale `visiofactory.es.otro.com`), sin credenciales (`@`),
   sin puerto distinto de 443, sin IP ni nombres locales. En la consulta real se vuelve a comprobar que el DNS no
   apunte a direcciones privadas y que una redirección no salga del dominio.
2. Acceso: si el sitio no está comprobado se lee `robots.txt`; si no permite consultar o hay bloqueo previo
   (403/429/CAPTCHA) la URL se guarda **pendiente de verificación** y no se consulta ni se insiste.
3. Se lee la ficha una vez y se compara con el producto pedido (EAN publicado, marca, modelo, color, calibre).
   - Exacta → «URL verificada y guardada»; se registra la oferta por el importador habitual y la URL se reutiliza.
   - Probable o ambigua → se pide confirmación; al confirmar se guarda como referencia pendiente de revisión, sin
     oferta comparable.
   - Producto distinto → «Coincidencia incorrecta»; no se guarda como válida y se puede facilitar otra URL.
4. Facilitar una URL nunca la marca como verificada por sí solo. «Añadir ficha conocida» (Competidores) y el CLI
   `add-url` dejan la URL pendiente hasta que una búsqueda la verifique.
5. Duplicados: la misma URL (ignorando `#fragmento`) no se registra dos veces. Una segunda URL distinta exige
   «Sustituir una ficha registrada».
6. Si una ficha deja de responder queda «necesita reubicación» y se vuelve a pedir; la nueva sustituye a la anterior
   (`replaced`) y el histórico de precios (`offers`) no se toca.

Estados en la columna «Ficha registrada»: Ficha localizada automáticamente · URL aportada por el usuario, pendiente de
verificar · URL verificada y guardada · Coincidencia incorrecta · Acceso bloqueado. Se guardan en
`competitor_product_urls` (columnas `source` = auto/user, `status`, `note`).

## Estado real (2026-10-30, base temporal)

- `robots.txt` permite la raíz en los 14 sitios; ninguno tiene las condiciones de uso revisadas → ninguno es operativo.
- Visiofactory: 3 fichas Prada con JSON-LD, EAN idéntico, IVA 21 % demostrado y mercado ES declarado por la página.
  239 € / 187 € (sin stock) / 219 €.
- Resto de competidores: acceso comprobado, fichas no localizadas todavía (sin fichas conocidas ni sitemap probado).

## Estados por competidor (pestaña Competidores)

Tres dimensiones independientes, calculadas con los campos existentes (`competitor_product_urls`, `competitor_web_config`,
`offers`); no hay migraciones nuevas.

| Dimensión | Valores |
|---|---|
| Localización | Sin ficha · URL candidata (probable, pendiente, retirada o descartada) · Ficha verificada |
| Extracción | Sin probar · Verificada con datos reales (ficha verificada con precio) · Incompleta (sin precio o solo probable) · Fallida (ficha descartada o retirada) |
| Operatividad | Pendiente de revisión de condiciones · Autorizado (condiciones revisadas por una persona y robots.txt permite) · Bloqueado (403/429/CAPTCHA) · No operativo (robots.txt) |

La extracción correcta no autoriza consultas periódicas. La tabla muestra además «Ofertas» (EAN con oferta registrada) y
«Comparables» (vigentes, con identificación, IVA, mercado, moneda y stock confirmados). Hoy ninguno está «Autorizado».

Las fichas importadas con `price_known_urls.py` aparecen como «Ficha candidata conocida, pendiente de verificar» y no cuentan
como ofertas ni como competidor con precio. Ver [IMPORTAR_FICHAS.md](IMPORTAR_FICHAS.md).

## Mensajes cuando falla una URL

`Submission.reason` y `Submission.action` (también `Outcome.reason`) indican la causa y qué hacer. Cada mensaje explica
qué ha pasado.

| Causa | Qué ocurre con la URL | Acción |
|---|---|---|
| `gone` (404/410) | No se guarda | Facilitar otra URL |
| `wrong_product` / `variant_differs` / `ean_conflict` | Se guarda como descartada; el histórico no se toca | Facilitar la URL correcta (sustituye) |
| `ambiguous` | Pide confirmación; solo referencia pendiente | Confirmar o descartar |
| `no_product_data` | Se conserva pendiente | Revisión del extractor; no se pide otra URL |
| `no_price` | Ficha verificada, sin oferta | Revisión del extractor; no se pide otra URL |
| `vat_unconfirmed` | Ficha verificada; oferta no comparable | Confirmar IVA en la web y en el competidor |
| `market_currency` | Ficha verificada; oferta no comparable | Confirmar mercado y moneda (EUR/ES) |
| `robots` / `authorization` | Se conserva pendiente; no se consulta | Esperar a la autorización |
| `http_blocked` (403/429) / `captcha` | Se conserva pendiente; no se reintenta ni es «producto inexistente» | Esperar |
| `temporary` (conexión, 5xx) | No se guarda nada | Reintentar manualmente más tarde |
| `unchecked` | Se conserva pendiente | Repetir la comprobación de robots.txt |

Cuando la acción es revisión, espera o autorización la interfaz oculta el campo de URL. Tests: `tests/test_price_web_failures.py`.

## Validación de 14 fichas candidatas (2026-10-30, una consulta por URL, base temporal)

Estados independientes: ficha localizada / extracción verificada / operativo. Ninguno es operativo.

| Competidor | Ficha | Extracción (PR 27ZS 16K08Z salvo indicación) | Comparable hoy |
|---|---|---|---|
| Visiofactory | sí | 239 €, stock, IVA y ES demostrados, EAN | sí |
| Óptica Óptima | sí | 273 €, stock, IVA (precio sin IVA) y ES, EAN | sí |
| Domanci | sí | 240,5 €, stock, IVA y ES, EAN | sí |
| Amevista | sí | 273 €, stock, EAN; sin IVA ni mercado | no (IVA/mercado sin confirmar) |
| Edel-Optics | sí | 243,5 €, stock, EAN; sin IVA ni mercado; PVP 390 € ignorado | no |
| Otticait | sí | 241,8 €, stock, EAN; sin IVA ni mercado | no |
| ConGafasDeSol | sí | 253,5 €, stock, EAN; sin IVA ni mercado | no |
| Blickers | sí | 273 €, stock, EAN; sin IVA ni mercado | no |
| Mia Burton | URL de PR 26ZS (EAN 8056597875226, 247 €) | no es la referencia buscada | no; falta URL de PR 27ZS |
| Óptica Universitaria | sí | EAN en `data-ean`; precio cargado con JavaScript | no; requiere extractor específico |
| Óptica Bassol, Visual-Click, Optical H, Céline | URL guardada | HTTP 429, acceso bloqueado, sin reintento | no; pendiente |

Céline puede mostrar varias variantes: no se usa ninguna hasta verificar color, calibre y EAN.
El IVA y el mercado se confirman por oferta (JSON-LD, precio sin IVA publicado, o `tax_policy`/`confirm_market`
decididos por una persona); nunca por el dominio. Tests: `tests/test_price_web_competitor_pages.py` con fixtures en
`tests/fixtures/web_pages/`.
