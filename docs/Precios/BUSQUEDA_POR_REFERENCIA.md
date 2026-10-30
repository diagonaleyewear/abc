# Búsqueda por EAN, modelo y color — comparador de precios

Ampliación del comparador (sin conectores nuevos, sin planificador activo, sin cambios de producción).

## Identidad de un producto

Un producto es **marca + modelo + código de color + calibre** (y EAN cuando existe). El mismo modelo en otro color o calibre es otro producto y **nunca se mezcla**.

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
| `exact_match` (base `ean`) | EAN publicado idéntico, sin contradicciones | Sí |
| `exact_match` (base `reference`) | Marca, modelo, color y calibre confirmados en ambos lados, sin EAN | Sí |
| `probable_match` | Falta calibre o marca, o EAN idéntico con datos contradictorios | No (revisión) |
| `rejected` | Otro EAN, color, calibre, modelo o marca | No (se guarda en `search_results`) |

Un `exact_match` declarado en un JSON que no se verifica contra el catálogo **se rechaza** al importar.

## Datos estructurados

`offers` y `search_results` guardan `ean_published`, `model_code`, `color_code`, `caliber` y `match_basis`; `offers` también `price_includes_tax` y `vat_rate`. IVA no incluido queda fuera de las comparaciones y gráficos; IVA no determinado en una observación nueva la deja en `manual_review`. El calibre del catálogo está en `products.caliber` (`lente-puente`, p. ej. `63-13`). Las columnas se añaden solas a bases existentes.

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
- Mercado y moneda de los competidores: por defecto `ES`/`EUR` al importarlos (editable en la interfaz). Para bases existentes, `PriceComparatorStore.fill_competitor_defaults(apply=True)` rellena solo los vacíos.
- «Diferencia %» principal = diferencia ÷ precio Vytria (`difference_pct`); sobre el competidor queda en `difference_vs_competitor_pct`.

## Cobertura de competidores

Fichero [cobertura_competidores.json](./cobertura_competidores.json) (14 competidores). Cargar y ver:

```powershell
python price_observations.py coverage --load docs/Precios/cobertura_competidores.json          # simulación
python price_observations.py coverage --load docs/Precios/cobertura_competidores.json --apply
```

Estados: `not_checked`, `no_authorized_connector`, `pending_authorization`, `blocked`, `manual_verified`, `operational`. `operational` solo se acepta con conector verificado en `competitor_schedule` **y** al menos una oferta confirmada; hoy ninguno lo cumple. Las referencias localizadas y ofertas confirmadas se calculan desde la BD. Las condiciones de uso de las tiendas **no están revisadas**; `robots.txt` no equivale a autorización.

## Pendiente de autorización

Feed, API o permiso expreso para fichas conocidas en los 14 competidores; el conector `feed_csv` sigue siendo solo por EAN. Revisar condiciones de uso antes de cualquier consulta automática.
