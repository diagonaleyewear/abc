# Sprints de Implementacion: Alerta de Pedidos de Clientes Vigilados

## Contexto

Nuevo apartado en la app (Streamlit) para comprobar si algun cliente incluido
en una "lista de vigilancia" (mantenida por el usuario) tiene pedidos en Odoo,
y enviar un email de alerta cuando se detecte coincidencia.

## Decisiones confirmadas (respuestas del usuario)

| Pregunta | Decision |
|---|---|
| Trigger de comprobacion | Solo bajo demanda (boton). El cron se anade en un sprint posterior. |
| Que es "un pedido" | Cualquier `sale.order`, incluye borradores/cotizaciones. |
| Coincidencia de nombre | Nombre exacto en `res_partner.name` (cliente) O `res_partner.name` (dirección de entrega). No parcial, no email/NIF. |
| Origen de la lista vigilada | Archivo de configuracion simple (texto/JSON) en el servidor, editable desde la app. |
| Ventana temporal a revisar | Filtro de fechas configurable en la UI (Desde/Hasta), por defecto ultimos 30 dias. Sin historico de "ya alertado". |
| Cuenta de envio | `images@diagonaleyewear.com` via Graph API. Requiere anadir permiso `Mail.Send` en el registro de aplicacion de Azure (actualmente solo tiene permisos de lectura de correo). |
| Destinatario de la alerta | `roberto@diagonaleyewear.com` (configurable por variable de entorno). |
| Filtro de estado del pedido | Selector "Pendientes" / "Todos". Pendientes = `state = 'sale'` y (no facturado del todo o con lineas sin entregar por completo). Todos = sin filtro de estado. |
| Campo de fecha del filtro | `sale_order.date_order`. |

## Riesgo conocido / limitacion aceptada en MVP

Al no llevar historico de pedidos ya alertados, si se pulsa el boton varias
veces con el mismo pedido abierto todavia en Odoo, se volvera a enviar la
alerta para ese mismo pedido. Es un comportamiento aceptado para el MVP
(uso manual, bajo demanda). Se resuelve en el Sprint 4 (cron) anadiendo
un registro de "ultima ejecucion / IDs ya notificados".

## Preguntas todavia abiertas (a confirmar antes de Sprint 2)

- ?El permiso `Mail.Send` para `images@diagonaleyewear.com` ya esta disponible
  en Azure AD o hay que solicitarlo a IT/administrador del tenant?
- ?"Pedidos abiertos" excluye los cancelados (`state = 'cancel'`) o se
  revisan literalmente todos los `sale_order` sin filtrar por estado?
- Formato exacto de la lista de vigilancia: ?nombres exactos tal cual figuran
  en `res_partner.name`, uno por linea/JSON, sin mayusculas/tildes especiales
  a normalizar?
- ?El boton vive dentro del apartado nuevo (pagina propia) o dentro de una
  pagina existente (ej. Configuracion)?

---

## Sprint 0 - Cierre funcional (0.5 dia)
Objetivo: cerrar el resto de decisiones pendientes antes de programar.

Tareas:
- Confirmar disponibilidad del permiso `Mail.Send` en el App Registration
  de `images@diagonaleyewear.com` (Azure Portal > API permissions).
- Confirmar filtro de estado de pedido (todos vs excluir cancelados).
- Confirmar formato del fichero de lista de vigilancia (JSON propuesto:
  `{"clientes": ["Nombre Exacto 1", "Nombre Exacto 2"]}`).

Entregable:
- Especificacion funcional v1.0 aprobada (variante de este documento).

Criterio DoD:
- No quedan preguntas abiertas de la seccion anterior.

## Sprint 1 - Gestion de la lista de vigilancia (0.5-1 dia)
Objetivo: permitir al usuario mantener la lista de nombres desde la app.

Tareas:
- Crear `watchlist_clientes.json` (fichero de configuracion en servidor).
- Modulo `watchlist_config.py`: `load_watchlist()`, `add_customer()`,
  `remove_customer()`, `save_watchlist()`.
- Nueva pagina Streamlit "Alerta Pedidos" con formulario para anadir/quitar
  nombres y ver la lista actual.

Entregable:
- Pagina funcional donde el usuario gestiona la lista sin tocar codigo.

Criterio DoD:
- Anadir/eliminar un nombre persiste en el fichero y se refleja al recargar.

## Sprint 2 - Comprobacion contra Odoo (1 dia) - IMPLEMENTADO
Objetivo: consultar `sale_order` + `res_partner` en la BD de Odoo y detectar
coincidencias exactas con la lista de vigilancia en cliente O dirección de entrega.

Tareas:
- Query SQL (via `db_config.py` / patron de `db_loader.py`) que devuelve
  pedidos cuyo `res_partner.name` (cliente) o `partner_shipping_id.name`
  (dirección de entrega) coinciden exactamente con algun nombre de la lista,
  con filtro de rango de fechas (`date_order`) y filtro de estado
  (Pendientes/Todos).
- Modulo `check_pedidos_vigilados.py` con funcion
  `find_matching_orders(clientes, fecha_desde, fecha_hasta, solo_pendientes)
  -> DataFrame` (pedido, cliente, dirección_entrega, fecha, estado,
  invoice_status, total).
- Boton "Comprobar ahora" en la pagina nueva, con selectores de fecha
  Desde/Hasta y radio "Pendientes"/"Todos", que ejecuta la consulta y
  muestra resultado en tabla (o "sin coincidencias").

Entregable:
- Boton operativo que detecta coincidencias reales contra Odoo con filtros
  de fecha, estado y busqueda dual (cliente + direccion de entrega).

Criterio DoD:
- Prueba manual con un nombre de cliente conocido con pedido abierto
  devuelve resultado correcto; con nombre sin pedidos devuelve vacio.
- Cambiar el filtro de fechas o el estado (Pendientes/Todos) cambia el
  resultado mostrado.
- La búsqueda encuentra también pedidos donde el nombre figura en la
  dirección de entrega, no solo en el cliente principal.

## Sprint 3 - Envio de alerta por email (0.5-1 dia) - IMPLEMENTADO
Objetivo: enviar email via Graph API (`images@diagonaleyewear.com`) cuando
el boton detecta coincidencias.

Tareas:
- Permiso `Mail.Send` concedido en el App Registration (confirmado por el usuario).
- Funcion `send_alert_email()` en `graph_mail_downloader.py` usando endpoint
  `POST /users/{mailbox}/sendMail` con el mismo flujo de client credentials
  ya existente.
- Cuerpo del email con tabla de pedidos detectados (cliente, numero de
  pedido, fecha, estado, estado factura, importe).
- Destinatario configurable via variable de entorno `ALERT_RECIPIENT_EMAIL`
  (default `roberto@diagonaleyewear.com`).
- Integrado en el flujo del boton "Comprobar ahora" (solo si hay
  coincidencias).

Entregable:
- Boton que, al detectar coincidencias, envia el email y muestra
  confirmacion en la interfaz.

Criterio DoD:
- Email recibido correctamente en un envio de prueba con datos reales.

## Sprint 4 - Programacion via cron (0.5-1 dia)
⚠️ SUSTITUIDO por el servicio `abcd-order-alerts` (ver Sprint 5)

El cron del host nunca se llegó a instalar desde el despliegue (`deploy.yml` solo programa
la foto semanal), por lo que las alertas solo salían con «Comprobar ahora». El despliegue
retira ahora cualquier línea de crontab que invoque `run_alerta_pedidos.py` para que cron
y servicio no se ejecuten a la vez.

Objetivo original: automatizar la comprobacion periodica sin intervencion manual.

Tareas:
- Script standalone `run_alerta_pedidos.py` (equivalente a
  `poll_luxoptica_mail.py` / `scheduler_luxoptica.py`) que ejecuta la misma
  logica de los Sprints 2-3 (solo pedidos "Pendientes").
- Registro de pedidos ya notificados (hoy en SQLite compartido, issue #19;
  `alerta_pedidos_notificados.json` solo se importa como legacy).

## Sprint 5 - Servicio Docker de alertas automáticas - IMPLEMENTADO

Contenedor independiente `abcd-order-alerts` (misma imagen que la app) que ejecuta
`python run_alerta_pedidos.py --loop`. No depende de que nadie tenga la app abierta.

### Reglas de negocio (sin cambios)

| Aspecto | Automático (`abcd-order-alerts`) | Manual («Comprobar ahora») |
|---|---|---|
| Estado del pedido | **Solo pendientes**: `state = 'sale'` y (no facturado del todo o con líneas sin entregar). Es `PENDING_CONDITION` de `check_pedidos_vigilados.py`. | Selector Pendientes/Todos |
| Ventana de fechas | Últimos `ALERTA_PEDIDOS_DIAS_ATRAS` días (180 por defecto) hasta hoy | Desde/Hasta de la UI (30 días por defecto) |
| Coincidencia | Nombre exacto de cliente o dirección de entrega | Igual |
| Destinatarios | `ALERT_RECIPIENT_EMAIL` (por defecto roberto@) + virginia.nunez@ | Igual (`order_alerts.alert_recipients`) |
| Lista de clientes | `WATCHLIST_PATH` (`/app/data/watchlist_clientes.json`) | Igual |
| Registro de notificados | `ORDER_ALERT_STATE_PATH` (`/app/data/order_alerts.sqlite3`) | Igual |
| Reintentos de envío | Máx. `ALERTA_PEDIDOS_MAX_INTENTOS_ENVIO` (5) por pedido | Sin límite (acción explícita) |

El automático **no** busca borradores ni pedidos ya facturados y entregados. Cambiar esa
regla requiere aprobación explícita.

### Deduplicación compartida (issue #19)

- Manual y automático usan `order_alerts.dispatch_order_alerts` sobre el mismo SQLite.
- Cada pedido se reserva en una transacción `BEGIN IMMEDIATE` (lease de 10 min): dos
  ejecuciones simultáneas no envían el mismo pedido.
- Solo se marca `sent` tras una respuesta 2xx de Graph `sendMail`. Un fallo lo deja `failed`
  con el error y es reintentable. Un timeout ambiguo se trata como fallo: puede repetirse
  un email, pero nunca se marca como notificado un pedido no confirmado.
- Tras `ALERTA_PEDIDOS_MAX_INTENTOS_ENVIO` fallos el automático deja de reintentarlo y lo
  muestra como error; «Comprobar ahora» puede reenviarlo.

### Configuración

| Variable | Defecto | Uso |
|---|---|---|
| `ALERTA_PEDIDOS_INTERVALO_MINUTOS` | 5 | Intervalo entre comprobaciones (mín. 1) |
| `ALERTA_PEDIDOS_DIAS_ATRAS` | 180 | Ventana de búsqueda del automático |
| `ALERTA_PEDIDOS_REINTENTOS_CONSULTA` | 3 | Intentos de consulta a Odoo por ciclo (esperas 10 s y 30 s) |
| `ALERTA_PEDIDOS_MAX_INTENTOS_ENVIO` | 5 | Intentos automáticos de envío por pedido |
| `DB_*`, `M365_*`, `ALERT_RECIPIENT_EMAIL` | — | Mismas variables (anclas YAML) que `abcd-app` |

### Resiliencia

- `restart: unless-stopped` (`always` en producción): arranca con Docker tras reiniciar el
  servidor. Requiere `systemctl enable docker` en el host.
- Un error de Odoo, Graph o inesperado se registra y el bucle sigue en el siguiente ciclo.
- `SIGTERM` (`docker compose stop`) detiene el bucle de inmediato y registra la parada.
- Healthcheck `python run_alerta_pedidos.py --healthcheck`: unhealthy si no hay latido en
  `2 × intervalo + 5 min`. Odoo caído **no** marca unhealthy (el proceso sigue vivo).

### Pantalla «Alerta Pedidos»

La sección «Envío automático» muestra última comprobación, último envío y último error, y
distingue:
- **Activo** – latido reciente; p. ej. «Sin pedidos pendientes» o «Sin pedidos nuevos».
- **Activo con error** – la última comprobación falló (Odoo/Graph).
- **Detenido** – parada ordenada registrada (`docker compose stop`).
- **Sin actividad** – sin latido reciente: proceso caído o bloqueado.
- **Nunca iniciado** – el servicio no se ha desplegado.

### Persistencia y permisos en producción

- Lista de clientes y registro SQLite viven en `./masterdata_data` → `/app/data`, montado en
  `abcd-app` y `abcd-order-alerts`. Está en `.gitignore`, por lo que el `git reset --hard`
  del despliegue ya no pisa la lista editada desde la app.
- `./watchlist_clientes.json` y `./alerta_pedidos_notificados.json` se montan `:ro` solo
  para migrar: mientras no exista `/app/data/watchlist_clientes.json` se lee el legacy; el
  primer guardado desde la app crea el nuevo fichero (escritura atómica).
- El servicio corre con `read_only: true`, `tmpfs /tmp` y `no-new-privileges`; en producción
  además `cap_drop: ALL`. Sin `CAP_DAC_OVERRIDE`, root del contenedor solo escribe si
  `./masterdata_data` es de root:
  ```bash
  sudo chown -R root:root /opt/abcd-control/masterdata_data
  sudo chmod 750 /opt/abcd-control/masterdata_data
  ```
- Si el volumen no es escribible el servicio sale con código 2 y el mensaje
  «no se puede usar el registro persistente…» en los logs.

**Antes del primer despliegue**, si se editó la lista en el servidor, copiarla al volumen:
```bash
cd /opt/abcd-control
[ -f masterdata_data/watchlist_clientes.json ] || sudo cp watchlist_clientes.json masterdata_data/
```

### Operación

```bash
cd /opt/abcd-control
docker compose up -d abcd-order-alerts                 # arrancar / actualizar
docker compose ps abcd-order-alerts                    # estado y health
docker compose logs -f --tail=100 abcd-order-alerts    # un registro por ciclo
docker compose exec abcd-order-alerts python run_alerta_pedidos.py --healthcheck
docker inspect --format '{{json .State.Health}}' abcd-order-alerts
docker compose restart abcd-order-alerts
docker compose stop abcd-order-alerts                  # la UI mostrará «Detenido»
crontab -l | grep run_alerta_pedidos || echo "sin cron legacy"
```

Ejemplos de log:
```
[2026-10-02 09:00:00] Servicio de alertas iniciado. Intervalo: 300s.
[2026-10-02 09:00:01] Comprobación: sin_pedidos. 0 pedidos pendientes (últimos 180 días)
[2026-10-02 09:05:02] Comprobación: enviado. 3 pendiente(s) (últimos 180 días), 1 nuevo(s)
[2026-10-02 09:10:31] Comprobación: error_consulta. 5 cliente(s) vigilado(s). ERROR: Consulta Odoo tras 3 intento(s): ...
```

`python run_alerta_pedidos.py` sin argumentos sigue ejecutando una única comprobación, pero
se omite si el servicio tiene latido reciente (usar `--force` solo para diagnóstico; la
deduplicación sigue aplicándose).

