# Sincronización del PVP Vytria desde Odoo

Sustituye la importación manual de precios propios del comparador. Integración **solo de lectura**:
no se modifica nada en Odoo ni en PrestaShop.

## Fuente del precio

Auditoría de solo lectura sobre `DiagonalDBProd` (PostgreSQL 12.8, sesión `READ ONLY`):

- Tarifa real de Vytria España: **ID 59** («Vytria TARIFA_ESPAÑA», EUR, `website_id=10`, canal
  `prestashop`). La 17 es la versión anterior (`website_id=4`).
- Precio final: tabla `prestashop_pricelist_item_pricelist` (`pricelist_id=59`), unida a
  `prestashop_pricelist_item` (`product_id` → `product_product`). Se guarda **tal cual**: ya incluye
  los descuentos de la tarifa y el IVA. No se vuelve a descontar ni se suma IVA.
- Descuento de la tarifa: `list_price × (1 − descuento %)`, con el descuento del ítem
  `1_pricelist_map` de la tarifa 59 asociado a los mapas del producto
  (`product_pricelist_map_product_template_rel`), redondeado a 1,00. El campo
  `product_template.pricelist_map_id` **no** es este mapa.
- IVA: un único impuesto de venta por producto con `price_include = True` (10 % o 21 %). El
  porcentaje se guarda solo como información.
- Los 12 EAN auditados coinciden exactamente entre `list_price`/mapa, precio de integración y
  precio sincronizado (probado también contra Odoo real, con SQLite temporal).
- No se usan `diagonal_vytria_int_catalog_item*` (0 filas) ni el canal 5.

**Pendiente: contraste directo con los precios publicados en PrestaShop. No está validado.**

## Datos guardados (SQLite del comparador, volumen persistente)

| Tabla | Contenido |
|---|---|
| `vytria_prices` | Precio vigente por EAN: precio, moneda, IVA incluido, % IVA, origen, tarifa, IDs de origen (producto, plantilla, catálogo, fila de precio), fecha de origen, precio recalculado, estado de validación, fechas de primera/última variación y de sincronización. |
| `vytria_price_history` | Una fila por cambio (precio, IVA, moneda, estado). Nunca se borra. |
| `vytria_sync_runs` / `vytria_sync_issues` | Registro de cada ejecución y de las incidencias por EAN. |
| `vytria_sync_settings` | `auto_enabled` (por defecto `0`). |

Solo se sincronizan los EAN del catálogo del comparador (consulta acotada por código de barras;
los UPC-12 se buscan con y sin cero inicial). La escritura es incremental: si el origen no cambia,
solo se actualiza `synced_at` y no se crea histórico.

## Validación cruzada y estados

El precio de origen nunca se modifica; la discrepancia se registra. Solo `validated` se compara
con la competencia (`comparable = 1`).

| Estado | Significado |
|---|---|
| `validated` | El precio coincide con la fórmula de la tarifa (tolerancia 0,01 €). |
| `price_mismatch` | Difiere del recálculo. Se guarda el precio recibido y el esperado. |
| `no_map` | El producto no tiene mapa con regla en la tarifa 59. |
| `multiple_maps` | Mapas con resultados distintos. |
| `exceptional_rule` | Descuento negativo (p. ej. `Vytria CT0649S`, −300 %), nombre del mapa distinto del % aplicado (p. ej. «65 %» que en la 59 aplica 45 %), recargo, cantidad mínima, fechas, regla por producto o cálculo no `formula`. |
| `tax_issue` | Sin IVA, más de un IVA, impuesto no porcentual o no incluido en el precio. |
| `currency_issue` | Moneda distinta de EUR. |
| `discontinued` | Variante/plantilla inactiva o no vendible. |

Incidencias que **no sobrescriben** el último precio confirmado: `not_in_odoo`, `no_price`
(sin fila, inactiva o ≤ 0), `multiple_prices`, `ambiguous_product`, `unexpected_catalog`.
En el comparador, un EAN sincronizado sin precio `validated` queda sin precio propio (no se usa el
Excel); un EAN nunca sincronizado conserva el precio importado manualmente.

## Ejecución

- **Manual**: botón «Actualizar precios Vytria» (Análisis comercial → Comparador de precios).
- **Diaria**: interruptor «Sincronización diaria automática», **desactivado por defecto**. Solo
  actúa `python vytria_price_sync.py --scheduled` (se omite si está desactivado o si hubo una
  sincronización correcta en las últimas 23 h). Esta PR **no** crea ningún disparador ni servicio;
  activarlo en producción requiere un job aprobado que invoque ese comando.
- **Concurrencia**: `FileLock` junto a la base; un segundo intento recibe «sincronización en curso».
- **Errores y reintentos**: 3 intentos con espera 2 s / 4 s ante fallos de conexión; si falla, se
  conservan los precios y se registra solo el tipo de error (sin host ni credenciales).
- **Conexión**: `load_db_config()` (`DB_*`), `default_transaction_read_only=on`,
  `SET TRANSACTION READ ONLY`, `statement_timeout=30000`; solo `SELECT`.

## Contingencia

La importación Excel de precios queda en un desplegable «Contingencia administrativa». Un precio
sincronizado desde Odoo tiene prioridad sobre el manual para el mismo EAN.

## Limitaciones

- Sin precio promocional ni disponibilidad: la tarifa 59 ya incorpora los descuentos y la
  disponibilidad no procede de esta fuente.
- Mercado fijo `ES` y moneda EUR.
- Los productos con mapas especiales quedan marcados para revisión y no entran en la comparación
  hasta confirmarse.
- La muestra auditada (12 EAN) no contenía mapas 65 % ni CT0649S: se cubren con datos de prueba.
- El Dockerfile copia `vytria_price_sync.py`; sin él la página del comparador no arranca.
