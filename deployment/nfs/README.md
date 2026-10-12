# Synology NFS: PREPARADO, NO ACTIVADO

**Configuracion vigente para staging:** [STAGING.md](STAGING.md).
Solo subcarpetas privadas de `/mnt/cloud-imagenes/staging` en la exportacion
existente: source/images/backups/recovery/work, SQLite local. No crear nuevas
exportaciones/unidades. Sustituye todas las propuestas anteriores.
Los apartados siguientes conservan la evolucion y la evidencia historica.

Objetivo: imágenes en `192.168.1.32:/volume1/cloud-imagenes`, montadas en el
servidor `192.168.1.55` bajo `/mnt/cloud-imagenes`. Sólo bytes de imágenes y locks
compartidos deben vivir en NFS. SQLite, WAL, historial, estado de correo, recibos,
planes y backups de bases de datos deben permanecer en almacenamiento persistente
local o en copias de seguridad independientes, no en un SQLite activo sobre NFS.

## Comprobaciones reales del 10/10/2026

- SSH como rubensg (UID/GID 1000:1000).
- TCP 111 y 2049 del NAS accesibles. **No acredita exportación ni autorización**.
- Actualizacion posterior: `mount.nfs` instalado y montaje real comprobado.
- Sudo no interactivo requiere contraseña; no se usa Docker para eludirlo.
- `/mnt/cloud-imagenes`: origen exacto `192.168.1.32:/volume1/cloud-imagenes`,
  NFS 4.1, rw, hard, TCP, sec=sys, clientaddr=192.168.1.55.
- App, monitor Luxoptica y scheduler Kering ejecutan actualmente como **0:0**.
  No se han cambiado permisos, usuarios, configuración NFS, servicios ni imágenes.
- Disco local: 5.332.529.152 B libres; NAS imagenes: 3.041.245.659.136 B libres.
- Pruebas **reales** de escritura/lectura/fsync/rename/SHA-256/flock entre dos
  procesos correctas como host 1000:1000 y dentro de la imagen actual como 0:0,
  con red del contenedor deshabilitada. Carpetas temporales unicas eliminadas.
  Ninguna imagen existente leida, modificada o migrada por estas pruebas.
- Directorio de prueba propio 1000:1000 con modo 700: escritura correcta.
  La exportacion existente sigue 0:0 y 777; no se ha cambiado su ACL.
- Una creacion temporal desde el contenedor root devuelve propietario 0:0
  y modo 777. No acredita una politica segura de squash ni la ausencia de
  mapeos administrativos: comprobar configuracion Synology con su administrador.
- No se simulo una interrupcion de red/desmontaje sobre el montaje compartido.
  Las pruebas de guard y de migracion en pytest usan datos sinteticos separados.

## Preparación de Synology (administrador del NAS)

Crear/confirmar la exportación indicada y autorizar exclusivamente el host
192.168.1.55, lectura/escritura, seguridad SYS y NFS 4.1.
Conservar root squash; **no mapear root/todos a admin** ni conceder permisos
administrativos. Acordar UID/GID de servicio no privilegiado y otorgar únicamente
acceso a esta carpeta. La opción propuesta es UID/GID 1000:1000, pero debe
verificarse con ACL/identidad real del NAS: no se presupone su equivalencia.
Si root squash rechaza las escrituras actuales 0:0, es un bloqueo válido, no un
motivo para desactivarlo. Cambiar el UID de los contenedores requiere además
preparar permisos de almacenamiento local y el PATH de Python/Playwright
(la imagen actual instala dependencias bajo `/root/.local`).

## Montaje persistente (no ejecutado)

Con sudo autorizado y sin modificar aún Compose ni el repositorio activo:

La unidad visible actualmente es la unidad dinamica del montaje existente:
`FragmentPath` vacio, `SourcePath=/proc/self/mountinfo`, `is-enabled=not-found`.
No es persistente y no hay entrada fstab. Reutilizar su mismo nombre escapado;
no crear una segunda unidad ni un segundo montaje. Plantilla y unidad generada
estan preparadas en `/home/rubensg/cloud-nfs-preparation-20261010`.

Con autorizacion sudo, instalar la unidad sobre ese mismo nombre y habilitarla
para el siguiente arranque, sin desmontar ni reiniciar el montaje actual:

```sh
UNIT="$(systemd-escape --path --suffix=mount /mnt/cloud-imagenes)"
sudo install -m 0644 "/home/rubensg/cloud-nfs-preparation-20261010/$UNIT" "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT"
systemctl show "$UNIT" -p FragmentPath -p SourcePath -p UnitFileState
findmnt -T /mnt/cloud-imagenes -o TARGET,SOURCE,FSTYPE,OPTIONS
```

No ejecutar chmod/chown sobre `/mnt/cloud-imagenes` mientras esta montado:
afectaria al NAS. El eventual modo 000 de la carpeta local subyacente solo se
prepara en una ventana con el montaje ausente y escritores detenidos.
**No basta mientras los contenedores sean root**.
El montaje usa `hard`, TCP y NFS 4.1; nunca `soft`, `nolock` ni un montaje
automático que permita escribir en el directorio local. Se espera la red al
arrancar. Si Synology sólo permite otra versión/export path, revisar y validar;
no degradar silenciosamente.

`scripts/nfs_repository_guard.py --root /mnt/cloud-imagenes` verifica en
mountinfo la fuente exacta, filesystem NFS, montaje raíz, rw y hard.
No crea directorios. Rechaza montaje ausente, local, export distinto, subdirectorio
o soft. El codigo preparado lo integra en el repositorio, consulta/resolucion,
locks, Graph y migracion; comprueba tambien antes de publicar bytes mediante
rename. **No esta desplegado**. Comprobar mountinfo no elimina la carrera entre
la comprobacion y un desmontaje forzado; el bloqueo de activacion incluye
identidad no root, carpeta subyacente protegida y supervision del montaje.

Después del montaje, ejecutar `scripts/test_nfs_image_storage.py` como UID/GID
real del escritor, primero con identidad actual para comprobar squash y después
con la identidad de servicio acordada. Sólo crea una carpeta temporal única
`.cloud-nfs-probe-*`; prueba bytes, fsync, rename, checksum y flock de dos procesos;
limpia exclusivamente sus archivos. No accede a imágenes existentes.
Si falla o se pierde NFS puede quedar esa carpeta de prueba; identificarla
explícitamente antes de limpiarla. No usar borrado recursivo con comodines.

## Arranque y pérdida de conexión: condición previa al corte

La configuración activa Docker tiene `restart: unless-stopped`; un ExecStartPre
externo por sí solo no impide que Docker restaure esos contenedores al arrancar.
Antes del corte deben prepararse y validarse conjuntamente:

1. El codigo preparado implementa `IMAGE_REPOSITORY_STATE_ROOT` local
   (catalogo/WAL/correo/pendientes/planes/recibos) y
   `IMAGE_REPOSITORY_BACKUP_ROOT` independiente (imagenes y archivos ZIP).
   SQLite/historial/configuracion originales se respaldan bajo estado local.
   `KERING_DATA_ROOT`, `PROCESS_ACTIVITY_PATH` y otras bases mantienen volumen
   local. Mantener el comportamiento anterior cuando no se configura separacion.
   No copiar SQLite activo: copia consistente y verificacion antes del corte.
   Un catalogo/estado legacy sin copia separada provoca un bloqueo, no un
   catalogo nuevo vacio ni repeticion silenciosa de correo.
2. Integrar el guard en todos los escritores (Graph, Kering, galería/revisión,
   migración y réplica), antes de mkdir/open/rename, además del arranque.
   No admitir fallback ni éxito ante fallos de NFS. Locks compartidos en NFS;
   locks locales no coordinan clientes distintos.
3. Usar bind con `create_host_path: false`, identidad no root y sin capacidades,
   y dependencia de la unidad mount sólo para los escritores Cloud. No alterar
   el servicio Docker global: también aloja aplicaciones ajenas.
4. Cambiar la política de autoarranque de escritores a una supervisión gated:
   `restart: "no"` y una unidad dedicada con `Requires`/`After`/`BindsTo` del
   montaje y `ExecStartPre` del guard. La política existente no debe mantenerse
   como vía de bypass. Una unidad mount no prueba que el NAS responde.
5. En desconexión, `hard` mantiene las operaciones pendientes, no escribe en
   almacenamiento local. Guard detecta export/montaje incorrecto, no una caída
   de red mientras el kernel conserva el montaje. Healthcheck/watchdog dedicado
   debe declarar servicio no disponible sin inventar éxito. Probar reconexión,
   checksum y recuperación de locks en ventana aislada; no desmontar a la fuerza
   ni usar lazy unmount con escritores activos.

No basta definir variables: todo lo anterior bloquea la activacion. No se ha
instalado la unidad ni cambiado Compose/.env/UID/permisos del NAS. La plantilla
de variables `storage.env.template` es solo de preparacion; no cargarla en
produccion. Falta crear/verificar la exportacion independiente de backups
e indicar su ruta/origen; no se ha inventado una exportacion.

## Simulación de capacidad (basada en inventario previo, no plan ejecutable)

Fuente: inventario privado `20261010-1148-retry` del servidor. Se verifica firma
antes de derivar el cálculo; hay que repetir inventario con escritores pausados
antes de aplicar porque producción ha seguido funcionando.

| Componente | Servidor local | NAS |
| --- | ---: | ---: |
| Originales actuales, ya ocupados | 18.780.210.777 B | 0 |
| Imágenes únicas por EAN/checksum | 0 adicionales | 5.011.781.184 B |
| Backup de SQLite, JSON y demas metadatos | 771.372 B adicionales | 0 |
| Backup de imagenes originales | 0 | 9.893.383.353 B, exportacion independiente |
| Backup de archivos ZIP/archivos comprimidos | 0 | 8.886.056.052 B, exportacion independiente |
| SQLite activo, WAL, historial, planes/recibos | local; medir crecimiento/reserva | 0 |
| Recuperacion de ensayo | 771.372 B metadatos | 18.779.439.405 B en destino independiente, si se ensaya alli |
| Temporal de copia de migracion | 220.332 B | 9.616.571 B imagenes; 4.804.982.367 B backups |
| ZIP bajo demanda | hasta 256 MiB por sesión concurrente | 0 |

El usuario ha elegido una exportacion NAS **independiente** para backups de
imagenes. Incluye originales y ZIP completos, no solo fotografias deduplicadas.
Su origen, montaje y capacidad estan pendientes de provision administrativa.
La copia de archivos se transmite por bloques; el temporal de un ZIP de backup
vive en esa exportacion, no en el disco local. Conserva todos los bytes
inventariados y nunca elimina originales. No contar una replica como gratis.

Los temporales/SQLite son reservas, no una predicción exacta de su concurrencia.
El informe privado derivado registra máximos concretos y separa bytes/GiB.
Resultado del recalculo: SQLite inventariado **110.592 B**, plan privado previo
**4.522.307 B**. Minimo adicional local (metadatos, su temporal, copia de SQLite,
plan previo y una sesion ZIP): **274.060.059 B (0,26 GiB)**. NAS imagenes +
temporal: **5.021.397.755 B (4,68 GiB)**. Exportacion independiente de backups +
temporal: **23.584.421.772 B (21,96 GiB)**; capacidad alli aun no comprobada.
No incluye crecimiento de catalogo/recibos, historiales activos fuera del
inventario, nuevo plan, concurrencia ni snapshots. No es garantia de capacidad.

Graph mantiene `.incoming` local: un ZIP recibido puede requerir otros
**4.804.982.367 B** antes de extraerlo. Sumado al minimo local anterior consume
**5.079.042.426 B**, frente a **5.332.529.152 B** disponibles: solo quedan
253.486.726 B de margen, insuficiente para afirmar operacion segura. Reservar
mas espacio o limitar y medir concurrencia antes de activar; el traslado de
backups no resuelve automaticamente el cache de descargas.
No hubo cambios de tamaño/mtime ni fuentes ausentes respecto al inventario
previo, pero esa comprobación no sustituye nuevos checksums con escrituras pausadas.
Los nuevos inventarios generan planes v2 con raices firmadas de estado/backups;
`apply`, `verify` y `recover` usan esa separacion. Un plan v1 conserva su
comportamiento legacy; si pretende cambiar raices, se rechaza y se exige nuevo
inventario. Esta simulacion derivada del plan anterior NO es un plan ejecutable.

## Réplica y montaje sobre sí mismo

No ejecutar `sync-nas` contra esta misma exportación como origen y destino,
ni mediante otro bind o alias. El control preparado compara rutas, samefile
y mountinfo y rechaza la misma exportacion NFS antes de crear/copiar.
`sync-nas` legacy se bloquea con almacenamiento separado: requiere un mecanismo
de snapshot/recuperacion independiente, no una copia sobre si mismo.
Backup consistente SQLite local mediante API backup;
no copiar WAL activo ni exportar el catálogo activo desde NFS.

Estado final: **preparacion unicamente**, montaje manual real comprobado,
persistencia no instalada, repositorio activo sin cambios, no migracion,
sin permisos ampliados. Solo se escribieron temporales aislados de prueba.

Suite offline completa: **269 correctas**, lint/compilacion correctos; 16 avisos
preexistentes de adaptador datetime SQLite. Pruebas offline del guard:
**9 correctas**, mas regresiones de separacion,
historial, backups ZIP, recuperación y compatibilidad. Cubren montaje
esperado, exportación incorrecta, filesystem local, soft, ro, subdirectorio,
desmontaje/overmount y carpeta ausente sin crear fallback. No son pruebas de
NFS real ni de ACL del Synology.

Smoke adicional del codigo preparado dentro de la imagen Linux actual:
imports, catalogo local, backup de imagen/ZIP separado, repeticion idempotente,
recuperacion y rechazo de montaje ausente correctos. Datos sinteticos en `/tmp`,
red deshabilitada y raiz de contenedor solo lectura; no monta datos productivos.
No sustituye a un build Docker nuevo/CI ni valida reinicio o perdida de red.
App sigue healthy y conserva el bind activo
`/opt/abcd-control/image_repository`. No se publico ni desplego esta rama.

## Continuacion de 9fbf069: comandos y bloqueos administrativos

Identidad actual confirmada del NAS para servicios de imagenes: **1037:100**,
usuario `cloud-images` en el artefacto Docker. No existe colision con una cuenta
administrativa del contenedor; GID100 es `users`, no un permiso compartido.
El build coloca Python en `/opt/venv`
y Chromium en `/opt/playwright`, legibles sin acceder a `/root/.local`.
La identidad por defecto de produccion no cambia; el override de staging fija
`user: 1037:100`, sin capacidades y sin red. El override de preparacion
`image-services.identity.compose.yml` fija la misma identidad para app,
monitor Luxoptica y scheduler Kering, **sin aplicarse a contenedores activos**.
El entrypoint establece umask077 y rechaza grupos suplementarios para UID1037.

Scripts preparados en `/home/rubensg/cloud-nfs-preparation-20261010`:

```sh
cd /home/rubensg/cloud-nfs-preparation-20261010
sh -n install-persistent-mount.sh
sudo sh ./install-persistent-mount.sh
# Solo despues de revisar ACL DSM y repetir los tests efectivos:
sudo env CLOUD_NAS_ACL_VERIFIED=1037-owner-only sh ./set-image-permissions.sh
sudo sh ./prepare-local-state.sh
```

El primero comprueba exportacion/unidad/fstab, verifica la plantilla, instala
la unidad con el mismo nombre dinamico y ejecuta solo daemon-reload/enable.
**No stop/restart/remount/--now**. Si existe ya una definicion persistente,
rechaza reemplazarla. El segundo exige exportacion vacia y actua solo sobre el
directorio raiz: `chown 1037:100` y `chmod 0700`, nunca `-R`; verifica que
Synology retiene exactamente esos valores. No cambia squash ni asigna admin.
No conceder rwx al grupo `users`, ni modo2770, ni ACL heredada `users`/everyone
con lectura/escritura. En DSM revisar ACL de la carpeta compartida y travesia:
permitir al usuario `cloud` lectura/escritura/listar/crear/renombrar/eliminar
sus archivos; conservar ACL administrativas preexistentes del NAS sin asignar
cloud al grupo administrators. No aplicar una denegacion de users que anule
el permiso individual cloud. Las herramientas NFS no equivalen a synoacltool.
No ejecutar simultaneamente con otra provision/escritura en esa exportacion.

La exportacion se comprobo vacia y sigue 0:0, 777. Sudo no interactivo sigue
requiriendo autenticacion: **los scripts no se han ejecutado**. No se utiliza
Docker para modificar unidades, permisos del host o eludir sudo.

### Propuesta concreta de backups y recuperacion

Provisionar en Synology, con autorizacion limitada a 192.168.1.55 y SYS:

Las propuestas anteriores de destinos quedan sustituidas para el ensayo por
[STAGING.md](STAGING.md). Las copias staging comparten NAS y NO constituyen
backup independiente para produccion; los requisitos de recuperacion
productiva siguen pendientes.

No sustituir estas raices por subdirectorios del repositorio activo. Verificacion
real `showmount -e` solo publica cloud-imagenes para .55, mas tres exportaciones
de otros clientes; **no se ha probado capacidad ni permisos de las propuestas**.
El volumen montado de imagenes dispone de 3.048.893.317.120 B en la nueva
consulta; puede compartir pool con las propuestas, pero no prueba cuota ni
reserva de esas carpetas. No escribir en exportaciones ajenas.

Reserva propuesta: al menos **64 GiB** para backups + un temporal de copia
(21,96 GiB), ZIP retenidos y 8 GiB de descarga activa + 2 GiB libres protegidos;
al menos **24 GiB** independientes para recuperacion completa y **8 GiB** para
imagenes staging. Medir cuotas/libres con df y statvfs despues de provisionar,
e incluir snapshots/retencion. Backups y temporales comparten capacidad y deben
contabilizarse juntos, no sumarse como espacio libre independiente.

La copia remota `188.227.143.110::Fotos` (`/volume1/Fotos`) mediante rsync se
conserva en la documentacion y no se modifica. TCP 873 y listado rsync desde
.55 exceden el timeout en esta comprobacion: acceso remoto bloqueado.
No se intentaron credenciales ni se escribio en ese NAS. Esa replica excluye
ZIP y no acredita plan, backups de todos los originales, checksums de recibos
ni recuperacion: no equivale a la exportacion de backup de migracion.

### Graph: reserva y concurrencia resueltas en codigo, activacion bloqueada

`IMAGE_REPOSITORY_WORK_ROOT=/app/image-backups/.work` traslada ZIP/adjuntos
voluminosos a la exportacion independiente. SQLite y los JSON de estado siguen
locales. La variable de fuente y la raiz exacta de montaje son obligatorias
en NFS; montaje ausente bloquea antes de crear `.work`.

Los importadores adquieren un lock compartido `.incoming.lock` durante consulta,
descarga y extraccion: solo un importador Graph activo entre servicios usando
esta configuracion. El limite por archivo es 8 GiB; antes de contactar Graph
se requieren 8 GiB + **2 GiB libres protegidos**. Cada bloque comprueba cuota y
capacidad de nuevo; un fallo genera error, limpia su `.part` cuando el montaje
esta verificado y no reemplaza ni elimina archivos anteriores.
Los adjuntos HTTP y ZIP por enlace se transmiten por bloques de 1 MiB; no se
carga el ZIP completo en memoria. `contentBytes` inline ya viene en la respuesta
Graph; se limita antes de decodificar, pero ese canal no puede convertirse en
streaming despues de recibir el JSON. No afirmar memoria acotada a 1 MiB alli.

La extraccion no materializa todo el ZIP en disco local: incorpora una imagen
cada vez al repositorio, con limite **30 MiB por entrada y 50.000 entradas**.
Se conservan nombres/colisiones y ZIP originales; si no se puede resolver EAN,
la descarga queda conservada y se devuelve error. Los ZIP retenidos consumen
espacio real; no hay limpieza automatica destructiva. La reserva se vuelve a
comprobar en la siguiente descarga. Validar retencion/cuotas en NAS antes del corte.

Asi, los 4,80 GB del ZIP grande previo dejan de sumarse a los 274 MB de minimo
local de migracion. Reservar al menos **2 GiB locales** para catalogo/historial,
WAL, planes y exportaciones ZIP bajo demanda (256 MiB por sesion); limitar y
medir sesiones concurrentes en la ventana de staging. No se considera seguro
el escenario anterior de solo 253 MB de margen. Si `.work` no esta provisionado,
el corte permanece bloqueado; no activar un fallback local.

### Ensayo staging, sin produccion

Los comandos `Other=/staging/source` siguientes son un ejemplo minimo, no
acreditan importacion del historial Kering. Para el ensayo completo usar
origenes separados `Luxoptica=/staging/source/Luxoptica` y
`Kering=/staging/source/Kering`, con `history.sqlite3` consistente en la raiz
Kering y mapeos revisados. No sustituir ambos proveedores por `Other`.

`staging.compose.yml` no publica puertos, no monta pedidos ni credenciales,
no tiene red, no reinicia automaticamente y rechaza binds ausentes. Antes de
usarlo, provisionar las tres exportaciones y el estado local
`/opt/cloud-image-staging/state` con 1037:100, permisos 0700. Montar solamente
copias anonimizadas en `/staging/source` (solo lectura), un directorio privado
de planes local y el destino independiente `/staging/recovery`.

Con un artefacto cuyo smoke/identidad de build hayan pasado:

```sh
python migrate_image_repository.py inventory --source Other=/staging/source --target /staging/images --output /staging/state/plan.json
python migrate_image_repository.py apply --plan /staging/state/plan.json
python migrate_image_repository.py verify --plan /staging/state/plan.json
python migrate_image_repository.py apply --plan /staging/state/plan.json
python migrate_image_repository.py recover --plan /staging/state/plan.json --target /staging/recovery
```

Estos comandos son **dentro del contenedor staging**, nunca sobre origenes
productivos. Verificar sumas y nombres antes/despues, historiales/asociaciones,
segunda ejecucion idempotente, galeria/ZIP y restauracion. Los datos reales de
Kering del commit 3c807b3 permanecen documentados; vistas desconocidas siguen
pendientes de evidencia, no se declaran acreditadas por descargar mas fotos.

### Evidencia real aislada de arranque, desconexion y permisos

`validate-isolated-nfs.sh` monta la misma exportacion solo en el namespace
de un contenedor nuevo, con red bridge propia y `nosharecache`. No usa red/PID
del host, volumen productivo ni socket Docker dentro del contenedor. Las
capacidades SYS_ADMIN/NET_ADMIN y perfiles relajados pertenecen exclusivamente
a esta herramienta de prueba, no al artefacto de app ni a staging.compose.

Resultados reales:

- Montaje ausente rechazado por el guard dentro del contenedor antes de montar,
  despues de desmontar su montaje aislado y despues de reiniciar el contenedor.
- Con su interfaz propia bajada, la escritura hard-NFS permanece pendiente.
- Tras restaurar interfaz **y ruta por defecto**, termina fsync y el checksum
  coincide. Se corrigio el primer ensayo, que subia la interfaz pero perdia
  la ruta; no era un fallo de recuperacion del montaje compartido.
- Dos escritores con flock se serializan despues de reconectar.
- Solo sus archivos temporales eliminados; exportacion compartida sigue vacia,
  sin stop/unmount/restart del montaje host y app healthy.

**Permisos minimos NO acreditados:** en una carpeta temporal propia 10001:10001
y modo 2770, escritura como UID10001 falla con PermissionError; UID10002 tambien
es rechazado. Propietario/mode POSIX visibles no garantizan ACL/mapeo Synology.
El test de transporte se completo con root, claramente separado del test de
identidad no privilegiada. No mapear UID/root a admin para hacerlo pasar:
administrador NAS debe revisar identidad SYS, ACL de la carpeta compartida,
travesia y permisos efectivos. Repetir el test UID10001 y rechazo UID10002
antes de sustituir 777/activar servicios. No se cambio el 777 de la raiz.

Esto valida reinicio de contenedor sin montaje, no un reinicio del servidor
ni el futuro servicio systemd de escritores. La dependencia BindsTo/Requires
y proteccion de carpeta local subyacente siguen pendientes de instalar y
validar. En una caida de red con hard, mountinfo no detecta falta de respuesta:
no se inventa exito ni se escribe en local; se requiere supervision externa
de disponibilidad antes de activar automatizaciones.

### Actualizacion: identidad NAS confirmada 1037:100

El fallo historico UID10001 anterior no se borra de la evidencia. Tras confirmar
el usuario NAS `cloud` UID1037/GID100, se repitio el montaje aislado:

- UID1037/GID100: lectura, escritura, fsync, rename y checksum correctos.
- Root export rw/travesia efectivos para UID1037. UID1038 con **el mismo
  GID100** no obtiene lectura, escritura ni travesia en la raiz.
- Carpeta temporal propia 1037:100, modo0700: cloud escribe; UID1038/GID100
  recibe PermissionError. No se concede acceso al grupo users.
- Corte/reconexion del namespace aislado, escritura como1037 pendiente/resumida,
  checksum correcto y flock de dos procesos como1037 serializados al reconectar.
- Guardas en contenedor siguen rechazando montaje ausente antes/despues de
  montar y despues del reinicio. Temporales eliminados; raiz compartida aun
  0:0/777 y exportacion vacia; app healthy.

`nfs4_getfacl` devuelve salida vacia: **no se han podido enumerar ACL DSM**.
Los tests acreditan acceso efectivo para los dos usuarios probados, no ausencia
de permisos para todas las identidades del NAS. Antes de ejecutar el ajuste
owner-only, confirmar en DSM que no existen grants generales users/everyone
ni pertenencia a administrators para cloud. No se modificaron esas ACL.

Auditoria productiva solo lectura, sin abrir contenidos ni bases:
`masterdata_data`, su subdirectorio kering y la raiz local de imagenes son0:0/755;
history.sqlite3, process_activity.sqlite3, order_alerts.sqlite3 y catalogo
son0:0/644. En binds de auditoria solo lectura no son escribibles por1037;
sus modos POSIX tampoco conceden escritura a1037. No autoriza cambiar la
identidad activa sin preparar datos/ACL locales. No copiar WAL/SHM activo.

`prepare-local-state.sh` crea solo estado **nuevo de staging**1037:100/0700;
rechaza estado existente y NAS. No hace chown recursivo de masterdata_data,
ni altera bases compartidas con alertas. Para el futuro corte se requiere
copia consistente SQLite/local con permisos por usuario y plan revisado para
historial, configuracion y process_activity compartidos; permanece bloqueado.
Los temporales voluminosos siguen en `.work` de exportacion independiente,
no provisionada: su capacidad/ACL reales no quedan acreditadas por cloud-imagenes.

El smoke CI actualizado usa1037:100 sin grupos adicionales, volumen Docker
local persistente **nuevo y desechable** para catalogo/SQLite WAL/SHM y streaming,
modo owner-only, tmpfs solo para navegador/tmp; comprueba tambien que1038:100
no accede al estado. No monta ni modifica almacenamiento de produccion.

Validacion de este ajuste: **281 pruebas correctas en CI**, lint/compilacion
correctos; local279 correctas y2 testsCompose omitidos porque no hay Docker
local, mismos16 avisos SQLite. Compose nativo tambien validado en el servidor,
sin iniciar servicios ni imprimir su configuracion.

[CI38055117566](https://github.com/sginfoocio/abc/actions/runs/38055117566)
correcto: nuevo build y smoke1037:100 con navegador, volumen local, catalogo,
SQLite/WAL/SHM, streaming y recuperacion sinteticos. UID1038/GID100 no obtiene
lectura/escritura/travesia del estado local. Build
`actions-38055117566-1`, version `sha-56c35b619b4e` del merge de prueba real;
identidad/labels verificadas. No publicar imagen ni desplegar en evento PR.
El primer CI detecto dependencia YAML no declarada de los tests; se corrigio
usando DockerCompose nativo, sin agregar una dependencia.

No aplicar los overrides ni el script de permisos hasta confirmar las ACL DSM
y preparar estado local. El build/smoke positivo no cambia esos bloqueos.

## Continuacion de 22db349: ensayo condicionado a provision

### Comprobacion real del 2026-10-10, sin cambios

El responsable confirma en esta conversacion las ACL DSM para cloud1037:100,
sin grants generales users/everyone. Es **confirmacion administrativa**, no
una nueva enumeracion NFS de ACL ni un test de los destinos pendientes.
No se aplica el ajuste de permisos del repositorio compartido.

SSH al servidor confirma:

- `showmount -e 192.168.1.32` publica solo cloud-imagenes para .55; las otras
  tres exportaciones listadas son de otros clientes y no se utilizan.
- No existen `/mnt/cloud-imagenes-staging`, `/mnt/cloud-migration-backups`,
  `/mnt/cloud-migration-recovery`, `/opt/cloud-image-staging/state` ni
  `/opt/cloud-image-staging/source`.
- El montaje compartido conserva el origen esperado, NFS4.1/rw/hard/SYS.
  Su unidad sigue dinamica: FragmentPath vacio, SourcePath=/proc/self/mountinfo.
  Sudo no interactivo requiere autenticacion. App productiva healthy.
- Disco local: **20.285.116.416 B disponibles (80% usado)**. Es una nueva
  medida de capacidad, no un inventario de imagenes. Una copia completa de
  los 18.780.210.777 B del inventario previo dejaria 1.504.905.639 B, menos
  que la reserva local de 2 GiB, incluso antes de estado/WAL/ZIP.
  No preparar esa copia completa en el disco local ni asumir tamano vigente.

**Ensayo completo NO ejecutado**: faltan destinos y conjunto fuente congelado
de staging. Los tests NFS reales1037 y el smoke sintetico previos se conservan;
no equivalen a migrar/restaurar el conjunto real. CI final de 22db349:
[38055469918](https://github.com/sginfoocio/abc/actions/runs/38055469918),
281 tests, lint/compilacion y build/smoke1037 correctos; artefacto
`sha-4325a6a7a878`, build `actions-38055469918-1` del merge de prueba.

### Provision que debe completar el administrador

1. Crear las tres carpetas compartidas/exportaciones de la tabla anterior,
   acceso NFS SYS limitado a .55, cloud1037 con rw/travesia, sin grants de
   grupo users/everyone ni mapeo a administrador. Reservas: backups/work
   >=64 GiB, imagenes staging >=8 GiB, recuperacion >=24 GiB; verificar
   tambien espacio agregado del pool y cuotas, no multiplicar el libre
   del mismo pool por cada montaje.
2. Proporcionar un conjunto fuente independiente y congelado. Para una copia
   completa, proponer una **cuarta exportacion dedicada**
   `192.168.1.32:/volume1/cloud-migration-source` >=24 GiB, montada en
   `/mnt/cloud-migration-source`, con copias anonimizadas Luxoptica/Kering.
   Es propuesta adicional, NO creada ni autorizada automaticamente.
   Mantenerla separada de recuperacion, backups y destino de imagenes.
   Adaptar el bind de `/staging/source` a ese montaje solo tras verificarlo;
   no crear un enlace o fallback local para esconder la falta de capacidad.
   Un conjunto representativo pequeno requiere indicar expresamente su
   alcance; no llamarlo ensayo de todo el inventario productivo.
3. Instalar unidades persistentes para los nuevos montajes y medir
   `findmnt -M`, `df -B1` y permisos efectivos1037/control1038 en cada uno.
   Para el montaje compartido existente usar exclusivamente
   `sudo sh ./install-persistent-mount.sh`: no reiniciarlo ni desmontarlo.
4. Preparar solo estado local nuevo con
   `sudo sh ./prepare-local-state.sh`; comprobar1037:100/0700 y filesystem
   local. No cambiar propietarios/modos de las bases activas.
5. Repetir probes aislados de escritura/fsync/lectura/rename/checksum/flock
   en cada destino provisionado; repetir dentro del artefacto verificado.
   Provision y montajes por si solos no acreditan permisos ni recuperacion.

### Criterios de aceptacion del ensayo pendiente

- Inventario v2 nuevo con raices firmadas, manifiestos/mapeos revisados y cero
  bloqueos; conservar plan privado y publicar solo cifras/alias anonimizados.
  Nunca retargetear el plan productivo v1 ni inventar EAN para desbloquearlo.
- Aplicar, verificar, repetir y recuperar a raiz independiente. Comparar
  checksums/nombres/extensiones de TODOS los originales recuperados y comprobar
  que no han cambiado las fuentes. Repetir tras interrupcion controlada solo
  del trabajo staging y demostrar reanudacion sin sobrescribir.
- Comparar asociaciones por EAN, snapshots/results del historial restaurado,
  representaciones/procedencias/mercados, galeria y contenido completo del ZIP,
  incluidos manifiesto, alternativas y nombres originales. La deduplicacion
  fisica no elimina los nombres de representaciones para exportacion.
- `recover` restaura originales bajo su prefijo de origen; **no activa ni
  reconstruye por si solo el catalogo/historial operativo**. Restaurar los
  SQLite a estado local nuevo y abrirlos con sus lectores; comprobar historial,
  galeria/ZIP y relaciones por EAN de nuevo, no solo contar archivos recuperados.
- Mantener Kering **Parcial - clasificacion pendiente** cuando las imagenes
  validas carezcan de senales acreditadas; no inferir V1/V2/V3 por cantidad.

### Mapeos y archivos no reutilizables: evidencia separada

El inventario local anterior tenia388 EAN sin resolver y7 imagenes invalidas.
El inventario productivo previo tenia0 imagenes sin EAN/invalidas, pero excluyo
154 claves modelo/color ambiguas de manifiestos y registro2.187 vistas
desconocidas. Son conjuntos/categorias distintos:154 claves no son154 archivos
sin EAN, ni unknown equivale a imagen invalida. Ninguna de esas cifras es un
resultado nuevo de staging. Reevaluar con el conjunto congelado, mantener los
originales no reutilizables, y bloquear apply si persisten mapeos/validacion
pendientes. La deteccion automatica de vistas reales Kering sigue sin evidencia.

### Preparacion futura del estado local productivo y recuperacion

Procedimiento **no ejecutado**, requiere ventana/corte autorizados por separado:

1. Inventariar rutas persistentes y escritores. Como minimo:
   `masterdata_data/kering/history.sqlite3`, `kering/config.enc`,
   `process_activity.sqlite3`, `order_alerts.sqlite3`, `auth_state.sqlite3`
   si existe, catalogo actual y JSON de correo/mercados/watchlist/diccionario.
   Detectar tambien otros SQLite/JSON usados en el despliegue, antes del corte.
   Nunca publicar config.enc, claves de cifrado, cookies, tokens o datos Odoo.
2. Para una copia global coherente, detener los escritores **solo en la futura
   ventana autorizada**. Crear backup de cada SQLite mediante
   `sqlite3.Connection.backup` con la identidad que ya tiene acceso al origen,
   a destino local privado NUEVO. No copiar un DB activo sin su WAL, no copiar
   SHM ni ejecutar checkpoints/reparaciones sobre bases activas en este ensayo.
   La API backup captura WAL comprometido; una transaccion entre varias bases
   no es atomica, por eso se requiere ventana sin escritores para el conjunto.
3. Validar en las COPIAS `PRAGMA integrity_check` y `foreign_key_check`,
   tablas/recuentos e historial por pedido/EAN; guardar checksum del SQLite
   cerrado y de JSON/config cifrada en manifiesto privado. Conservar backup
   local inmutable y copia independiente aprobada; nunca SQLite operativo NFS.
4. Provisionar un NUEVO estado local productivo, por ejemplo
   `/opt/cloud-image-production/state`,1037:100/0700, sin `chown -R` de
   masterdata_data. Instalar COPIAS revisadas en ese destino; configuracion
   cifrada y clave con acceso minimo separado, sin descifrar en logs/Git.
   Revisar rutas absolutas de pendientes y los binds/env de todos los escritores.
   Alertas conserva su identidad: resolver su acceso al nuevo
   process_activity compartido con ACL nominativa de directorio/archivos y
   herencia WAL/SHM, probado en staging; no abrir el grupo users ni asumir que
   permiso del fichero SQLite basta. Sin esa prueba, el corte sigue bloqueado.
5. Ensayar recuperacion del backup local en un TERCER estado nuevo1037:100,
   sin reemplazar bases activas. Comprobar integridad, lectura del historial,
   snapshots/results, asociaciones, catalogo y checksums de imagenes/ZIP.
   Validar UID/control, WAL/SHM y reinicio con los mismos binds futuros.
6. Antes de activar, revisar plan de cambio de binds/identidad/raices,
   guardas NFS, supervision y reservas. Rollback futuro: parar nuevos escritores,
   volver a binds/config/artefacto anteriores y originales retenidos; no
   sobreescribirlos con una copia vieja. Estos pasos NO autorizan activar,
   desplegar, migrar produccion ni eliminar originales ahora.
