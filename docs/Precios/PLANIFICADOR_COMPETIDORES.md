# Planificador de precios de competidores: guía operativa

Estado: **implementado y probado, NO activado**. No existe servicio Docker, cron ni tarea programada.
Solo se ejecuta cuando una persona lanza el comando. El Compose de producción no se ha modificado.
Activarlo en producción requiere autorización expresa (ver «Integración posterior»).

Código: [price_scheduler.py](../../price_scheduler.py). Tests: [test_price_scheduler.py](../../tests/test_price_scheduler.py).

## Qué hace

- Un ciclo por competidor, con **frecuencia propia en horas** (`interval_hours`, 1 h – 30 días).
- **Cola persistente** (`scheduler_jobs`, un job por EAN) en el SQLite del comparador. Un reinicio no pierde
  trabajo: los jobs `running` huérfanos vuelven a `pending` y la cola continúa.
- **Límites por competidor**: peticiones por ejecución (`max_requests_per_run`), pausa entre peticiones
  (`pause_seconds`), reintentos con backoff exponencial (`max_attempts`). El trabajo del ciclo se reparte en
  el 80 % del intervalo.
- **Concurrencia**: un bloqueo de fichero por competidor (`<base>.competitor-<id>.lock`). Una segunda
  ejecución del mismo competidor responde `busy` sin consultar nada. El bloqueo lo libera el sistema
  operativo si el proceso muere, así que nunca queda «atascado».
- **Registro**: `scheduler_runs` (inicio, fin, estado, peticiones, cambios, sin cambio, errores, mensaje) y
  `competitor_schedule` (`next_run_at`, `last_run_at`, `last_ok_at`, suspensión y motivo).
- **Seguridad**: un competidor no se ejecuta hasta tener conector, nota de autorización y verificación
  correcta. Un bloqueo del proveedor (HTTP 401/403/407/429) **suspende** el conector sin insistir.
  Solo existe el conector `feed_csv` (feed autorizado HTTPS o CSV en la carpeta de feeds). No hay scraping.

## Dónde vive el estado

| Elemento | Ruta en el contenedor `abcd-control` | Ruta en el host |
|---|---|---|
| Base SQLite | `$PRICE_COMPARATOR_DB_PATH` (por defecto `/app/data/price_comparator.sqlite3`) | `./masterdata_data/` |
| Bloqueos | `<base>.competitor-<id>.lock` y `<base>.schema.lock` | `./masterdata_data/` |
| Feeds en fichero | `/app/data/competitor_feeds/` (`$PRICE_COMPARATOR_FEEDS_DIR`) | `./masterdata_data/competitor_feeds/` |

En los ejemplos, `D="docker exec abcd-control"`. En local (sin Docker) basta quitar el prefijo y definir
`PRICE_COMPARATOR_DB_PATH`.

## Ejecutar

Todo a mano; ninguna orden crea procesos que sobrevivan al comando salvo `--loop`.

```
# 1. Estado, plan y registro (solo lectura)
docker exec abcd-control python price_scheduler.py --status
docker exec abcd-control python price_scheduler.py --plan <id>
docker exec abcd-control python price_scheduler.py --runs 20

# 2. Configurar un competidor (las opciones omitidas conservan su valor)
docker exec abcd-control python price_scheduler.py --configure <id> --connector feed_csv \
    --source feed-competidor.csv --authorization-note "Autorizado por X el AAAA-MM-DD" \
    --interval-hours 12 --max-requests 20 --pause-seconds 2 --max-attempts 3

# 3. Verificar el acceso y habilitar (cambiar conector o fuente obliga a verificar de nuevo)
docker exec abcd-control python price_scheduler.py --verify <id>
docker exec abcd-control python price_scheduler.py --enable <id>

# 4. Ejecución manual de prueba: una pasada, sale con 0 (ok) o 1 (error/bloqueo)
docker exec abcd-control python price_scheduler.py --once --competitor <id> --force
```

`--force` inicia un ciclo aunque no toque todavía; sin él, solo se actúa si `next_run_at` ha vencido.
`--workers N` procesa varios competidores en paralelo (uno por competidor). `--with-odoo` incluye además la
sincronización de precios Vytria desde Odoo si está activada y toca por su frecuencia.

Modo continuo (solo con autorización; ver más abajo): `python price_scheduler.py --loop --tick-seconds 300`.

### Prueba segura recomendada (sin consultar a terceros)

1. Copiar un CSV (`ean,precio,moneda,disponibilidad,url`) a `competitor_feeds/` del volumen.
2. `--configure <id> --connector feed_csv --source fichero.csv --authorization-note "Prueba interna"`.
3. `--verify`, `--enable`, `--once --force`, `--status`, `--runs`.
4. Repetir `--once --force`: debe dar «sin cambio» y no duplicar ofertas.
5. `--disable <id>` al terminar.

Requisito de datos: el competidor debe tener mercado informado; si no, las ofertas se rechazan con
«El mercado es obligatorio» (se ve en `--runs`).

## Verificar

- `--status`: por competidor, estado (Activo / En pausa / Suspendido / Desactivado / Pendiente…), cola
  pendiente, frecuencia, **próxima actualización**, última correcta y último intento; y «En ejecución ahora».
- `--runs N`: últimas ejecuciones con estado (`ok`, `partial`, `error`, `blocked`), peticiones, cambios,
  errores y mensaje.
- Pestaña **Configuración** del comparador: mismos datos y edición individual por competidor.
- Consulta directa (solo lectura):
  `docker exec abcd-control python -c "import sqlite3,os;c=sqlite3.connect('file:'+os.environ.get('PRICE_COMPARATOR_DB_PATH','/app/data/price_comparator.sqlite3')+'?mode=ro',uri=True);print(c.execute('select status,count(*) from scheduler_jobs group by 1').fetchall())"`

## Detener

| Objetivo | Orden |
|---|---|
| Que un competidor no se ejecute más (la cola se conserva) | `--pause <id>` (se reanuda con `--resume <id>`) o `--disable <id>` |
| Parar todo | `--disable <id>` de cada competidor habilitado |
| Parar un `--once` o `--loop` en curso | Ctrl+C, o `docker top abcd-control` para ver el PID y `docker exec abcd-control kill -TERM <pid>` |

SIGINT/SIGTERM terminan **tras la petición actual** (la pausa entre peticiones se interrumpe en segundos).
La cola queda intacta y continúa en la siguiente pasada. Un `docker stop`/reinicio del contenedor también es
seguro: ver «Recuperar».

## Recuperar

- **Tras un reinicio o caída**: no hace falta ninguna orden especial. Basta relanzar `--once` (o `--loop`):
  los jobs `running` huérfanos pasan a `pending`, la ejecución interrumpida se marca `error`
  («Interrumpida antes de terminar») y la cola sigue donde estaba.
- **«busy»**: hay otra ejecución real del mismo competidor. Espere o deténgala; no borre ficheros `.lock`.
- **Suspendido** (bloqueo del proveedor): `--status` muestra el motivo. Revise con el proveedor y reanude
  con `--resume <id>`. Los jobs pendientes se conservan y no se consultó de nuevo.
- **Jobs en `error`** (agotaron reintentos): se reintentan en el siguiente ciclo (cada ciclo crea jobs
  nuevos). Para forzarlo ya: `--once --force --competitor <id>`.
- **Base corrupta o ausente**: restaurar `price_comparator.sqlite3` desde la copia del volumen
  `masterdata_data` (el planificador recrea el esquema al abrirla).
- **Error de almacenamiento al arrancar**: revisar `PRICE_COMPARATOR_DB_PATH` y permisos de escritura.

## Integración posterior (no incluida)

El comando es idempotente y seguro de lanzar repetidamente, por lo que se integra sin cambios de código con:

- un cron/timer del host o de Cloud: `docker exec abcd-control python price_scheduler.py --once`
  cada 5–15 min (la frecuencia real de cada competidor la decide `interval_hours`);
- o un servicio Compose con `command: python price_scheduler.py --loop --tick-seconds 300`.

Ambas opciones requieren autorización expresa y tocar el despliegue; hoy no está hecho. Antes de activar:
tener al menos un competidor con feed autorizado verificado, y revisar `--plan` para comprobar que el volumen
de EAN cabe en el intervalo (`fits_interval`).
