# Marcolin: validación real (#59) — estado y procedimiento

Estado a 2026-10-22. Este documento separa **lo observado**, **lo comunicado por el usuario** y **lo pendiente**. No se
publican códigos comerciales, identificadores de pedido o de solicitud, capturas ni credenciales. El proceso completo
(Odoo → portal → ZIP → biblioteca) **no está validado**.

## 1. Observado en el piloto real (22/10/2026)

Las dos auditorías autónomas **no las hizo `scripts/marcolin_odoo_pilot.py` en su versión original**: se ejecutaron
en el host de producción con código ad hoc. Este PR incorpora una versión autónoma del script (§4) que reproduce esas
comprobaciones, pero esa versión **todavía no se ha ejecutado contra producción**.

| Qué | Resultado | Alcance |
|---|---|---|
| Auditoría autónoma de Odoo | Seis UPC de un pedido de compra confirmado de Marcolin confirmados con PostgreSQL en modo `READ ONLY` y rollback | Solo ese pedido; no es una auditoría anual ni valida todos los contactos comerciales |
| Auditoría autónoma del catálogo | Catálogo SQLite abierto en solo lectura (`mode=ro`, `query_only`) sin instanciar `ImageRepository`: **sin registros** para los seis UPC ni para sus equivalentes con cero inicial | Prueba la ausencia **en el catálogo**, no la de archivos sin catalogar |
| Portal, selección | Asset requests → Products images → Purpose *Web site* → Use *Product catalogue* → marca → Apply filters → `Select by list` → seis UPC de 12 dígitos pegados separados por saltos de línea | Observado en una sesión iniciada por el usuario |
| Portal, agrupación | El portal agrupó los seis UPC en **cuatro referencias de modelo/color**: 0 activas, 4 inactivas en la colección, 0 incorrectas | Una sola prueba, seis códigos |
| Portal, envío | Con autorización del usuario se envió la solicitud y se vio **una** solicitud en `My pending requests` | La descarga sigue pendiente |

## 2. Comunicado por el usuario (no verificado por nosotros)

- Preparación: rara vez menos de 24 h y puede tardar varios días.
- La interfaz indica un máximo de 300 códigos; **solo se han probado seis**. El límite real no está verificado.
- El login/MFA se conoce solo como una sesión iniciada por el usuario: frecuencia de MFA/CAPTCHA y permiso para
  automatizar no están verificados.

## 3. Pendiente (no afirmar hasta observarlo)

Descarga del ZIP, estructura de nombres, manifiesto, vistas disponibles, caducidad del enlace, correspondencia
imagen↔UPC e importación. Las cuatro vistas de la biblioteca (frontal, lateral, perspectiva, detalle) son la taxonomía
del repositorio, **no una política confirmada de Marcolin**. Una referencia «inactiva» o una solicitud pendiente durante
días **no se descarta ni se repite**: el estado del portal se registra tal cual y nunca filtra códigos.

## 4. Ejecución autónoma de la auditoría (solo lectura)

`scripts/marcolin_odoo_pilot.py` solo importa `sqlalchemy`, `kering_jobs` e `image_repository`, los módulos presentes en
producción (`provider_images`, `provider_odoo_orders` y `marcolin_images` **no** están allí). Si falta alguno, termina con
el bloqueo `dependencia_ausente` (exit 5), con solo los nombres de módulo.

El sistema de archivos del contenedor es de solo lectura, así que el script se pasa por **entrada estándar** (no hay
que copiarlo ni instalarlo) y los informes se dirigen a un volumen con escritura (`/app/data` o `/app/image-state`):

```
docker exec -i -w /app <contenedor> python - --partner 573 --days 365 --size 8 \
    --upc-file /app/data/in/upcs.txt --out /app/data/audits < scripts/marcolin_odoo_pilot.py
```

(En PowerShell: `Get-Content -Raw -Encoding utf8 scripts\marcolin_odoo_pilot.py | docker exec -i ... python - ...`.)
Las credenciales `DB_*` ya están en el entorno del contenedor y nunca se imprimen; los errores solo muestran la clase.

| Parámetro | Uso |
|---|---|
| `--partner`, `--expect-name` | partner de Odoo y fragmento de nombre que debe contener (por defecto `marcolin`); solo informa, **no guarda asociaciones** |
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
catalogar. La propuesta (máx. 10) queda en `propuesta_pendiente_de_aprobacion`; `requests_created` es 0 y
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

## 7. Ajustes detectados en otras PR (fuera del alcance de #76)

- **#70/#71 — duplicado de solicitudes:** planificar el UPC-12 y su EAN-13 con cero inicial crea **dos** solicitudes
  vivas (el índice único usa el EAN literal y la coincidencia usa la clave de 14 dígitos). El portal agrupa por UPC, así
  que hay riesgo de duplicar. Unificar el índice por clave.
- **#71/#74 — separador:** `export_ean_list` usa comas por defecto y la UI dice «separada por comas»; Marcolin necesita
  saltos de línea (`export_ean_list(id, "\n")` ya funciona): default propio para Marcolin y texto de UI.
- **#75 — no es estrictamente solo lectura:** `build_preview`/`pick_pilot` usan `ImageRepository.pending_views`, cuyo
  constructor crea directorios y DDL. Hace falta un lector de catálogo de solo lectura (como el de este script) y que
  «Imágenes: Ausentes» se rotule «sin registros en el catálogo».
- La cobertura de este script solo es comparable con la del repositorio si se usa `--verify-files`.
