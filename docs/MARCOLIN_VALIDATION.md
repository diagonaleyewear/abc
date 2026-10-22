# Marcolin: validación real (#59) — estado y procedimiento

Estado a 2026-10-22. **Nada de lo que sigue se ha validado contra Odoo ni contra el portal reales**: el entorno de
desarrollo no tiene la conexión de Cloud a Odoo (`DB_*` sin definir) ni acceso autorizado al portal Marcolin.

## 1. Auditoría de Odoo (solo lectura)

Ejecutar donde Cloud ya conecta a Odoo (usa `kering_jobs.db_engine`; no imprime ni guarda credenciales):

```
python scripts/marcolin_odoo_pilot.py --partner 573 --days 365 --size 8
```

Comprueba: que el partner 573 se llama MARCOLIN (el nombre solo se informa, **no guarda ninguna asociación**), su
familia de contactos comerciales, pedidos por estado, líneas de pedidos `purchase`/`done` (excluye cancelados y
borradores), referencias y EAN, sin EAN, EAN inválidos, duplicados y cobertura en la biblioteca (completas / parciales /
ausentes). Escribe `proposals/marcolin_pilot_<fecha>.json` y `.csv` en `PROVIDER_DATA_ROOT` con la tabla del lote
(EAN, referencia, pedido, fecha, estado de imágenes, proveedor, motivo) y estado `propuesta_pendiente_de_aprobacion`.
No crea solicitudes. Códigos de salida: 0 informe, 2 Odoo no disponible (bloqueo registrado), 3 el partner no es Marcolin.

Resultado de la ejecución en el equipo de desarrollo: **bloqueo `odoo_no_disponible`** (exit 2). Pendiente de ejecutar en
el host de Cloud y adjuntar el JSON (sin datos sensibles) en #59.

Después: revisar la propuesta, confirmar a mano la asociación en Cloud (Proveedores de imágenes → Marcolin → Odoo →
«Asociación…») y solo entonces aprobar el lote y crear solicitudes. Nada se envía al portal.

## 2. Qué le falta al driver de Marcolin

Existe solo el contrato (`MarcolinDriver`: `submit`, `status_text`, `download`, `requests`) y el adaptador
`MarcolinPortal`. No hay driver real. Para tenerlo hay que confirmar, con acceso autorizado y por escrito:

| Punto | Qué se necesita saber | Cómo afecta |
|---|---|---|
| Autenticación | usuario/contraseña, SSO, API o sólo web | decide HTTP frente a navegador |
| MFA / CAPTCHA | existencia y frecuencia | si los hay, sesión asistida por una persona (`InterventionRequired`), no automatizable |
| Condiciones de uso | si permiten automatizar | sin permiso, sólo modo manual |
| Introducción de EAN | pegado, CSV o selección; separador | `export_ean_list` ya emite lista separada por comas |
| Límite por solicitud | máximo real de EAN | hoy 50 provisional (`batch_size_verified=false`) |
| Envío e id de solicitud | dónde se muestra el id | se registra como `remote_id`; si no hay id, la reconciliación es manual |
| Estados del portal | vocabulario exacto | `map_portal_status`; los desconocidos van a revisión |
| Descarga | enlace, caducidad, cuándo disponible | `download()` y backoff de sondeo |
| ZIP | estructura, nombres, vistas | determina el emparejamiento con evidencia |
| Manifiesto | si existe y su formato | sin él, solo EAN completo en el nombre o revisión |

## 3. Procedimiento manual autorizado para el piloto (sin automatización)

1. Aprobar el lote en Cloud y crear solicitudes (estado `ready`; no se contacta con el portal).
2. Una persona con credenciales entra al portal Marcolin (Multimedia → solicitudes de activos), pega la lista de EAN
   exportada y envía la solicitud. **Anotar**: método de login, si hubo MFA/CAPTCHA, límite de EAN aceptado, id de
   solicitud, textos de estado, cuánto tarda y cómo se descarga.
3. Reconciliar el id en Cloud si se desea seguimiento; descargar el ZIP cuando esté disponible.
4. Guardar la estructura del ZIP (listado de nombres, manifiesto si lo hay) en #59 antes de importar.
5. Importar el ZIP en Cloud; lo ambiguo queda en revisión. Verificar originales, SHA-256, miniaturas y biblioteca.
6. Con esos datos se decide si procede un driver real; hasta entonces `MARCOLIN_AUTOMATION_ENABLED` sigue en `false`.
