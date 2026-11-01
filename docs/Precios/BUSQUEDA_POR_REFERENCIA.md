# Búsqueda por EAN, modelo y color — comparador de precios

Ampliación del comparador (sin conectores nuevos, sin planificador activo, sin cambios de producción).

## Identidad de un producto

La identidad exige **marca + modelo + código de color** cuando no hay EAN publicado. El calibre es informativo y nunca bloquea esa coincidencia. Con EAN publicado idéntico, los campos no publicados pueden faltar; cualquier contradicción explícita mantiene la ficha en revisión. Un EAN distinto no se acepta automáticamente: si la referencia coincide, queda pendiente de revisión; si también contradice marca/modelo/color, se rechaza.

`BOTTEGA VENETA BV1274S 005` = `BV1274S 005` = `bv1274s-005` = `BV1274S/005` = `BV1274S005`. Se ignoran mayúsculas, acentos, espacios, guiones y barras; los ceros del color se conservan (`005` ≠ `5`). El calibre `54□21`, `54-21`, `54/21` y `54 21` se normaliza a `54-21`. Módulo: [price_references.py](../../price_references.py).

## Orden de búsqueda

1. EAN exacto.
2. Marca + modelo + color.
3. Modelo + color.
4. Variantes normalizadas (`BV1274S005`, `BV1274S-005`…).

`python price_observations.py search "BOTTEGA VENETA BV1274S 005"` muestra el nivel que resolvió, el producto del catálogo y el plan de consultas. Si hay varios productos (colores/calibres), devuelve `ambiguous: true` y no elige.

## Clasificación de coincidencias

| Estado | Condición | ¿Entra en comparación? |
|---|---|---|
| `exact_match` (base `ean`) | EAN publicado idéntico y sin contradicciones explícitas; calibre informativo | Sí |
| `exact_match` (base `reference`) | Marca, modelo y código de color confirmados; sin EAN publicado | Sí |
| `probable_match` | Identidad incompleta/ambigua, EAN distinto con referencia coincidente o EAN idéntico con contradicción explícita | No (revisión) |
| `rejected` | Marca/modelo/color contradicen el catálogo sin EAN idéntico | No (se guarda en `search_results`) |

Un `exact_match` declarado en un JSON que no se verifica contra el catálogo **se rechaza** al importar.

Las colecciones comerciales como «Symbole» no sustituyen el modelo conocido. El código `16K08Z` se recupera del título o identificadores publicados; «Negro» es descriptivo. Los campos opcionales `identificadores` y `ambigua` conservan esa evidencia al importar JSON. La validación normalizada y las observaciones dimensionales quedan en la evidencia; no se modifica el catálogo ni se reclasifica el histórico. Véase [regla y regresiones](../PRICE_IDENTITY.md).

## Datos estructurados

`offers` y `search_results` guardan `ean_published`, `model_code`, `color_code`, `caliber` y `match_basis`; `offers` también `price_includes_tax` y `vat_rate`. IVA no incluido o no determinado (NULL) queda fuera de las comparaciones y gráficos (se conserva almacenado); IVA no determinado en una observación nueva la deja en `manual_review`. El calibre del catálogo está en `products.caliber` (`lente-puente`, p. ej. `63-13`). Las columnas se añaden solas a bases existentes.

## Importar observaciones (JSON)

Esquema `cloud-price-observations/1`; ejemplo real en [observaciones_prada.json](../../tests/fixtures/observaciones_prada.json). Campos: `ean_consultado` o `referencia`, `competidor`, `marca`, `modelo`, `color`, `calibre`, `ean_publicado`, `titulo`, `url`, `precio_venta`, `precio_anterior`, `precio_promocional`, `coste_envio`, `moneda`, `iva_incluido` (true/false/null), `tipo_iva`, `disponibilidad`, `fecha_observacion`, `metodo`, `evidencia`, `mercado`, `estado` (`observada`, `no_encontrada`, `error`, `bloqueada`, `sin_conector`), `detalle`.

```powershell
python price_observations.py import observaciones.json            # simulación (por defecto): copia temporal de la BD
python price_observations.py import observaciones.json --apply    # escribe en la BD
```

- Observación idéntica a la última (mismo precio, stock e identidad): no duplica; actualiza `last_ok_at` y `checks` (`unchanged_checked`).
- Cambio de precio o disponibilidad: nueva fila (histórico conservado, compatible con los gráficos).
- Ofertas sin stock: se guardan, no se comparan.
- Errores, bloqueos y «sin conector»: se registran como resultado de búsqueda, sin precio.

## Catálogo y competidores

- `python price_observations.py sync-catalog [--apply]`: completa modelo, color y calibre desde Odoo (**solo lectura**, simulación por defecto).
- Mercado y moneda de los competidores: no se asumen. Quedan vacíos hasta que se verifiquen (editable en la interfaz). `ES`/`EUR` es solo el mercado objetivo de la comparación (precio Vytria). Las observaciones JSON deben traer `mercado` (y `moneda` si hay precio); si faltan se rechazan con aviso. `PriceComparatorStore.fill_competitor_defaults()` solo lista los pendientes; `apply=True` exige `market=` y `currency=` confirmados explícitamente.
- «Diferencia %» principal = diferencia ÷ precio Vytria (`difference_pct`); sobre el competidor queda en `difference_vs_competitor_pct`.

## Dashboard comercial

La pestaña inicial «Resumen» muestra indicadores, posicionamiento, evolución,
productos que requieren atención y consulta individual. El ámbito son
productos únicos del catálogo identificados por EAN; las ofertas se validan
por las reglas existentes de identidad, disponibilidad, IVA incluido, EUR y
mercado coincidente con el precio de Vytria. Se usa una sola observación
vigente por EAN y competidor. El calibre es informativo.

- **Productos comparados**: referencias con precio Vytria validado y al menos
  una oferta comparable.
- **Igual o más barato**: productos donde Vytria cuesta lo mismo o menos que
  el mínimo comparable, dividido por los productos comparados. El empate
  cuenta como competitivo.
- **Más caro**: productos por encima del mínimo comparable, con el mismo
  denominador.
- **Diferencia media vs. mejor**: media no ponderada por producto de
  `(precio Vytria − mejor precio comparable) / mejor precio comparable × 100`.
  Un resultado positivo indica que Vytria es más cara. Sin comparaciones
  válidas, los porcentajes se muestran como «Sin datos», no como cero.

El filtro de periodo (7, 30 o 90 días) se comparte entre el resumen y las dos
series históricas; los datos del resumen se limitan además a la vigencia máxima
configurada. La serie global recalcula cada día con los últimos precios
disponibles hasta ese día, exige que sigan vigentes en esa fecha y no rellena
días sin productos comparables. Un único día con datos se muestra como punto,
no como tendencia. La línea por producto solo usa observaciones exactas,
disponibles, con IVA y del mismo mercado; las observaciones sin disponibilidad
crean una interrupción, no prolongan una oferta como vigente.

Los filtros de marca, competidor, estado y texto se aplican antes de recalcular
KPIs, distribución, tabla de atención y selección de producto. Al elegir un
competidor, el mínimo y la posición se vuelven a calcular solo con sus ofertas.
La tabla de atención incluye exclusivamente Vytria por encima de competencia y
se ordena por la mayor diferencia porcentual frente al mejor precio. No se
generan recomendaciones de rebaja ni cambios automáticos de precios.

Capturas de la revisión visual con datos sintéticos: [escritorio](./dashboard-desktop-synthetic.png),
[móvil](./dashboard-mobile-synthetic.png), [tabla de atención](./dashboard-atencion-synthetic.png)
y [detalle de producto](./dashboard-detalle-synthetic.png). Los importes y productos
de las capturas no representan datos comerciales reales.

## Cobertura de competidores

Fichero [cobertura_competidores.json](./cobertura_competidores.json) (14 competidores). Cargar y ver:

```powershell
python price_observations.py coverage --load docs/Precios/cobertura_competidores.json          # simulación
python price_observations.py coverage --load docs/Precios/cobertura_competidores.json --apply
```

Estados: `not_checked`, `no_authorized_connector`, `pending_authorization`, `blocked`, `manual_verified`, `operational`. `operational` solo se acepta con conector verificado en `competitor_schedule` **y** al menos una oferta confirmada; hoy ninguno lo cumple. Las referencias localizadas y ofertas confirmadas se calculan desde la BD. Las condiciones de uso de las tiendas **no están revisadas**; `robots.txt` no equivale a autorización.

## Pendiente de autorización

Feed, API o permiso expreso para fichas conocidas en los 14 competidores; el conector `feed_csv` sigue siendo solo por EAN. Revisar condiciones de uso antes de cualquier consulta automática.
