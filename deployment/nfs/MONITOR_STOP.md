# Parada drenada de Luxoptica y preparacion del artefacto

Continua [PRECOPY.md](PRECOPY.md). No se repite la pausa productiva ni se
despliega en esta entrega. La candidata sigue no migrable y los originales
intactos. La ventana anterior termino abortada por salida137 del monitor.

## Reproduccion con el artefacto realmente activo

Imagen exacta observada:
`sha256:5af62fade21e88c9f3fd57f23d08eb2e7b535fcb63f9933598a7a7c86ecb11e8`.
Comando exacto:

```sh
python poll_luxoptica_mail.py --interval-minutes 10 \
  --sender-hint luxottica --subject-hint image \
  --lookback-days 30 --top-messages 100
```

Contenedor separado, sin red, sin secretos ni mounts productivos, runtime
read-only, cap-dropALL. Una fixture opt-in sustituye solo Graph y recibos por
un ciclo vacio sintetico, sin cambiar el monitor. El ciclo termino y alcanzo
el reposo. SIGTERM no produjo salida; Docker stop3s termino137.
Inicio23:32:26.974307847Z/final23:32:32.239807501Z del2026-10-10UTC.
No se enviaron correos ni consultaron proveedores/Odoo. Logs privados en
`/home/rubensg/cloud-nfs-preparation-20261010/monitor-stop-20261010T233000Z/`.

Python eraPID1. Su codigo no registrabaSIGTERM y dormia con `time.sleep`.
La reproduccion de un ciclo vacio prueba que no hace falta una descarga
activa, un hijo o una conexion SQLite para reproducir137.

## Implementacion

- SIGTERM/SIGINT solo fijan un Event: el handler no hace I/O, no lanza
  excepciones asincronas y no corta transacciones.
- No iniciar otro ciclo y despertar inmediatamente del reposo con Event.wait.
  Al drenar una unidad ya iniciada, cerrar sus conexiones/locks antes de salir.
- `service_stop.py` aplica checkpoints solo dentro del contexto del monitor:
  entre correos, adjuntos, entradasZIP, variantes de mercado y chunks recibidos.
  No interrumpir `ImageRepository.save`, commit o publicacion atomica a medias.
- Esperas de FileLock del importador/repositorio se comprueban cada0,1s sin
  robar el lock. Sin contexto de monitor mantienen el comportamiento anterior.
- Timeout HTTP/SQLite5s en el contexto del monitor; Odoo connect5s y
  statement_timeout5s. No replay de escrituras/envios. Conexiones SQLite,
  respuestas HTTP y engine Odoo se cierran; errores reales se propagan.
- Cancelacion de stream elimina solo el temporal nuevo `.part`, no archivos
  originales o alternativas. ZIP completo conservado; entradas ya publicadas
  se reutilizan mediante catalogo/checksum/procedencia al reintentar.
- Tras completar sus imagenes/catalogo, el correo se registra atomicamente
  en el JSON local: fsync del archivo y directorio antes de marcar
  leido en Graph. `pending_read_ids` permite repetir SOLO el acuse idempotente
  si Graph falla o se solicita parada. No repetir procesamiento ya completado.
  Un correo parcialmente procesado no se declara completado.
- Recibos operativos terminan `Parcial`/StopRequested ante cancelacion
  cooperativa. Un fallo real durante parada sale1, no0. Si la operacion
  finalmente retorna pero el drenaje excedio30s, tambien sale1.
- Compose prepara SIGTERM/40s para dejar margen al presupuesto30s y cierre,
  sin cambiar contenedores activos. No es la solucion por si sola: el
  artefacto antiguo siguio fallando incluso con90s.

El monitor no lanza procesos hijos para descargar/extraer. Los ensayos
SQLite usan un hijo independiente que mantiene una transaccion y verifican
su rollback, salida0 y wait; no quedan hijos o recibos activos. Playwright
no forma parte de este ciclo.

## Ensayos y limites

`tests/test_luxoptica_stop.py` y `scripts/smoke_luxoptica_stop.py` prueban
SIGTERM real en procesos independientes: reposo, stream incompleto, espera
SQLite real ante escritor independiente y fsync NFS simulado. Se cancela
el stream antes de publicar; SQLite/fsync drenan la unidad atomica tras
liberacion controlada, sin una excepcion inyectada dentro del commit.

Primera prueba POSIX, capa auxiliar sobre la imagen activa con codigo nuevo:
reposo0,251s, descarga0,415s, SQLite0,465s y fsync0,365s; todas salida0,
reinicio correcto, cero recibos sin terminar y un solo registro por imagen.
Ese ensayo auxiliar como propietario NO sustituye el smoke del Dockerfile
completo1037:100 ni la comprobacion DockerPID1 del comando exacto en CI.
CI incorpora ambas comprobaciones, sin red, secretos o mounts de produccion.
Tambien prueba fallos/deadline sin convertirlos en salida limpia, handler
restaurado, lock contendido, limpieza del temporal y acuse fallido/reinicio.

Build completo local verificado del codigo final `e7f22bb4975a9dc658701c238abd7560fc842505`,
tag local `cloud-monitor-stop:e7f22bb`,
ID `sha256:f74748fb48ebe721152b642909eb4af20a24ae99500b7a2a4dbc4d2a60805de8`,
published2026-10-10T23:50:24Z/build `isolated-monitor-e7f22bb`.
No publicado ni desplegado. Smoke del artefacto completo, runtime read-only,
rednone/cap-dropALL, como1037:100:

| Escenario | SIGTERM a salida0 | Reinicio / duplicados |
| --- | --- | --- |
| Reposo | 0,251s | correcto / cero |
| Descarga parcial | 0,365s | correcto / cero |
| SQLite bloqueado por otro proceso | 0,465s | correcto / cero |
| fsync NFS simulado | 0,365s | correcto / cero |

Ademas, cuatro contenedores con ENTRYPOINT exec y **comando productivo
exacto**, monitor comoPID1, UID1037:GID100: todos terminaron0 en~1s desde
SIGTERM hasta comprobacionDocker (incluye arranque del comprobador aislado).
Smoke completo de almacenamiento tambien paso: Playwright, SQLite/WAL,
backups/recovery sinteticos, procesos de alertas y guarda de montaje ausente.
Se limpiaron solo volumen/containers sinteticos identificados; logs conservados.

Suite completaWindows:318passed/9skipsPOSIX-Docker y16avisos datetimeSQLite
preexistentes Kering; lint/compilacion correctos. Las primerasCI de8585aaf/
0b2a6a1 fallaron en los cuatro actores por no heredar PYTHONPATH del runner;
no se ampliaron tiempos ni debilitaron tests. `e7f22bb` fija entorno explicito
en los actores y muestra error de arranque inmediatamente, mantiene al hijo
SQLite fuera de la fixturePID1 y acota tambien la conexion del heartbeat.
La CI del head debe quedar verde antes del rollout; consultar su resultado
publicado en laPR, no interpretar estas pruebas locales como CI aprobada.

**Limite fisico conservado:** NFS `hard` desconectado puede bloquear un
syscall del kernel de manera no interrumpible; lo mismo ocurre con un
fsync/disco realmente atascado. El Event y timeout de Python NO garantizan
salida acotada en ese caso. El ensayo simulado no acredita perdida/reconexion
del NFS compartido, que no se interrumpe. No os._exit, SIGKILL, desmontaje
compartido ni salida0 falsa para sortearlo. Captura permanece bloqueada si
el servicio no termina dentro de presupuesto o sale distinto0; watchdog
independiente del procedimiento restaura Cloud local y conserva el parcial.
Timeout de requests es de connect/read, no un deadline total frente a un
servidor que entregue bytes indefinidamente. Si una operacion excede el
presupuesto, tampoco se certifica parada limpia.

## Actualizacion controlada preparada, NO ejecutada

1. Exigir CI verde del commit exacto y nuevo build Docker completo con
   identidad, smoke1037:100 y pruebasSIGTERM/PID1. Una capa auxiliar o una
   prueba local Windows que omitePOSIX no autoriza actualizar.
2. Mantener imagen anterior porID/tag de rollback, comando/Config/HostConfig,
   mounts, estado running y hashes privados de configuracion. No imprimir
   Env con credenciales ni mezclar el rollout con el cambio de raiz/UID de
   produccion o una migracion de SQLite.
3. Preparar override de **solo** `abcd-luxoptica-monitor` con referencia
   inmutable verificada, mismos env/rutas/volumenes/usuario productivos.
   El binario nuevo permite1037:100, pero cambiar ahora el UID de bases
   activas requeriria aprovisionamiento local independiente y autorizacion.
4. Revisar compose config **privadamente**, sin aplicar. El comando propuesto
   tras aprobacion es `docker compose ... up -d --no-deps --no-build
   abcd-luxoptica-monitor`; nunca compose up global ni recrear otros servicios.
   No usar un tag PR inexistente en registry: buildsPR no publican/despliegan.
5. Coordinar usuarios y comprobar cero trabajos antes de actualizar el
   monitor antiguo: su SIGTERM sigue sin resolver hasta cambiar artefacto.
   Resolver/autorizar su terminacion para rollout por separado; no aceptar137
   como captura consistente. Esta entrega no detiene nuevamente produccion.
6. Tras autorizacion futura, comprobar identidad nueva, running, recibos
   locales y ausencia de duplicados. El monitor no tiene healthcheckHTTP:
   running solo no acredita una consulta Graph correcta. No lanzar jobs
   manuales para comprobarlo; observar su ciclo autorizado y sus recibos.
7. Si falla, restaurar referencia anterior/mismos mounts sin sustituir DBs
   ni borrar imagenes/JSON/versiones. Mantener incidente explicito.

Solo despues de actualizar y validar el servicio real, y con nueva
autorizacion/coordinacion, proponer otra ventana de captura sin snapshot.
No fusionar, desplegar, activar replica remota ni promover candidata ahora.
