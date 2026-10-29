# Auditoría de la solicitud Marcolin nº 1

Fecha de la evidencia: 2026-10-29.

Esta auditoría usa la SQLite persistente de Cloud abierta en solo lectura por el
administrador. No contiene una copia de la base ni credenciales.

## Fuente y alcance

- Base consultada:
  `/opt/cloud-image-production/run-20261011T180000Z/masterdata/providers/provider_requests.sqlite`
- Solicitud consultada: `requests.id = 1`
- Tablas revisadas: `requests`, `request_eans` y `events`
- No se modificaron filas, estados, eventos ni datos de Odoo.

## Estado reconstruido

| Campo | Valor |
|---|---|
| Solicitud | 1 |
| Proveedor | `marcolin` |
| Modo | `portal` |
| Estado interno | `ready` |
| Intentos | 1 |
| `remote_id` | `NULL` |
| `last_error` | `NULL` |
| `next_poll_at` | `NULL` |
| `retry_of` | `NULL` |

El estado `ready` significa que la solicitud está preparada localmente. No
significa que haya sido entregada a Marcolin.

## EAN registrados

Los seis registros están activos y tienen el resultado local vacío:

| EAN/UPC | Resultado | Activo |
|---|---|---|
| `664689520060` | vacío | sí |
| `664689520084` | vacío | sí |
| `664689628810` | vacío | sí |
| `664689628827` | vacío | sí |
| `664689764686` | vacío | sí |
| `664689897605` | vacío | sí |

Estos códigos fueron utilizados anteriormente como referencias Tom Ford en
pruebas de Odoo. Su relación comercial con Marcolin debe confirmarse antes de
cualquier envío real.

## Historial y evidencia de envío

La solicitud solo tiene este evento:

| Evento | Valor |
|---|---|
| `id` | 1 |
| `request_id` | 1 |
| `from_state` | `NULL` |
| `to_state` | `ready` |
| `detail` | `6 EAN` |

No hay:

- identificador externo;
- evento de transición a `submitting`, `submitted` o `pending`;
- consultas de respuesta;
- resultados de imágenes recibidas;
- evidencia local de ZIP o descarga asociada;
- error registrado.

El contador `attempts = 1` no demuestra por sí solo que haya existido un
envío. Tampoco se puede descartar una operación externa que no se registrara
en Cloud.

### Clasificación respaldada

**Preparada — envío no acreditado.**

No es correcto afirmar que Marcolin no ha respondido, porque los datos no
acreditan que haya recibido la solicitud.

## Asociación Odoo y exportación

La asociación Marcolin-Odoo se resuelve mediante
`provider_odoo_links.json`. El proveedor Odoo `573` sigue siendo un candidato:
no se configura automáticamente y solo es válido tras confirmación
administrativa explícita.

Si falta esa asociación, la interfaz debe mostrar que **Marcolin no tiene
configurada su asociación con Odoo**. Esto es distinto de que Odoo no responda.
La exportación puede conservar los seis EAN como texto y marcar las filas sin
SKU como pendientes; regenerar el XLSX no crea otra solicitud ni modifica el
estado de la solicitud 1.

## Pendientes

1. Confirmar comercialmente que los seis EAN corresponden a Marcolin.
2. Confirmar de forma administrativa el proveedor Odoo correcto, sin asumir que
   es el `573`.
3. Generar y revisar el Excel desde Cloud con la conexión Odoo existente.
4. Registrar manualmente cualquier entrega al portal y su identificador externo.
5. Esperar una respuesta o ZIP verificable antes de importar imágenes.

No se debe reenviar ni marcar como enviada la solicitud 1 sin evidencia externa
verificable.
