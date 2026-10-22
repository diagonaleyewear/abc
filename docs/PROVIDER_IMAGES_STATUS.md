# Proveedores de imágenes: matriz de estado (#59)

Diseño y auditoría: PR #69 (`docs/PROVIDER_INTEGRATIONS_59.md`). Este documento refleja lo **implementado y probado
offline** (portales y Odoo simulados). No se ha accedido a ningún portal, no se ha descargado nada real, no hay
credenciales en el repositorio y los schedulers siguen desactivados.

## Componentes

| Módulo | Función |
|---|---|
| `provider_images.py` | Núcleo: SQLite durable con máquina de estados, un solo `request` vivo por EAN (índice único parcial), eventos append-only, demanda desde Odoo (solo lectura, por pedidos de compra del partner configurado y su familia comercial; nunca por marca/nombre), emparejamiento fichero↔EAN con evidencia, importación a `ImageRepository` (SHA-256, huella de píxeles, originales inmutables, procedencia en `metadata`), cola de revisión con cuarentena, ZIP seguro, límites diarios/piloto, backoff con jitter. |
| `marcolin_images.py`, `safilo_images.py`, `nuorder_images.py` | Conectores independientes sobre el núcleo. |
| `provider_images_ui.py` | Página admin «Proveedores de imágenes»: solicitudes, estado, errores, reintento, reconciliación, importación de ZIP, revisión. |
| `scripts/audit_provider_odoo.py` | Auditoría Odoo solo lectura (conteos, calidad EAN, EAN compartidos entre proveedores, candidatos por nombre *solo como propuesta*). |

Configuración no secreta: `masterdata_data/providers/provider_config.json`
(`{"marcolin": {"supplier_ids": [573], "max_eans_per_request": 50}, ...}`). Interruptor por proveedor:
`MARCOLIN_AUTOMATION_ENABLED`, `NUORDER_AUTOMATION_ENABLED`, `SAFILO_AUTOMATION_ENABLED` (por defecto `false`).
`run_cycle` es `dry_run` por defecto y no hace nada real sin el interruptor.

## Matriz por proveedor

| Capacidad | Marcolin | NuORDER | Sáfilo |
|---|---|---|---|
| Planificación de EAN desde Odoo (partner configurado) | ✅ probado | ✅ probado | ✅ probado |
| Solicitudes diferidas + seguimiento (`pending→available→imported`) | ✅ probado (sondeos con backoff, sin duplicados tras días) | n/a (manual) | ✅ síncrono (poll inmediato) |
| Sin duplicar peticiones pendientes | ✅ índice único en BD + planificador | ✅ | ✅ |
| Fallo ambiguo de envío → `needs_review`, nunca reenvío | ✅ | n/a | ✅ |
| Importación asistida (exportar EAN, subir ZIP descargado a mano) | ✅ | ✅ (único modo) | ✅ |
| Búsqueda masiva por lista de EAN con comas | n/a | n/a | ✅ lista y troceado (`max_eans_per_request`) |
| Todas las vistas por selección | n/a | n/a | ✅ contrato de driver y adaptador probados con driver simulado |
| Correspondencia demostrable fichero↔EAN | manifiesto, metadatos del portal o EAN completo en el nombre | manifiesto/metadatos | **solo manifiesto/metadatos** (nombre nunca basta) |
| Vista sin evidencia | `unknown` + revisión | idem | idem |
| Driver real de portal (HTTP/navegador) | ❌ no implementado | ❌ no implementado | ❌ no implementado |
| Requiere credenciales/acceso | sí (portal Marcolin) | sí (portal NuORDER) | sí (Safilo Omnia) |
| Pendiente de piloto real | sí | sí | sí |

## Bloqueos reales (no se automatiza hasta resolverlos)

- **Marcolin**: no confirmados método de acceso (API/web), términos de uso para automatizar, MFA/CAPTCHA, estructura
  del ZIP, nombres de fichero ni límite de EAN por solicitud (`max_eans_per_request=50` es provisional).
  Contrato del driver: `MarcolinDriver`; el vocabulario de estados del portal está en `map_portal_status` y debe
  validarse (cualquier texto desconocido pasa a `needs_review`).
- **NuORDER**: la documentación pública oficial indica que la API usa OAuth 1.0 y **la inicia la marca, ligada al
  portal de la marca**; no hay API de imágenes para minoristas documentada. Automatizar `app.next.nuorder.com`
  no está confirmado. Decisión: solo modo manual asistido; se necesita confirmación de NuORDER/de las marcas.
- **Sáfilo**: no confirmados acceso, restricciones de automatización de `youandsafilo.com`/`commportal.safilo.com`
  ni si la descarga incluye manifiesto. **Sin manifiesto todo queda en revisión**.
- **Odoo (todas)**: el partner 573 y los de NuORDER/Sáfilo deben verificarse con
  `python scripts/audit_provider_odoo.py --partner marcolin=573 --candidate-name dita ...` con cuenta de solo
  lectura y registrar los conteos en #59. Hasta entonces `supplier_ids` queda vacío y la demanda falla con
  `proveedor_odoo_no_configurado`.

## Cómo se garantiza la restricción «no modificar originales»

Los originales se guardan por `ImageRepository.save` (escritura atómica, verificación SHA-256, nombre por colisión
sin sobrescritura). Las miniaturas y exportaciones las genera la biblioteca existente y nunca tocan el original. El
núcleo no escribe en el NAS salvo mediante `ImageRepository`.

## Piloto (pendiente, requiere autorización)

1. Ejecutar la auditoría Odoo y aprobar `supplier_ids`.
2. Confirmar acceso/condiciones por portal y por escrito.
3. Implementar el driver aprobado detrás del contrato correspondiente.
4. Activar **un** proveedor con `pilot_cap` pequeño y `*_AUTOMATION_ENABLED=true` en el host; revisar resultados y
   cola de revisión antes de ampliar.
