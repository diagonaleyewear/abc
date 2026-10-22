# Proveedores de imágenes: matriz de estado (#59)

Diseño y auditoría: PR #69 (`docs/PROVIDER_INTEGRATIONS_59.md`). Este documento refleja lo **implementado y probado
offline** (portales y Odoo simulados). No se ha accedido a ningún portal, no se ha descargado nada real, no hay
credenciales en el repositorio y los schedulers siguen desactivados.

## Decisión: Cloud es independiente de Odoo

Flujo principal, sin Odoo ni `supplier_ids`: elegir proveedor → pegar/importar EAN (CSV/TXT) → validación y
deduplicación (vista previa sin escribir) → crear solicitudes → exportar lista para el portal / solicitud diferida →
importar ZIP o descarga → identificar vistas → originales al repositorio común (`ImageRepository`) → miniaturas →
pestaña «Biblioteca». «Consultar EAN desde Odoo» es un expander secundario que solo actúa al pulsar el botón. La página
no contacta con Odoo ni con portales al renderizarse.

## Componentes

| Módulo | Función |
|---|---|
| `provider_images.py` | Núcleo: SQLite con máquina de estados, un solo `request` vivo por EAN, eventos append-only, `plan_eans`/`preview_eans`/`parse_ean_text`/`parse_ean_file`, emparejamiento fichero↔EAN con evidencia, importación a `ImageRepository` (SHA-256, originales inmutables, procedencia en `metadata`), revisión con cuarentena, ZIP seguro, límites, backoff. Odoo opcional (`detect`). |
| `marcolin_images.py`, `safilo_images.py`, `nuorder_images.py` | Conectores independientes. |
| `provider_images_ui.py` | Página admin: solicitudes, planificar por EAN (texto/archivo), revisión, biblioteca con miniaturas. |
| `scripts/audit_provider_odoo.py` | Auditoría Odoo solo lectura, opcional. |

Interruptores: `MARCOLIN_/NUORDER_/SAFILO_AUTOMATION_ENABLED` (por defecto `false`). `run_cycle` es `dry_run` por defecto.

## Matriz por proveedor (todo probado solo con simuladores)

| Capacidad | Marcolin | NuORDER | Sáfilo |
|---|---|---|---|
| EAN manual / lista CSV-TXT / validación / deduplicación sin Odoo | ✅ | ✅ | ✅ |
| Solicitud diferida + estados + sin duplicados | ✅ | n/a (manual) | ✅ síncrono |
| Interrupción/respuesta ambigua → `needs_review`, nunca reenvío | ✅ | n/a | ✅ |
| Reintentos seguros | ✅ | ✅ | ✅ |
| Exportar lista de EAN para el portal | ✅ | ✅ | ✅ (separada por comas) |
| Subir ZIP (con o sin manifiesto) | ✅ | ✅ | ✅ |
| ZIP sin manifiesto | EAN completo en nombre, si no → revisión | idem | **siempre revisión** |
| Vistas V1/V2/V3/detalle, `unknown` → revisión | ✅ | ✅ | ✅ |
| Biblioteca común + miniaturas + compatibilidad Kering/Luxottica | ✅ | ✅ | ✅ |
| Driver real de portal | ❌ no existe | ❌ no procede | ❌ no existe |
| Requiere credenciales/acceso | portal Marcolin | portal NuORDER | Safilo Omnia |
| Validado contra el portal real | **no** | **no** | **no** |

## Bloqueos reales

- **Marcolin**: sin confirmar método de acceso, términos, MFA/CAPTCHA, estructura del ZIP ni límite real de EAN por
  solicitud (`max_eans_per_request=50` provisional, `batch_size_verified=false`; la UI lo advierte).
  Contrato: `MarcolinDriver`; estados desconocidos del portal → revisión.
- **NuORDER**: la API oficial es OAuth 1.0 iniciada por la marca; no hay API documentada para minoristas. Solo modo
  manual asistido. Acción pendiente: preguntar a NuORDER/las marcas si existe un mecanismo autorizado para nuestra cuenta.
- **Sáfilo**: sin confirmar acceso ni condiciones de automatización, ni si la descarga trae manifiesto. Sin
  manifiesto/metadatos todo va a revisión.
- Odoo (opcional): `supplier_ids` vacío → solo falla «Consultar EAN desde Odoo»; nada más.

## Operación manual autorizada (hoy)

1. Cloud → Proveedores de imágenes → proveedor → «Planificar por EAN» → pegar/subir EAN → «Validar y crear solicitudes».
2. «Solicitudes» → elegir solicitud → copiar la lista de EAN.
3. Persona autorizada entra en el portal con sus credenciales (MFA incluido), pega la lista (Marcolin: solicita imágenes y
   anota el id en «Reconciliar»; Sáfilo Omnia: búsqueda por EAN separados por comas, seleccionar productos, descargar todas
   las vistas; NuORDER: descargar medios) y guarda el ZIP.
4. Subir el ZIP en la solicitud → «Importar ZIP». Lo ambiguo queda en «Revisión».
5. Pestaña «Biblioteca» → «Mostrar imágenes» para comprobar originales y miniaturas.

## Procedimiento de piloto (requiere autorización)

1. Elegir un proveedor y **5–10 EAN** reales conocidos; confirmar por escrito acceso y condiciones del portal.
2. Hacer la operación manual anterior; registrar el tamaño de lote aceptado por el portal y el contenido real del ZIP.
3. Verificar: nº de imágenes por EAN, vistas, SHA-256 de originales en el NAS, miniaturas y visualización en Cloud, y
   que los casos dudosos fueron a revisión.
4. Solo con resultados correctos, valorar un driver real tras el contrato correspondiente y activar un proveedor con
   `pilot_cap` pequeño. Nada de esto se ha hecho.

## Odoo opcional: EAN desde pedidos de compra (`provider_odoo_orders.py`)

Secundario y de solo lectura (`SET TRANSACTION READ ONLY`, timeout de sentencia, límite de líneas 2000 por defecto y
10 000 máximo). Cloud funciona igual sin él; un fallo de Odoo solo muestra un error en ese apartado.

1. **Asociación explícita proveedor Odoo ↔ portal** (`provider_odoo_links.json` en `PROVIDER_DATA_ROOT`, sin secretos).
   Sin asociación confirmada la consulta se rechaza (`asociacion_proveedor_portal_no_configurada`). «Buscar candidatos»
   por nombre solo propone; hay que confirmar los ids y queda registrado el revisor y la fecha. Las marcas son
   informativas: el filtrado por marca requiere conocer el campo real de marca en Odoo y no se ha implementado.
2. **Consulta**: pedidos en estado `purchase` o `done` (se excluyen `draft`, `sent` y `cancel`), contactos comerciales del
   partner agrupados, líneas con producto, `default_code`, `barcode`, proveedor, pedido y fecha, más recientes primero.
3. **Vista previa**: EAN, Referencia, Producto, Proveedor, Pedido, Fecha, Imágenes (Completas/Parciales/Ausentes frente a
   `required_views` en la biblioteca común) y Estado (Elegible, Duplicado, Requiere revisión por falta de EAN o EAN inválido,
   Ya completa). Duplicado = EAN repetido o con solicitud en curso.
4. **Lote piloto**: «Proponer lote piloto» preselecciona 5–10 EAN elegibles (primero sin imágenes, luego parciales, pedidos
   recientes); el usuario ajusta la tabla, marca «Apruebo la selección definitiva» y pulsa «Crear solicitudes…».
   Solo se planifican EAN *Elegibles* de la vista previa (da igual lo marcado), mediante `plan_eans` del núcleo; no se
   contacta con ningún portal. Cada selección se anota en `provider_odoo_selections.jsonl` (aprobador, pedido, referencia).
5. Odoo solo prueba el origen del EAN: la imagen descargada sigue necesitando manifiesto, metadatos o revisión.

### Estado de la auditoría real (pendiente)

No se ha podido consultar el Odoo real desde el entorno de desarrollo (sin credenciales). Quedan por ejecutar con una
cuenta de solo lectura, y registrar en #59: `python scripts/audit_provider_odoo.py --partner marcolin=573` (contactos,
pedidos y EAN de MARCOLIN IBERICA), búsqueda de candidatos de Sáfilo y de las marcas NuORDER (Dita, Cutler, Akoni, Linda
Farrow). Hasta validarlo, ninguna asociación está guardada.

## Originales

`ImageRepository.save` escribe de forma atómica con verificación SHA-256 y sin sobrescribir; el núcleo solo escribe en
el NAS por ahí. Las miniaturas las genera la biblioteca existente (test: original byte a byte intacto).

