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
| fuente títulos | Cormorant Garamond → Georgia | Cormorant se usa en Vytria; **el fichero de fuente no se incluye** (licencia sin verificar), se usa fallback serif |

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

- Agrupación por EAN (texto, conserva ceros iniciales). Contadores: imágenes = archivos, EAN = productos distintos, modelos = modelos distintos no vacíos.
- Vistas: se usa solo la clasificación almacenada (`frontal`=V1, `perspectiva`=V2, `lateral`=V3, `detalle`). No se reclasifica; el detalle nunca se
  convierte en V2 (regla posicional Kering intacta). Las vistas ausentes se avisan.
- Miniatura de portada: V1 original si existe; en otro caso la primera vista por orden.
- Paginación por producto (12/24/48), solo se valida y genera miniatura de las portadas visibles; el detalle solo carga las fotos del producto y
  el original seleccionado.
- ZIP bajo demanda (`zip_eans_for_user`, comprueba rol) y descarga individual con el nombre original. Sin consultas a Odoo ni a proveedores.

## Pendiente (fuera de este piloto)

Migrar el resto de pantallas, `/imagenes-kering` (buscar/historial), `/abc-historico`, retirar el `render_luxoptica_images_page` heredado de
`app_enhanced.py` (código muerto), ZIP por pedido (#38), fuente de marca con licencia, tema global (`.streamlit/config.toml`).

Capturas sintéticas antes/después: [`docs/design/biblioteca-imagenes/`](design/biblioteca-imagenes).
