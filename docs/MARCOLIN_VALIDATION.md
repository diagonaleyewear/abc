# Marcolin: validación real (#59) — estado y procedimiento

Estado a 2026-10-22. Este documento separa **lo observado en navegador**, **lo comunicado por el operador** y **lo
pendiente**. No se publican códigos comerciales, identificadores de pedido o de solicitud, capturas ni credenciales. El
proceso completo (Odoo → portal → ZIP → biblioteca) **no está validado**.

## 1. Observado en navegador (22/10/2026)

- El operador inició sesión en MyMarcolin. Se observó la navegación **Multimedia → Asset requests → Products images →
  Purpose *Web site* → Use *Product catalogue* → Apply filters → `Select by list`**. Se omite del repositorio la marca
  seleccionada.
- La interfaz indica UPC de 12 dígitos y un máximo de 300 códigos. Se probaron seis códigos, no un lote de 300. Para el
  pegado se usaron saltos de línea; la UI advierte que no se introduzcan comas ni espacios.
- El portal agrupó los seis UPC en **cuatro referencias de modelo/color**: 0 activas, 4 inactivas en la colección y
  0 incorrectas. «Inactiva» no demuestra ausencia de imágenes ni justifica descartar el código.
- Con autorización expresa del usuario se completaron las confirmaciones; se verificó **una** solicitud en
  `My pending requests`. Sigue pendiente de descarga.

## 2. Evidencia comunicada por el operador

- Una comprobación autónoma distinta del script original consultó seis UPC de un pedido confirmado mediante PostgreSQL
  `READ ONLY` y rollback. No fue una auditoría anual ni una revisión de todos los contactos comerciales.
- Otra comprobación autónoma abrió el catálogo SQLite con `mode=ro` y `query_only`, sin instanciar `ImageRepository`.
  Ninguno de los seis UPC ni sus equivalentes con cero inicial tenía registros en ese catálogo. Esto acredita ausencia
  en el catálogo consultado, no ausencia de archivos sin catalogar.
- La preparación rara vez tarda menos de 24 horas y puede tardar varios días; no se debe reenviar por el mero transcurso
  del tiempo.

Las comprobaciones autónomas anteriores no las ejecutó `scripts/marcolin_odoo_pilot.py`. El script de esta PR reproduce
la auditoría acotada, pero **todavía no se ha ejecutado contra producción**.

## 3. Pendiente (no afirmar hasta observarlo)

- Frecuencia de MFA/CAPTCHA y permiso de automatización: sin verificar; solo consta una sesión iniciada por el operador.
- Descarga del ZIP, estructura de nombres, manifiesto, vistas disponibles, caducidad del enlace, correspondencia
  imagen↔UPC e importación.
- Relación verificable de todos los UPC con las cuatro referencias modelo/color tras recibir los activos.
- Las cuatro vistas de la biblioteca (frontal, lateral, perspectiva, detalle) son la taxonomía del repositorio, **no una
  política confirmada de Marcolin**.

Una referencia «inactiva» o una solicitud pendiente durante días **no se descarta ni se repite**. La descarga e
importación siguen pendientes; no se ha completado el piloto end-to-end.

## 4. Ejecución autónoma de la auditoría (solo lectura)

`scripts/marcolin_odoo_pilot.py` solo importa `sqlalchemy`, `kering_jobs` e `image_repository`, los módulos presentes en
producción (`provider_images`, `provider_odoo_orders` y `marcolin_images` **no** están allí). Si falta alguno, termina con
el bloqueo `dependencia_ausente` (exit 5), con solo los nombres de módulo.

El sistema de archivos del contenedor es de solo lectura, así que el script se pasa por **entrada estándar** (no hay
que copiarlo ni instalarlo) y los informes se dirigen a un volumen con escritura (`/app/data` o `/app/image-state`):

```
docker exec -i -w /app "$CONTAINER" python - --partner "$PARTNER_ID" --days 365 --size 8 \
    --expect-name "$PARTNER_NAME" \
    --upc-file /app/data/in/upcs.txt --out /app/data/audits < scripts/marcolin_odoo_pilot.py
```

(En PowerShell: `Get-Content -Raw -Encoding utf8 scripts\marcolin_odoo_pilot.py | docker exec -i ... python - ...`.)
Las credenciales `DB_*` ya están en el entorno del contenedor y nunca se imprimen; los errores solo muestran clases de
error o nombres de dependencias.

| Parámetro | Uso |
|---|---|
| `--partner`, `--expect-name` | ID obligatorio; filtro de nombre opcional y parametrizado; no se presupone ni guarda una asociación |
| `--days`, `--max-lines`, `--timeout-ms` | intervalo y límites de volumen/tiempo de la lectura |
| `--upc`, `--upc-file`, `--order-name` | códigos a confirmar y/o pedidos concretos; sin ellos descubre desde los pedidos `purchase`/`done` |
| `--portal-groups FILE` | JSON `{"groups":[{"portal_ref","status","upcs":[...]}]}` con las referencias modelo/color que devolvió el portal; se mantiene fuera de GitHub |
| `--catalog`, `--image-root` | catálogo (por defecto `IMAGE_REPOSITORY_STATE_ROOT`/`IMAGE_REPOSITORY_ROOT` + `/.catalog.sqlite3`) y raíz de imágenes |
| `--verify-files` | además de metadatos, comprueba ruta, tamaño, SHA-256 y huella de cada archivo |
| `--required-view` | vistas que cuentan como completas (por defecto frontal, lateral, perspectiva; **no es política de Marcolin**) |
| `--max-codes` | tope de entrada (300, el valor que indica la interfaz; sin verificar) |
| `--out` | directorio de informes (obligatorio para guardar; ficheros exclusivos 0600, nunca sobrescribe) |

**Entrada de códigos.** Un UPC por línea (el portal avisa de no usar comas ni espacios). Los códigos se tratan como
texto y se conservan literalmente (un UPC de 12 dígitos con cero inicial no se convierte a número). Las formas
equivalentes (UPC-12 / EAN-13 con cero) solo se usan para buscar y para avisar de duplicados equivalentes. Con
`--portal-groups` el informe mantiene la relación UPC → referencia modelo/color del portal, y lista los UPC sin grupo,
los repetidos en varios grupos y los grupos vacíos.

**Solo lectura.** Odoo: `SET TRANSACTION READ ONLY` + `statement_timeout` + rollback; solo pedidos `purchase`/`done`.
Catálogo: `file:…?mode=ro` + `PRAGMA query_only`; no se usa `ImageRepository` (su constructor crea directorios y ejecuta
DDL), no se crea ni modifica nada y no se usa `immutable=1`, de modo que se leen también las filas que solo están en un
WAL activo (se informa del modo de journal y de la presencia de `-wal`).

**Cobertura.** `completas`, `parciales`, `sin_registros`, `sin_vistas_validas` o `desconocida`. Un catálogo inexistente,
ilegible o con esquema distinto da `desconocida` (exit 6, informe parcial), **nunca** «sin imágenes». Sin
`--verify-files` el informe declara `solo_metadatos_del_catalogo`; `sin_registros` no prueba que no haya archivos sin
catalogar. La propuesta (máx. 8) queda en `propuesta_pendiente_de_aprobacion`; `requests_created` es 0 y
`portal_contacted` es `false`.

Códigos de salida: 0 completo · 2 Odoo no disponible o entrada inválida · 3 el partner no coincide · 4 informe no
escrito · 5 dependencia ausente · 6 parcial (catálogo no consultable).

## 5. Qué falta al driver de Marcolin

Solo existe el contrato (`MarcolinDriver`) y el adaptador `MarcolinPortal`; **no hay driver real**.

| Punto | Estado |
|---|---|
| Autenticación, MFA/CAPTCHA, permiso para automatizar | Sin verificar (solo se sabe de una sesión iniciada por el usuario) |
| Introducción | Observado: `Select by list`, un UPC-12 por línea |
| Límite por solicitud | UI indica 300; probados 6; el conector sigue en 50 provisional (`batch_size_verified=false`) |
| Envío e id | Se vio una solicitud en `My pending requests`; el id no se publica |
| Estados | Solo «pending» observado; el resto del vocabulario está sin verificar |
| Tiempos | Según el usuario, de 24 h a varios días; el sondeo no debe reenviar |
| Descarga, ZIP, nombres, manifiesto, vistas, caducidad | Pendiente |

## 6. Procedimiento manual autorizado (sin automatización)

1. Ejecutar la auditoría (§4) y revisar la propuesta; aprobarla a mano.
2. Una persona con credenciales pega los UPC (uno por línea) en `Select by list`, anota el resultado del portal (grupos
   modelo/color y su estado) y lo guarda como `--portal-groups`. No se repite una solicitud ya pendiente.
3. Cuando el portal la prepare, descargar el ZIP y guardar la lista de nombres y el manifiesto (si existe) en #59, sin
   códigos reales.
4. Importar el ZIP en Cloud: lo ambiguo va a revisión; verificar originales, SHA-256, miniaturas y biblioteca.
5. `MARCOLIN_AUTOMATION_ENABLED` sigue en `false` hasta decidir, con esos datos, si procede un driver real.

## 7. Ajustes en otras PR (resueltos en sus ramas, pendientes de revisión/CI)

- **#70/#71 — identidad UPC-12 ↔ EAN-13:** la clave de 14 dígitos (`ean_key`) es ahora la identidad de la solicitud en
  curso (columna generada + índice único parcial). El código original se conserva para mostrar y exportar. Las bases
  existentes **no se migran solas**: `python scripts/provider_requests_migrate.py --root <dir>` informa (dry-run) y
  `--apply` migra con copia previa; se niega si hay conflictos y no borra historial.
- **#71/#74 — separador:** Marcolin exporta un código por línea (`list_separator`); los demás proveedores siguen con comas.
  La relación UPC ↔ referencia modelo/color del portal se guarda con `MarcolinConnector.record_portal_groups`
  (estado del portal literal, sin descartar inactivas, sin tocar el estado de la solicitud).
- **#75 — catálogo solo lectura:** la vista previa usa `repository_catalog_readonly` (el mismo criterio que este script,
  verificado por `tests/test_catalog_reader_parity.py`) y no construye `ImageRepository`.
- Este script sigue siendo autónomo porque producción solo tiene `sqlalchemy`, `kering_jobs` e `image_repository`. Su
  versión nueva **no se ha ejecutado en producción**.
- La cobertura de este script solo es comparable con la del repositorio si se usa `--verify-files`.
