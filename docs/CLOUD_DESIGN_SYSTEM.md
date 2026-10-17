# Cloud Diagonal — Design System (v0, piloto Biblioteca de imágenes)

Alcance de esta primera entrega: tokens y componentes compartidos usados por la **Biblioteca de imágenes**
(`/repositorio-imagenes`). El resto de pantallas se migra solo tras la aprobación visual del piloto (issue #46).

## Principios

- Producto primero: la unidad visual es el producto/EAN, no el archivo.
- Información comercial en primer plano; nombres de archivo, checksum y rutas solo en «Detalles técnicos» (admin).
- Estilos propios `cd-*`; **nunca** selectores internos de Streamlit (`data-testid`, `st*`).
- Estado nunca solo por color: texto y marca (✓ / –).
- Todo lo interactivo es un widget nativo de Streamlit (teclado, foco y lectores de pantalla incluidos).

## Tokens (`cloud_design.TOKENS`)

Valores tomados del tema público de vytria.com (`theme.css`, `custom.css`):

| Token | Valor | Origen |
|---|---|---|
| `text` | `#42403B` | texto base Vytria |
| `tint` | `#fbfbfa` | fondo suave Vytria |
| `neutral` | `#f2f2f2` | fondos neutros (miniaturas, chips) |
| `border` | `#d9d9d9` | bordes |
| `accent` | `#003399` | azul de enlaces/acento (contraste 9,7:1 sobre blanco) |
| `highlight` | `#FFCB03` | acento de aviso |
| `radius` | 0 / 4 px | Vytria usa esquinas casi rectas |
| fuente cuerpo | Arial / sans-serif | `font-family` real del tema |
| fuente títulos | Cormorant Garamond → Times New Roman | Cormorant se usa en Vytria; **el fichero de fuente no se incluye** (licencia sin verificar), se usa fallback serif con cifras lining (Georgia usa cifras antiguas y deforma referencias como GU1060S) |

Valores propios (no extraídos): `success #1e6b3a`, `warning #7a5200`, `danger #b3361a` (elegidos para contraste AA sobre
blanco), escala de espaciado 4/8/16/24/32, altura de control 40 px (objetivo táctil 44 px), breakpoints 640/1024 px.

## Componentes

| Componente | Función | Notas |
|---|---|---|
| Cabecera | `page_header_html` | título, subtítulo y contadores (imágenes, EAN, modelos) |
| Tarjeta de producto | `product_card_html` | miniatura 4:3 `object-fit: contain` sobre fondo neutro, alturas uniformes, textos truncados con elipsis |
| Insignias de vista | `view_badges_html` | V1/V2/V3 presentes o ausentes |
| Galería / foto | `photo_html`, `caption_html` | misma proporción que las tarjetas |
| Avisos | `notice_html` | `info`, `success`, `warning`, `error` (`role=status/alert`) |
| Estado vacío | `empty_state_html` | sin resultados / biblioteca vacía |
| Paginación | `pager_text` + botones nativos | «1–24 de 960 productos · Página 1 de 40» |
| Búsqueda y filtros | widgets nativos `text_input` / `selectbox` | claves `lib_*` persistentes |

Los constructores devuelven HTML escapado y son comprobables sin Streamlit.

## Biblioteca de imágenes: reglas funcionales

- Agrupación por EAN (texto, conserva ceros iniciales). Contadores: imágenes = registros de imagen, EAN = productos distintos, modelos = modelos **verificados** distintos (nombre comercial o modelo/referencia no técnicos).
- Vistas: se usa solo la clasificación almacenada (`frontal`=V1, `perspectiva`=V2, `lateral`=V3, `detalle`). No se reclasifica; el detalle nunca se
  convierte en V2 (regla posicional Kering intacta). Las vistas ausentes se avisan.
- Miniatura de portada: V1 original si existe; en otro caso la primera vista por orden.
- Paginación por producto (12/24/48), solo se valida y genera miniatura de las portadas visibles; el detalle solo carga las fotos del producto y
  el original seleccionado.
- ZIP bajo demanda (`zip_eans_for_user`, comprueba rol) y descarga individual con el nombre original. Sin consultas a Odoo ni a proveedores.

## Modo claro y oscuro (issue #53)

Causa raíz: los estilos usaban colores fijos del tema claro (`ink #000`, texto `#42403B`). La cabecera no pinta fondo propio y
queda sobre el fondo de Streamlit; en modo oscuro el título era negro sobre `#0e1117`, mientras que chips y tarjetas pintaban su propia
superficie clara (indicadores incoherentes).

Solución: `TOKENS["color"]` (claro) y `TOKENS["color_dark"]` (oscuro). `build_styles("auto")` genera CSS con
`light-dark(claro, oscuro)`; Streamlit fija `color-scheme` en `.stApp`, por lo que el navegador elige la paleta sin recargar ni depender del
servidor (`st.context.theme.type` no es fiable). El marco de la fotografía es siempre claro (`#f4f4f2`) para que fotos con fondo blanco, negro o
transparente se vean completas y sin modificar el archivo.

Contraste (WCAG AA ≥ 4,5:1), verificado en tests por `CONTRAST_PAIRS` para ambas paletas: ink/text/muted sobre página, tarjeta, neutro y tint;
acento sobre tarjeta y `accent_soft`; éxito/aviso/peligro sobre tint; texto del marco sobre el marco.

## Identificación comercial

Orden de resolución (`ProductGroup.title`): 1) denominación comercial verificada (`nombre_comercial`, `denominacion_comercial`, `product_name`);
2) modelo/referencia verificados (`modelo`, `identity.model`, `referencia`); 3) identificación neutral por EAN («Modelo no disponible» + EAN).
La marca se muestra aparte, junto al proveedor. Se descartan como modelo: nombres de archivo del propio registro, extensiones, `__`, rutas,
palabras de vista (frontal, lateral…), marcadores (`n/a`) y cifras tipo EAN. Nunca se inventan datos de catálogo.

Origen de `frontal.img`: `graph_mail_downloader._model_from_image_name` (Luxoptica) y `migrate_image_repository` guardan en `metadata["modelo"]`
el prefijo anterior a `__` del nombre del archivo; si no hay `__`, guardan el nombre completo (`frontal.img`). Kering sí guarda el modelo del
portal (`identity.model`). Odoo `product_name` solo existe en líneas de pedido, no en los metadatos de imagen.

## Tarjetas y responsive

Rejilla: `st.container(horizontal=True, wrap=True, key="lib_grid")` + CSS `grid-template-columns: repeat(auto-fill, minmax(min(100%, 200px), 1fr))`
(clase `st-key-lib_grid`, única excepción documentada a «solo clases `cd-*`»; son claves creadas por nosotros). Cada ficha es
`st.container(border=True, key="lib_card_<ean>")` con el HTML y el botón «Ver fotografías» dentro. Filtros: `lib_filters` (mínimo 150 px),
miniaturas del detalle: `lib_thumbs`. Con barra lateral abierta: 4 columnas a 1440, 2 a 1024 y 768, 1 a 375; sin desbordamiento horizontal.

## Diagnóstico de estadísticas

`python scripts/diagnose_image_library.py` (solo lectura) imprime, por proveedor: imágenes, EAN, EAN con modelo verificado, EAN con valor solo
técnico, EAN sin modelo y modelos distintos. Las cifras de producción (4.774 imágenes · 274 EAN · 62 modelos) provienen de: registros de imagen,
EAN distintos y valores `modelo` distintos no vacíos (antes sin filtrar nombres de archivo, por lo que 62 estaba sobreestimado en Luxoptica). El
valor corregido y el reparto por proveedor deben leerse ejecutando el script en producción; no se ha modificado ningún dato original.

## Miniaturas

La biblioteca no usa la miniatura genérica de 360×240: `image_thumbnails.generate(..., background="#f4f4f2")` convierte siempre a RGBA
(un PNG paletizado `P` se reescalaba con NEAREST y quedaba dentado), reescala con LANCZOS, compone sobre el gris claro del marco (antes el alfa
se aplanaba sobre negro y la silueta oscura desaparecía), guarda JPEG q90 sin submuestreo de croma y a 640×480 (≈2× el tamaño visual, retina).
Es una copia derivada en la caché existente (clave y versión propias `ui-lanczos-flat-v1`, no invalida otras miniaturas); el original no se
modifica. CSS: `object-fit: contain`, sin `transform`. Comparativa: [`docs/design/biblioteca-imagenes-miniaturas/`](design/biblioteca-imagenes-miniaturas).

**Límite de la fuente.** Reescalar solo evita el aliasing que introduce la generación de la miniatura. Si un original ya está dentado, es pequeño o viene muy comprimido (p. ej. un JPEG de baja calidad o un PNG paletizado con pocos colores), la miniatura hereda ese defecto: nunca se amplía por encima del tamaño original y no se promete una mejora que el reescalado no puede dar. Los originales no se tocan.

**Abrir el visor desde la miniatura.** En «Todas las fotografías» cada miniatura es clicable sin JavaScript ni componentes externos: el botón «Ver» (`st.button` con `on_click=_select`) sigue siendo el único control y la CSS (`button::after { position: absolute; inset: 0 }` dentro de `div[class*="st-key-lib_thumb_"]`) extiende su área a toda la ficha. Ratón, táctil, `Enter`/`Espacio` y foco usan por tanto la misma callback y el mismo visor, sin eventos duplicados, y el foco visible (`outline` 3 px) se dibuja alrededor de la ficha. El texto alternativo de cada imagen es «Fotografía <vista · mercado · proveedor>». No se usa `help=` en el botón: Streamlit duplica el botón dentro del tooltip y crearía dos paradas de tabulación. El fondo del marco es `#f4f4f2` en ambos temas y los pies de foto reservan dos líneas para que los botones queden alineados.

**Rendimiento de la caché.** `ThumbnailCache._locked` solo reescribe `index.json` si su contenido cambia, y un acierto en caliente refresca la recencia LRU como máximo una vez por minuto (`TOUCH_INTERVAL_NS`). Antes, cada miniatura de una página reescribía el índice con `fsync` aunque estuviera en caché (24 escrituras por página). Una página en caliente ya no lee originales ni escribe el índice; solo se escribe al generar, evictar o cambiar una prueba de acceso. La granularidad de LRU pasa a ser de 1 minuto.

## Regeneración de miniaturas

Las miniaturas son derivadas (`IMAGE_THUMBNAIL_CACHE_ROOT`, `index.json` + `<sha256>.jpg`) y se direccionan por el checksum del original, de modo que los originales compartidos no se duplican. El perfil de la Biblioteca (640×480, LANCZOS, fondo `#f4f4f2`, `ui-lanczos-flat-v1`) tiene clave distinta del perfil antiguo (360×240): no se sobrescribe nada y el cambio de clave invalida las miniaturas dentadas. La Biblioteca las genera bajo demanda por página; `scripts/regenerate_image_thumbnails.py` permite precalentarlas.

Ejecutar dentro del contenedor, con las mismas variables de entorno que la app:

```
python scripts/regenerate_image_thumbnails.py report                 # simulación: cuenta, espacio y tiempo estimados (no escribe)
python scripts/regenerate_image_thumbnails.py regenerate             # simulación
python scripts/regenerate_image_thumbnails.py regenerate --execute [--provider Kering] [--batch-size 200] [--limit N] [--resume]
python scripts/regenerate_image_thumbnails.py verify --per-provider 20
python scripts/regenerate_image_thumbnails.py purge-legacy [--execute]   # opcional: elimina las 360×240 antiguas
```

- Idempotente: solo genera las claves que faltan. Escritura atómica (fsync + `os.replace`). Solo lee los originales.
- `--resume` continúa desde `regeneration-state.json`; los errores van a `regeneration-errors.jsonl` (ambos en la raíz de la caché) y no detienen el lote (salida 1).
- `purge-legacy` borra únicamente derivados; otras pantallas (Kering) los regeneran bajo demanda.
- No ejecutar en producción sin autorización: ejecutar antes `report` y comunicar nº de miniaturas, espacio y tiempo.

## Pendiente (fuera de este piloto)

Migrar el resto de pantallas, `/imagenes-kering` (buscar/historial), `/abc-historico`, retirar el `render_luxoptica_images_page` heredado de
`app_enhanced.py` (código muerto), ZIP por pedido (#38), fuente de marca con licencia, tema global (`.streamlit/config.toml`).

Capturas reales antes/después (P1): [`docs/design/biblioteca-imagenes-p1/`](design/biblioteca-imagenes-p1), generadas con
`scripts/image_library_visual_harness.py` (datos sintéticos; página real de Streamlit). Capturas sintéticas anteriores: [`docs/design/biblioteca-imagenes/`](design/biblioteca-imagenes).
