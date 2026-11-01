# Integración de proveedores de imágenes (#59): auditoría inicial y diseño

Estado: **diseño, sin implementación**. Auditoría solo de código y documentación; no se han
hecho descargas, accesos a portales, consultas a Odoo ni pruebas con credenciales. Nunca
incluir credenciales, tokens ni URLs con token en el repositorio, issues, logs ni fixtures.

## 1. Auditoría del código existente

| Pieza | Ubicación | Reutilización |
|---|---|---|
| Lectura de pedidos de compra por proveedor | `kering_images.read_orders`, `read_suppliers`, `read_supplier_contacts` | Misma consulta (`purchase_order` → `purchase_order_line` → `product_product.barcode`); el filtro por `commercial_partner_id` ya agrupa partners hijos. Extraer a un módulo común sin cambiar Kering. |
| Orquestación con estados, locks y reintentos | `kering_jobs.KeringScheduler`, `process_pending` | Patrón para un scheduler por proveedor, desactivado por defecto. |
| Repositorio común por EAN | `image_repository.ImageRepository.save(ean, name, content, provider=, origin=, view=, market=, metadata=)` | Único destino de imágenes: checksum SHA-256, huella de píxeles, deduplicación, originales inmutables, `sync_nas`. |
| Nomenclatura y vistas | `image_naming`, `kering_media` | Vistas solo con evidencia; el resto `unknown` + revisión. |
| Miniaturas / biblioteca / ZIP | `image_thumbnails`, `image_library`, `ImageRepository.zip_eans*` | Sin cambios; filtran por `provider`. |
| Almacenamiento y seguridad de rutas | `repository_storage`, `image_work_storage` | Staging de descargas y comprobación de capacidad. |
| Panel de procesos | `process_activity.record_process`, `cloud_dashboard` | Un proceso por proveedor. |
| Correo Graph | `graph_mail_downloader` | Posible canal si el portal notifica aprobaciones por email (no confirmado). |

Hallazgo: en el repositorio **no existe código de Marcolin, NuORDER ni Sáfilo** para imágenes. La
única referencia es el flujo n8n `n8n/Trasformación Pedidos More.json`, que transforma ficheros
de albarán de Marcolin (`SHIP_*`) y fija `Proveedor: "MARCOLIN IBERICA, S.A."` como cadena de texto.
No sirve para identificar proveedores ni productos de imágenes.

## 2. Relación con proveedores reales de Odoo (por verificar, solo lectura)

Regla: **no clasificar por marca ni nombre; no asociar marcas de NuORDER/Sáfilo al partner 573**.

Consultas de solo lectura a ejecutar con una cuenta de lectura y registrar los resultados (conteos,
sin datos personales) en la issue:

1. `res_partner` por ID 573 y su `commercial_partner_id`; partners hijos (`commercial_partner_id = <commercial de 573>`).
2. Proveedores candidatos de NuORDER y Sáfilo: `res_partner` con `supplier_rank > 0` cuyos nombres
   sugieran Dita, Cutler and Gross, Akoni, Balmain, Linda Farrow, Jacquemus, Safilo (Carrera,
   Carolina Herrera, David Beckham). Confirmarlo con el equipo de compras; el nombre solo propone candidatos.
3. Vínculo producto–proveedor por **tres vías**, comparando cobertura:
   - `product_supplierinfo` (`partner_id` → `product_tmpl_id`/`product_id`);
   - `purchase_order_line` de pedidos con `partner_id` en la familia comercial (vía usada por Kering);
   - `stock_move`/recepciones, si se quiere cobertura de histórico.
4. Calidad de EAN: nulos, longitud ≠ 8/12/13/14, dígito de control inválido, duplicados entre
   variantes, EAN compartidos entre proveedores (conflicto → `needs_review`).
5. Marcas citadas que aparezcan en productos de **otros** proveedores (control negativo exigido por los
   criterios de aceptación).

Resultado esperado: una tabla `proveedor Odoo → portal → marcas`, aprobada por el cliente, almacenada
como configuración (no en código) con el ID de partner como clave.

## 3. Diseño común

Un núcleo independiente del portal y un adaptador por proveedor. No se crea otra librería de imágenes.

```
Odoo (solo lectura) ──► ProviderDemand ──► RequestPlanner ──► ProviderPortal (adaptador)
                                             │                    │ submit/poll/download
                                  SQLite durable (requests)       ▼
                                             └──────────► staging ► ImageRepository.save ► NAS
```

- **ProviderDemand**: EAN normalizados y deduplicados por proveedor (reutiliza `read_orders`, rango
  temporal y criterio Kering) menos los EAN con vistas completas en el repositorio y los EAN en solicitudes vivas.
- **Persistencia**: SQLite en `masterdata_data` (como `order_alerts`) con `provider_requests`
  (id, provider, remote_id, state, attempts, last_error, created/updated, next_poll_at) y
  `provider_request_eans` (request_id, ean, result). Transiciones transaccionales (`BEGIN IMMEDIATE`) y
  **append-only de eventos**, igual que el registro de entregas de #19.
- **Estados** (de la issue): `detected → ready → submitted → pending → available → downloaded → imported`,
  más `partial`, `failed`, `needs_review`. Reglas: nunca enviar dos veces el mismo EAN mientras
  exista una solicitud viva; un fallo de red tras enviar una solicitud es **ambiguo** (mismo criterio que
  #19): pasa a `needs_review` y se reconcilia consultando "My pending requests", no se reenvía.
- **Interfaz del adaptador** (mínima): `submit(eans) -> remote_id`, `status(remote_id) -> state`,
  `download(remote_id) -> manifest + ficheros`. Los errores se tipifican como en `kering_images`
  (`LoginFailed`, `SessionExpired`, `InterventionRequired`, …) para que CAPTCHA/MFA detengan la automatización.
- **Importación**: validar cada fichero (tipo real, tamaño, píxeles) y su EAN con evidencia
  (manifiesto/metadatos). Guardar con `provider="marcolin|nuorder|safilo"`, `origin` = id de solicitud y
  `view` solo si hay evidencia; en caso contrario `unknown` y revisión manual. Sin renombrados arbitrarios.
- **Operación**: `dry-run` por defecto, lote piloto con tope configurable, interruptor por proveedor
  (`<PROVIDER>_AUTOMATION_ENABLED=false`), sondeo con backoff y jitter, límite de solicitudes por día,
  comprobar/reintentar manual en la UI, y credenciales solo en secretos del host (nunca en GitHub/CI).

## 4. Proveedores

### Marcolin (partner 573 a verificar)
Flujo documentado: Multimedia → Assets requests → Product images + marcas → lista de EAN → validación →
confirmaciones → *Profile → Requests* (pendientes/disponibles) → descarga (horas o días).
Modelo asíncrono por solicitud, encaja directamente con `submitted/pending/available`.
**Desconocido**: API, estructura del ZIP, nombres de fichero, límite de EAN por solicitud, MFA/CAPTCHA,
condiciones de uso de la automatización. Hacerlo primero: es el único con flujo asíncrono completo y
con proveedor ya identificado.

### NuORDER (app.next.nuorder.com)
Grupos: Dita, Cutler, Akoni (Akoni y Balmain), Linda Farrow (Linda Farrow y Jacquemus). Sin pasos
documentados: la fase 1 es una exploración manual autorizada de selección por EAN, descarga masiva,
vistas y nomenclatura, y comprobar si ofrece API oficial (preferible a automatizar la web).
No asumir que comparte modelo de solicitudes con Marcolin.

### Sáfilo (youandsafilo.com / commportal.safilo.com, Safilo Omnia)
Flujo: búsqueda masiva por lista de EAN con coma → *Add to selection* → selección → *download all selected*
(todas las vistas; la descarga desde ficha da una sola vista). Es síncrono/interactivo.
**Riesgo principal**: nombres de fichero poco reconocibles → exigir manifiesto o metadatos del portal que
demuestren fichero–EAN–vista; si no existen, todo queda en `needs_review` (nunca inferir por orden).
Pendiente confirmar el comportamiento de la URL de búsqueda avanzada aportada y su compatibilidad con automatización.

## 5. Plan de PR (una responsabilidad cada una; ninguna mezcla #14/#19)

1. **Auditoría Odoo read-only** (script de solo lectura + informe sin datos personales) — sin cambios de runtime.
2. **Núcleo**: esquema SQLite, máquina de estados, `ProviderDemand` y pruebas offline (idempotencia,
   concurrencia, reinicio, EAN inválidos). Sin red.
3. **Marcolin**: adaptador con portal simulado + importación al repositorio + pruebas de partial/late approval.
4. **UI + scheduler** (deshabilitados por defecto) y proceso en el panel.
5. **Piloto Marcolin** con autorización expresa y pocos EAN reales; documentar resultados.
6. **NuORDER** y **Sáfilo**: una issue hija y una PR por conector tras su exploración autorizada.

## 6. Riesgos y decisiones pendientes

- Autorización contractual para automatizar los portales (términos de uso) y existencia de API oficial.
- MFA/CAPTCHA: si aparecen, el conector queda en modo asistido (`InterventionRequired`).
- Rotar las contraseñas compartidas en conversaciones; crear cuentas de servicio por portal y secretos separados.
- Mapa proveedor Odoo ↔ marcas aprobado por el cliente (sección 2).
- Límites de solicitud por portal y política de repetición para EAN no encontrados.
- Ubicación del estado (SQLite en `masterdata_data`) y su inclusión en las copias de seguridad.
