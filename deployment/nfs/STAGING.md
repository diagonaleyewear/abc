# Staging en la exportacion EXISTENTE, sin nuevas exportaciones

Configuracion vigente; sustituye las propuestas de cinco exportaciones y de
cloud-staging. NAS `192.168.1.32:/volume1/cloud-imagenes`, montaje existente
`/mnt/cloud-imagenes`. No cambiar `IMAGE_REPOSITORY_ROOT` activo de Cloud.

| Uso | NAS | Servidor |
| --- | --- | --- |
| Fuente congelada | `/volume1/cloud-imagenes/staging/source` | `/mnt/cloud-imagenes/staging/source` |
| Imagenes por EAN de ensayo | `/volume1/cloud-imagenes/staging/images` | `/mnt/cloud-imagenes/staging/images` |
| Backups de ensayo | `/volume1/cloud-imagenes/staging/backups` | `/mnt/cloud-imagenes/staging/backups` |
| Recuperacion | `/volume1/cloud-imagenes/staging/recovery` | `/mnt/cloud-imagenes/staging/recovery` |
| ZIP y temporales voluminosos | `/volume1/cloud-imagenes/staging/work` | `/mnt/cloud-imagenes/staging/work` |

`staging` y sus cinco hijos: UID1037/GID100, modo0700, umask077. Nunca permisos
generales users/everyone, mapeo a administrador ni chmod/chown recursivo.
SQLite, historial, planes y estado operativo permanecen **locales**:
`/opt/cloud-image-staging/state`,1037:100/0700. History congelado local
`state/frozen/Kering/history.sqlite3`0600; se monta read-only en el contenedor.
No cambiar permisos/propietarios de las bases activas.

## Capacidad AGREGADA, no backup independiente

`staging-layout.tsv` reserva source24 + images8 + backups64 + recovery24 +
work64 = **184 GiB**, mas **2 GiB libres protegidos** = **186 GiB**
(199.715.979.264 B) medidos UNA vez en el volumen. No sumar cinco `df`.
Work reserva candidata24 + versiones retenidas24 + ZIP/extraccion16 GiB;
16GiB anteriores no cabian para precopiar la fuente completa18,78GB.
Estas reservas conservan margen/retencion de las propuestas previas; recalcular
con nuevo inventario real si crecieron fuentes/archivos comprimidos.
Work mantiene limite8 GiB por ZIP, reserva2 GiB y bloqueo entre importadores.
El uso del mismo pool debe contabilizar originales productivos, staging,
snapshots, retencion, trabajos concurrentes y crecimiento.

Los backups y recovery son copias logicas separadas para el ensayo:
**NO protegen frente al fallo del NAS/volumen**. No sustituyen el backup
independiente requerido para migrar produccion. La replica remota Fotos sigue
documentada, excluye ZIP y no se considera automaticamente backup completo.

## Guardas y aislamiento

Staging requiere explicitamente `IMAGE_REPOSITORY_STAGING_ROOT=/nas/staging`
y `IMAGE_REPOSITORY_STAGING_MOUNT_ROOT=/nas` en el contenedor; la exportacion
esperada permanece exactamente `192.168.1.32:/volume1/cloud-imagenes`.
El bind completo en `/nas` permite comprobar identidad real de exportacion.
No se acepta un bind de subcarpeta como falsa raiz NFS.

Las rutas images/backups/recovery/work solo pueden ser los hijos canonicos
con esos nombres: se rechazan padres, `..`, symlinks, aliases, nested
binds/overmounts que cubran un destino, origen=destino y solapamientos.
Solo esas rutas hermanas pueden compartir exportacion; **no se relaja la
prohibicion de replica sobre la misma exportacion para produccion**.
Las escrituras de la aplicacion fuera de esos destinos NAS, incluidas source
y raiz productiva, se rechazan. Un plan no puede usar `/nas`, `/nas/staging`
ni source como destino. Sin modo staging, un destino que contenga staging
tambien se rechaza antes del inventario/apply/recover.

El bind `/nas` es rw y concede acceso filesystem al proceso1037; las guardas
son de aplicacion, **no una sandbox kernel de datos productivos**. No ejecutar
codigo arbitrario ni jobs de proveedor en el contenedor de ensayo. Source
se conserva congelada por coordinacion de escritores y verificacion de
checksums; el guard prohibe escrituras source mediante APIs de migracion.
History tiene bind local read-only. Sin red/puertos/capacidades/restart.
Montaje hard sin respuesta puede quedar pendiente; mountinfo no demuestra
disponibilidad de red. No desmontar/remontar el NFS compartido para probarlo.

## Preparacion exacta

Herramientas en
`/home/rubensg/cloud-nfs-preparation-20261010/staging-existing-export`.
No instalar nuevas unidades/montajes ni carpetas compartidas.
Usar el mismo artefacto actualmente disponible solo para la utilidad de
creacion (stdlib), no para ejecutar migracion con codigo antiguo:

```sh
cd /home/rubensg/cloud-nfs-preparation-20261010/staging-existing-export
IMAGE="$(docker inspect abcd-control --format '{{.Config.Image}}')"
docker run --rm --network none --read-only --user 1037:100 --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount type=bind,src=/mnt/cloud-imagenes,dst=/nas \
  --mount "type=bind,src=$PWD/runtime-code,dst=/tools,readonly" \
  --mount type=bind,src=/opt/cloud-image-staging/state,dst=/state,readonly \
  -e PYTHONPATH=/tools --workdir /tools "$IMAGE" \
  python -m scripts.verify_staging_exports --manifest /tools/staging-layout.tsv \
    --mount /nas --state /state --create
```

La utilidad valida montaje/capacidad antes de crear; valida todos los caminos
existentes sin cambiar sus permisos. Solo aplica chmod0700 a directorios
que acaba de crear. Si ACL Synology no retiene propietario/mode, aborta y
conserva diagnostico; no amplía permisos ni afirma privacidad sin verificar.
Una ejecucion interrumpida puede dejar directorios propios ya creados:
revisarlos antes de repetir, nunca eliminar datos ni chmod existentes a ciegas.
Ejecutar sin `--create` para verificar sin escribir.

## Retirada ACOTADA de unidades anteriores

Se detecto una sola fallida: `mnt-cloud\x2dmigration\x2dsource.mount`.
Las otras cuatro propuestas estan inactivas/dead, no fallidas: se conservan,
no se usan para esta configuracion. No se encontro unidad cloud-staging.
Sudo requiere autenticacion; no se usa Docker para cambiar systemd del host.

```sh
cd /home/rubensg/cloud-nfs-preparation-20261010/staging-existing-export
sh -n remove-failed-staging-units.sh
sudo sh ./remove-failed-staging-units.sh
```

El script limita nombres a las cinco unidades previas y solo retira la que
este failed, no montada, y coincida byte a byte con la plantilla anterior.
Una unidad modificada/activa se conserva con error explicito. Deshabilita,
elimina SOLO el fichero exacto y reset-failed. No stop/unmount/remount ni
retirada de `mnt-cloud\x2dimagenes.mount`, no elimina directorios/datos.

## Exclusiones obligatorias

La investigacion y el procedimiento concreto de captura estan en
[CAPTURE.md](CAPTURE.md). No se encontro tarea automatica rsync/DSM:
solo comandos manuales historicos. Por decision del responsable no bloquea
el ensayo; la nueva replica remota se preparara despues, sin activarla ahora.

- Inventarios productivos podan cualquier directorio `staging` antes de
  recorrerlo: no contabilizar ni importar source/backups/recovery/work.
- Busquedas, galeria y ZIP del repositorio no devuelven registros con rutas
  relativas bajo staging. Replica integrada tampoco copia sus backups.
- Receta rsync remoto: `--exclude='staging/'`, sin `--delete`. No se encontro
  tarea automatica ni se confirmo exclusion en ejecutor real. La nueva replica
  se preparara tras validar migracion; no se contacto/escribio el NAS remoto.
  La investigacion de comandos antiguos ya no bloquea staging.
- El ensayo SOLO inventaria hijos de source congelada bajo modo staging.
  No inventariar la raiz NAS/productiva ni usarla como destino.

## Ensayo completo sigue condicionado

La opcion de precopia y sus limites estan en [PRECOPY.md](PRECOPY.md).
Protocolo precopia necesita work64GiB, NO los16GiB del ensayo sin precopia;
la reserva agregada pasa a186GiB. No iniciar precopia con plantilla antigua.
Sin snapshot local probado no se promete cierre rapido basado en mtime.

Captura requiere ventana explicita para coordinar todos los escritores o
snapshot coherente aprobado. No detener servicios activos en esta entrega.
Copiar originales sin renombrar/eliminar ni omitir archivos invalidos o EAN
ambiguos. SQLite DB+WAL a scratch LOCAL nuevo en ventana sin escritores;
backup API sobre copia, integrity_check/foreign_key_check y checksum de DB
cerrado. No copiar SHM/checkpoint/chown de originales activos.

Origenes Luxoptica/Kering/Common y manifiestos deben conservar asociaciones,
nombres/procedencias y snapshots/results. Cualquier relacion no importada
por herramienta es bloqueo, no entrada descartable. Inventory v2 nuevo ->
revisar bloqueos/espacio -> apply -> verify -> repetir/reanudar ->
recover en recovery. Comprobar checksums de todos los originales, fuente
intacta, historial/EAN/nombres/extensiones, galeria y ZIP completo. Recuperar
tambien SQLite a estado local nuevo: recover no activa catalogo/historial.

388 EAN pendientes/7 invalidas del inventario LOCAL previo no equivalen a
las154 claves ambiguas excluidas/0 imagenes sin EAN o invalidas/2.187 vistas
desconocidas del inventario PRODUCTIVO previo. Reevaluar, no ocultar bloqueos.
Kering sigue **Parcial - clasificacion pendiente**, sin inventar V1/V2/V3
por posicion, cantidad ni nombres generados. No migration produccion,
originales eliminados, cambio de repositorio activo, fusion ni despliegue.

## Evidencia REAL de esta adaptacion

- Se crearon staging y source/images/backups/recovery/work con1037:100/0700.
  Synology retuvo inicialmente777 para el mkdir de staging: el primer intento
  aborto ANTES de crear hijos. Se verifico propietario1037:100 y carpeta propia
  vacia; se corrigio SOLO ese directorio a0700. La utilidad aplica chmod0700
  exclusivamente a directorios nuevos y valida el resultado.
- Desde contenedor aislado1037:100: cinco carpetas privadas verificadas,
  escritura/lectura/fsync/rename/checksum y flock de dos procesos correctos
  en CADA una. Temporales propios limpiados. UID1038 con mismoGID100 no tiene
  lectura/escritura/travesia de staging ni de sus cinco hijos.
- Capacidad libre medida una vez:3.041.600.602.112 B frente a138 GiB reservados.
  No son cinco cuotas independientes ni proteccion ante fallo del NAS.
- Padre NAS conserva0:0/777 y montajeNFS4.1/rw/hard/SYS; estado local conserva
 1037:100/0700; app healthy. No imagen existente leida/escrita por los probes.
- El responsable retiro por sudo la unica unidad fallida; ausencia de fichero/
  enlace verificada por SSH. Las otras unidades inactivas no se eliminan.
- Un timeout SSH intermedio se resolvio al reintentar; no se interpreta como
  ensayo de perdida/reconexion del montaje NFS. Ese ensayo no se repitio
  sobre el montaje compartido.
- No se obtuvo fuente real ni se ejecuto migracion/recovery completa en esta
  entrega; siguen pendientes ventana coherente, mapeos y backup independiente.

Validacion local:289 tests correctos,2 tests Compose omitidos (sin Docker local),
lint/compilacion correctos.16 avisos SQLite conocidos y1 aviso de hilo de alertas
por `database is locked` en esta ejecucion completa; no es una validacion de
alertas sin incidencias ni motivo para modificar bases activas.
El test de concurrencia de alertas repetido aisladamente paso sin ese aviso;
se conserva el limite observado de la suite completa.
