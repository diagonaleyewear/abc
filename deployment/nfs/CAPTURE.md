# Captura consistente: procedimiento y resultados

Estado actualizado: [PRECOPY.md](PRECOPY.md) distingue la ventana abortada
del2026-10-10UTC y la nueva autorizacion de parada completa, que permite
terminacion forzada documentada si la integridad posterior queda acreditada.
Los comandos y restricciones de autorizacion siguientes son historicos.

Staging existente, SQLite local y guardas: [STAGING.md](STAGING.md).
La nueva ventana cuenta con autorizacion explicita y usuarios avisados;
no concede autorizacion permanente para otras paradas o despliegues.
La replica antigua **no bloquea** el ensayo por decision del responsable.

**Revision posterior b9edc02**: [PRECOPY.md](PRECOPY.md) separa precopia,
cierre coherente e indisponibilidad/verificacion. Su protocolo de dos fases
sustituye la copia integra bajo pausa del documento original siguiente.
La parada corta queda condicionada a snapshot local verificado; sin snapshot
se requiere cierre de contenido bajo pausa, no quick-check por fechas.
La reserva vigente se actualiza a work64GiB/186GiB agregados; los138GiB
de las evidencias/comandos originales siguientes son historicos.
El aviso SQLite de alertas NO era esperado. La carrera de inicializacion
se corrigio con bloqueo compartido y espera acotada, sin debilitar los tests;
CI f4dae43 paso327pruebas, lint, compilacion y Docker con smoke. El artefacto
validado no se ha desplegado sobre los servicios activos.

## Preparar estado local productivo: pasos pendientes, NO ejecutar ahora

1. Autorizar por separado el corte y la identidad1037:100. Crear un directorio
   NUEVO local, por ejemplo `/opt/cloud-image-production/state/<fecha-UTC>`,
   privado0700, propietario1037:100; comprobar filesystem local y reserva2GiB.
   No cambiar owners/modos de DB activas ni reutilizar las copias de ensayo como
   estado productivo: los servicios han seguido escribiendo desde la captura.
2. En esa futura ventana, registrar los cuatro IDs/config/env/mounts/imagen,
   bloquear admision, comprobar recibos y parar escritores. Capturar DB,
   WAL y configuracion/clave con lector read-only en raw local nuevo. Ejecutar
   `scripts/snapshot_sqlite_copy.py --source <raw-local-db> --target <nuevo-db>`
   para cada base enumerada; cero journals sin revision. Integridad/FK,
   SHA256 y recuentos comprobados antes de publicar, sin abrir el original.
3. Conservar raw/sidecars y runtime previo para rollback, claves solo local0600.
   Comprobar travesia, escritura, rename y locks como1037:100 en estado y
   temporales; abrir copias verificadas, nunca corregir permisos de originals.
   El estado del catalogo/repositorio, historial Kering, alertas, autenticacion,
   process_activity y mail/market-state deben estar cubiertos individualmente.
4. Preparar override con rutas locales nuevas e identidad aprobada, comparar
   comando/env/mounts y referencia de artefacto. No habilitarlo hasta validar
   asociaciones, galeria/ZIP y rollback; cambio de repositorio requiere su
   autorizacion propia y resolver las incidencias aplicables al nuevo inventario.
5. Ante fallo, detener solo los servicios del intento y restablecer override,
   imagen y rutas originales registrados; iniciar los IDs/config anteriores
   y comprobar salud. Las DB originales no modificadas por la preparacion
   permiten rollback. Restaurar backups en otro directorio local si es necesario,
   validar integridad/FK/recuentos/configuracion antes de seleccionar esa copia.
   No copiar archivos SQLite sobre una base abierta ni borrar fuentes.

## Replica encontrada: hechos y limites

No se presupone tarea DSM. Se revisaron referencias de scripts en repositorios
locales, /opt/abcd-control, home rubensg, /usr/local/bin y /usr/local/sbin,
cron legible, timers systemd y acciones de tareas Windows visibles.
No se encontro un ejecutor automatizado de rsync a Fotos.

El historial shell del servidor contiene dos comandos manuales hacia
`188.227.143.110::Fotos`: uno dry-run, otro copia. Ambos tienen origen literal
`repo/images/`, sin exclusion literal `staging/`. No se publico el comando
completo ni credenciales. El cwd de esas ejecuciones no queda demostrado:
no convertir ese origen relativo en una ruta absoluta supuesta ni asumir
que esas lineas son una programacion vigente. No habia rsync/rclone activo
en el momento de comprobar. Cron root no legible y archivos OneDrive offline
no fueron inspeccionables; la ausencia de script no prueba inexistencia global.

La receta del repositorio SI tiene exclusion staging; el documento desplegado
en produccion y los comandos historicos no acreditan usarla. **Exclusion de
una replica real automatica NO confirmada**, porque tal ejecutor no se encontro.
No se ejecuto ni dry-run remoto ni copia ni se cambio configuracion.

Tras terminar y validar migracion, preparar NUEVA replica a Fotos, no activarla:
solo imagenes catalogadas del repositorio definitivo, nombres/EAN conservados;
excluir staging/backups/work/temporales/SQLite/metadatos privados; sin borrados
remotos, lock compartido, historial/estado persistidos visibles en Cloud.
Primero prueba de conectividad y simulacion aprobadas. No crear ese job ahora.

## Relacion de incidencias preparada (privada)

Se genera mediante `python -m scripts.report_migration_review`, verificando
firma del plan previo y leyendo manifiestos sin reescanear ni modificar fotos.
Conserva cada candidato EAN/manifiesto, fichero/checksum/tamano, bloqueo y
tratamiento; no inventa mapeos ni cambia nombres. No publicar datos pedidos,
credenciales ni listados identificables en Git.

| Conjunto | Relacion obtenida | Tratamiento |
| --- | --- | --- |
| PRODUCTIVO: plan previo firmado,16 manifiestos leidos en esta entrega | 154 claves modelo/color con mas de un EAN;0 imagenes del plan previo coinciden por nombre con esas claves | Conservar candidatos/procedencia; obtener manifiesto/ficha authoritative. No elegir EAN automaticamente. Revalidar correspondencias sobre fuente congelada, no equiparar154 claves a154fotos sinEAN. |
| PRODUCTIVO previo | 0 imagenes sinEAN y0 no reutilizables;2.187 vistas unknown Luxoptica en ese plan | Son resultados del plan previo, NO un escaneo fresco ni certificacion de todas las fotos actuales. Unknown valido no se descarta; vistas canonicas pendientes. |
| LOCAL previo, separado de produccion | 388 imagenes sinEAN,7 no reutilizables;1 clave ambigua con44imagenes coincidentes por nombre | Cada fichero retenido con checksum; resolver EAN por evidencia proveedor/manifiesto, no por posicion. Las7 invalidas requieren diagnostico offline de formato/decodificacion/dimensiones/limites; reacquirir si autorizado conservando original/version nueva. Apply bloqueado si persisten. |
| Kering real3c807b3 | Tres recursos opacos validos, sin senales acreditadas V1/V2/V3 | Parcial - clasificacion pendiente; no inferir por cantidad/posicion ni reinterpretar nombres generados. El desconocido puede exportarse, no acredita cobertura. |

Informe PRODUCTIVO privado:
`/home/rubensg/cloud-nfs-preparation-20261010/review-20261010/production-prior-review-with-files.json`,
modo0600. El informe LOCAL completo vive en artefactos privados de sesion.
Los ids MAP/EAN/FILE/VIEW son referencias de revision, no nuevos EAN ni nombres.
Current manifests no son snapshot congelado; cambios posteriores exigen informe
nuevo. No usar ni retargetear ese plan v1 como plan aplicable a staging.

## Escritores a coordinar

Comprobados running en el servidor (sin parar):

| Contenedor | Motivo de pausa en la futura ventana |
| --- | --- |
| `abcd-control` | UI/manual, imagenes, catalogo, autenticacion, historial y actividad |
| `abcd-luxoptica-monitor` | Graph/importaciones, JSON de correo/mercados, catalogo, actividad |
| `abcd-control-abcd-kering-scheduler-1` | Pedidos/imagenes/historial/schedule_state/actividad; aunque auto estuviera off no presumir que no escribe |
| `abcd-order-alerts` | SQLite alertas y process_activity compartido |

No Docker global restart, no otras apps del host, no cambios de env/permisos
de activos, no editar config para desactivar programacion. Antes de la ventana:
responsable impide nuevos trabajos manuales, revisa estado LOCAL de jobs y
espera finalizacion. No consultar proveedores/Odoo para esta comprobacion.
Si un trabajo activo no acaba, abortar/reprogramar ventana, no SIGKILL a un
descargador ni afirmar fuente coherente mientras escribe.

Ventana propuesta: **60-90 minutos**, no medicion ni garantia:
fuentes actuales metadata18.662.557.440 B legacy +117.213.812 B Kering +
45.056 B common +388.034 B manifiestos, mas docs/manifiestos/estado local.
18,78GB a10-50MiB/s orientan6-30min de transferencia; doble checksum,
SQLite y verificacion pueden alargar. El rendimiento NFS/copia completa
NO se ha medido. Acordar limite90min para abortar/restaurar si se supera;
validacion de imagenes/inventario costosa (previa1003s) se hace **despues de
restaurar servicios**, sobre la fuente congelada, no suma17min obligatorios
a indisponibilidad. No copiar fuente completa al disco local20GB libre.

## Secuencia concreta de la futura ventana

0. Artefacto nuevo realmente disponible/verificado en servidor, sin credenciales
   productivas en contenedor de copia; herramientas snapshot y preflight probadas.
   Preflight1037 exige montaje exacto, cinco carpetas0700 y>=138GiB agregados;
   estado local>=2GiB. Verificar de nuevo, no depender de cifras anteriores.
   Fuente vacia/nueva, snapshots locales NUEVOS, manifiesto privado fuera de
   originales, todos los servicios esperados identificados por ID/imagen.
   No arrancar una imagen CI no publicada ni inventar su tag.

1. Preparar una consola de recuperacion y un fichero privado de estado previo.
   Los comandos siguientes son **PLANTILLA DE VENTANA, NO se ejecutaron**:

```sh
set -euo pipefail
cd /opt/abcd-control
SERVICES=(abcd-control abcd-luxoptica-monitor abcd-control-abcd-kering-scheduler-1 abcd-order-alerts)
PRIOR_RUNNING=()
for C in "${SERVICES[@]}"; do
  if test "$(docker inspect "$C" --format '{{.State.Running}}')" = true; then
    PRIOR_RUNNING+=("$C")
  fi
done
restore_services() {
  RC=$?
  trap - EXIT INT TERM
  FAILED=0
  for C in "${PRIOR_RUNNING[@]}"; do
    docker start "$C" >/dev/null || FAILED=1
  done
  for C in "${PRIOR_RUNNING[@]}"; do
    test "$(docker inspect "$C" --format '{{.State.Running}}')" = true || FAILED=1
  done
  if test "$FAILED" != 0; then
    echo 'ERROR: restauracion incompleta; usar consola de recuperacion y avisar al responsable' >&2
    exit 1
  fi
  exit "$RC"
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# SOLO tras autorizacion de ventana, ausencia de trabajos activos y admision cerrada:
for C in "${PRIOR_RUNNING[@]}"; do docker stop --time 120 "$C" >/dev/null; done
for C in "${SERVICES[@]}"; do
  test "$(docker inspect "$C" --format '{{.State.Running}}')" = false
done
# Captura/verificacion pasos2-4 aqui. Cualquier error provoca restore_services.
```

`docker stop` termina procesos; no garantiza trabajo activo finalizado.
Registrar ExitCode/estado y exigir salida0; abortar tambien con salida1 de
drenaje fallido, no solo terminacion forzada137. Una pausa
no crea un snapshot atomico entre varias DB: no comenzar copia hasta que
**todos** los escritores esten detenidos. La consola de recuperacion usa
`docker start` SOLO para los cuatro que constaban running; no `compose up`
que recree contenedores ni active servicios antes apagados.

2. Fuentes bind **solo lectura** en contenedor de captura:
   `/opt/abcd-control/repo/images` -> source/Luxoptica,
   `masterdata_data/kering` -> source/Kering,
   `image_repository` -> source/Common,
   `luxoptica_data` -> source/Luxoptica-manifests,
   `docs/Luxoptica` -> source/Luxoptica-docs.
   Copiar ficheros como1037 con nombres/extensiones exactos, sin sobrescribir
   destino previo ni rsync --delete. El origen puede requerir lector root
   existente, pero destino solo1037; no chown/chmod origen para dar acceso.
   Un pipeline reader(tar readonly)->writer(tar1037) con `pipefail`, no
   comandos rsync contra remoto. Excluir `staging/` y trasladar SQLite/WAL a
   flujo local paso3; registrar esas excepciones en manifiesto, no descartarlas.
   Retener ZIPs e imagenes invalidas/ambiguas: no aplicar filtros Fotos.
   Guardar SHA256/tamano/ruta relativa ANTES y comparar origen/copias DESPUES.
   Si algo cambia, falla pipeline o aparece `.part`/journal activo, marcar
   captura incompleta y abortar; nunca llamar congelada a copia parcial.

   Comando de copia local, dentro del bloque protegido por trap del paso1.
   `CAPTURE_IMAGE` es el artefacto verificado disponible; sin variables/secretos
   de produccion. **No ejecutado**:

```sh
: "${CAPTURE_IMAGE:?Artefacto verificado disponible en el servidor}"
copy_tree() {
  ORIGINAL="$1"
  DESTINATION="$2"
  case "$DESTINATION" in Luxoptica|Kering|Common|Luxoptica-manifests|Luxoptica-docs) ;; *) return 1 ;; esac
  docker run --rm --network none --read-only --user 0:0 \
    --mount "type=bind,src=$ORIGINAL,dst=/original,readonly" "$CAPTURE_IMAGE" \
    tar -C /original --exclude='staging' --exclude='*.sqlite3' \
      --exclude='*.sqlite3-wal' --exclude='*.sqlite3-shm' \
      --exclude='*.sqlite3-journal' --exclude='*.lock' -cf - . |
  docker run --rm -i --network none --read-only --user 1037:100 --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount type=bind,src=/mnt/cloud-imagenes,dst=/nas \
    -e DESTINATION="$DESTINATION" "$CAPTURE_IMAGE" sh -c \
    'set -eu; umask 077; python -m scripts.verify_staging_exports --manifest /app/deployment/nfs/staging-layout.tsv --mount /nas; D="/nas/staging/source/$DESTINATION"; test ! -e "$D"; mkdir -m 0700 "$D"; chmod 0700 "$D"; tar --no-same-owner --no-same-permissions --keep-old-files -C "$D" -xf -'
}
copy_tree /opt/abcd-control/repo/images Luxoptica
copy_tree /opt/abcd-control/masterdata_data/kering Kering
copy_tree /opt/abcd-control/image_repository Common
copy_tree /opt/abcd-control/luxoptica_data Luxoptica-manifests
copy_tree /opt/abcd-control/docs/Luxoptica Luxoptica-docs
```

   Reader readonly puede leer como propietario original; writer1037 no obtiene
   capacidades de root. Este pipeline **no hace la verificacion por si mismo**:
   aceptar fuente solo tras comparar manifiestos del paso4. Tar de archivos
   SQLite es excluido exclusivamente para capturarlos locales; otras extensiones
   SQLite detectadas por auditoria deben trasladarse al mismo flujo local antes
   de ejecutar. Si hay symlinks o mounts internos no revisados, bloquear y no
   usar el pipeline. No ejecutarlo sin montaje validado/0700, directorios nuevos,
   revisiones y ausencia comprobada de escritores. No reutiliza una captura
   parcial automaticamente ni extrae datos no confiables recibidos de proveedor.

3. SQLite: enumerar TODAS las DB de raices incluidas y masterdata_data:
   history, catalogo, process_activity, order_alerts, auth_state y otras
   realmente presentes. Para cada una, directorio NUEVO local
   `state/frozen/<origen>/`, permisos1037:100/0700; scratch local, no NAS.
   La utilidad preparada copia DB+WAL sin abrir el original, verifica que no
   cambiaron, hace backup API sobre scratch, integrity_check/foreign_key_check,
   recuentos de tablas y checksum del DB cerrado:

   Enumeracion SSH solo metadata, sin abrir DB activas, en esta entrega:
   `masterdata_data/kering/history.sqlite3`, `masterdata_data/order_alerts.sqlite3`
   (WAL/SHM presentes), `masterdata_data/auth_state.sqlite3`,
   `masterdata_data/process_activity.sqlite3`, `image_repository/.catalog.sqlite3`.
   Todas las DB listadas0:0/0644: no cambiar esos permisos/propietarios.
   La lista se debe repetir al comenzar captura. Lectura como1037 desde bind
   readonly debe acreditarse antes de ventana; abortar si no tiene acceso.
   No ampliar permisos activos para resolverlo.

```sh
# Dentro de contenedor de captura con origen readonly y salida LOCAL nueva:
python -m scripts.snapshot_sqlite_copy \
  --source /original/history.sqlite3 \
  --target /local-frozen/Kering/history.sqlite3
```

   Ejemplo ejecutable para las cinco DB observadas, SOLO dentro de ventana.
   El bind `/original` es el directorio que contiene DB+WAL, no DB aislada.
   Cada salida se crea una vez; reintentar captura requiere nuevo RUN:

```sh
: "${CAPTURE_IMAGE:?Artefacto verificado disponible}"
RUN="capture-$(date -u +%Y%m%dT%H%M%SZ)"
snapshot_db() {
  ORIGINAL="$1"; DB="$2"; ROLE="$3"
  docker run --rm --network none --read-only --user 1037:100 --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=bind,src=$ORIGINAL,dst=/original,readonly" \
    --mount type=bind,src=/opt/cloud-image-staging/state,dst=/local \
    -e RUN="$RUN" -e DB="$DB" -e ROLE="$ROLE" "$CAPTURE_IMAGE" sh -c \
    'set -eu; umask 077; mkdir -p "/local/frozen/$RUN"; D="/local/frozen/$RUN/$ROLE"; test ! -e "$D"; mkdir -m 0700 "$D"; python -m scripts.snapshot_sqlite_copy --source "/original/$DB" --target "$D/$DB"'
}
snapshot_db /opt/abcd-control/masterdata_data/kering history.sqlite3 Kering
snapshot_db /opt/abcd-control/masterdata_data order_alerts.sqlite3 alerts
snapshot_db /opt/abcd-control/masterdata_data auth_state.sqlite3 auth
snapshot_db /opt/abcd-control/masterdata_data process_activity.sqlite3 activity
snapshot_db /opt/abcd-control/image_repository .catalog.sqlite3 Common
```

   Repetir con path exacto de cada DB. No copiar SHM, checkpoint/reparar la
   original, activar modo WAL sobre ella ni usar `cp DB` solo si habia WAL.
   Config cifrada/JSON conservados sin mostrar claves/tokens; secretos fuera
   de Git/logs. Snapshot root-readable puede generarse como lector autorizado
   y transferirse a archivo NUEVO1037:100/0600, nunca cambiar owners activos.
   Si la utility falla o FK/integridad falla, fuente NO aceptada.
   Preparar history local0600 con punto de bind vacio en source/Kering, no
   colocar SQLite operativo NAS. Verificar restauracion SQL/recuentos sobre
   copias locales nuevas, sin reemplazar bases activas.

4. Verificacion antes de reabrir:
   todos los originales antes/despues tienen mismo checksum/lista/tamano;
   toda copia tiene mismo nombre/bytes (DBs distinguen snapshot y original
   DB+WAL en manifiesto). Firmar/manifiesto privado de fuente y DB, comprobar
   que source no cambia, sin writers de ensayo sobre ella. Revisar bases
   locales1037:100/0600. No se crea catalogo de migracion ni apply durante pausa.

5. Salir del bloque de ventana para restaurar servicios con trap. Esperar
   app healthy y comprobar running de monitor/Kering/alertas, mismo ID/imagen,
   configuracion/raices y modo auto previo sin cambios. Si app no healthy en
   plazo2min, mantener incidencia explicita y consola de recuperacion; no
   desplegar/recrear ni modificar pedidos. No confirmar exito por docker start
   solamente. Por fallo, conservar fuente parcial etiquetada/incompleta en
   staging para diagnostico, no borrar originales ni usarla para apply.

6. Con produccion ya restaurada: inventario completo v2, reporte de incidencias,
   preservacion metadata Common/orderlinks y manifiestos, galeria/ZIP/historial;
   solo apply si cero bloqueos y capacidad suficiente. Verificar, repetir,
   recuperar y validar checksums/restauracion local. Es ensayo, no corte.
   La ausencia de replica antigua no lo bloquea; integridad/mapeos SI.

## Comprobaciones sin detener produccion realizadas

- Servicios running y rutas/tamanos/capacidad solo metadata, sin abrir SQLite
  activas ni proveedores/Odoo. App/monitor/Kering conservaron StartedAt de
  2026-10-10T10:27; alertas de09:37 en la consulta. Ningun writer parado.
- Informe completo154ambiguedades y relaciones de archivos LOCAL privado,
  preservando candidatos y originales; no validacion fresca de imagenes.
- Staging permisos/probes reales anteriores conservados. Unidad fallida
  fuente retirada por administrador, verificada; otras unidades inactivas
  conservadas, montaje productivo intacto.
- Herramienta snapshot probada con WAL sintetico comprometido, sin modificar
  DB/WAL originales del test; no snapshot de DB productivas ejecutado.
- Preflight repetido dentro de contenedor1037:100: cinco rutas0700 correctas,
  NAS3.041.583.169.536 B libres vs148.176.371.712 B reserva agregada. El bind
  readonly rechazo correctamente el montaje por no ser writable; el preflight
  sin --create paso con bind rw sin efectuar escrituras. Usuario SSH1000 no
  puede atravesar staging0700: no se ampliaron permisos para inspeccionarlo.
- Docker aislado1037:100 en volumen local nuevo: backup WAL sintetico y
  recuperacion SQL pasaron, originales DB/WAL intactos, snapshot0600.
  Volumen propio retirado. Imagen auxiliar minima no tiene Pillow y no puede
  ejecutar smoke completo; no se instalaron dependencias ni se uso produccion.
  El smoke completo se comprueba en el nuevo build CI con sus dependencias.
- Replica solo investigada como comandos manuales; futura replica pendiente
  sin implementar, activar ni ejecutar copia remota.
- Lectura/travesia acreditadas como1037:100 en bind readonly para las cinco DB
  enumeradas y WAL presente: solo stat/access, sin abrir SQLite ni hacer backup.
  Escritura negada en ese bind, propietarios/permisos activos intactos.
