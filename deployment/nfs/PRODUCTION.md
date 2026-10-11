# Corte controlado al NAS local

Continuacion autorizada desde b6502b8. No promover la captura antigua:
capturar de nuevo todos los cambios con los cuatro escritores detenidos.
Conservar el ensayo y todos los originales locales para rollback independiente
del NAS. Fotos remoto sigue pendiente y no bloquea este corte.

## Reconciliacion del ensayo, sin repetirlo

El plan congelado contiene3981entradas:3936imagenes elegibles y45archivos
adicionales (manifiestos/estado/documentos). Las3936representaciones conservan
procedencia y nombres por archivo de origen. Las2347entradas ZIP son las
combinaciones unicas EAN/nombre/checksum;1589representaciones repetidas no
duplican entradas ZIP. No falta ninguna imagen elegible: cero entradas imagen
sinEAN o invalida; cada representacion tiene asset verificable y su checksum
se verifico en el ZIP de su EAN. Hay2011assets unicos EAN/checksum: diferentes
nombres/procedencias pueden referirse a los mismos bytes. No equiparar numero
de fotos o versiones de mercado a vistas canonicas.

## Rutas productivas y guardas

Montar la exportacion completa `/mnt/cloud-imagenes` como `/nas`, sin aliases
ni bind de subcarpeta que oculte la identidad de la exportacion. Usar:

- `IMAGE_REPOSITORY_ROOT=/nas/images` (host `/mnt/cloud-imagenes/images`).
- `IMAGE_REPOSITORY_MOUNT_ROOT=/nas`.
- `IMAGE_REPOSITORY_NFS_SOURCE=192.168.1.32:/volume1/cloud-imagenes`.
- `IMAGE_REPOSITORY_BACKUP_ROOT=/nas/backups`, con la misma fuente NFS.
- `IMAGE_REPOSITORY_WORK_ROOT=/nas/work`, fuente y WORK_MOUNT_ROOT `/nas`.
- `IMAGE_REPOSITORY_STATE_ROOT` explicito LOCAL, separado de datos operativos
  locales nuevos (historial, alertas, autenticacion, recibos, claves).

Solo images/backups/work hermanos exactos son destinos productivos. No se
permite staging, raiz padre, aliases o sobre-montajes. Permitir almacenamiento
hermano NO permite replica NAS sobre la misma exportacion: esa guarda sigue
rechazandolo. Backups NAS son retencion, no copia independiente; originales
locales se conservan. Roles nuevos1037:100/0700; archivos0600. Todos los
servicios1037:100 sin grupos/capacidades extra. No ampliar grupo users ni
cambiar propietarios de bases activas. Entrada comprueba montaje/estado local/
backup/temporales antes de arrancar, y publicaciones vuelven a comprobarlos.

## Despliegue y rollback

El workflow solo puede desplegar automaticamente con la variable de repositorio
`CLOUD_AUTO_DEPLOY=true`; ausente/deshabilitada no despliega por fusion. En este
corte controlado mantenerla ausente, no fusionar para obtener un artefacto.
Todo cambio de codigo requiere nueva CI/build/smoke y SHA verificado antes
de parar Cloud. Guardar Compose efectivo, runtime, imagenes y servicios activos
en recibos privados fuera de Git.

Autoarranque: servicios nuevos con restart=no, unidad dedicada dependiente y
ligada a la unidad persistente NFS. No modificar Docker global ni otras apps.
Bind create_host_path=false; no restauracion automatica de escritores fuera
de la supervision/guardas. Preparar y validar unidad con sudo antes del corte.

Parar cuatro servicios y comprobar ausencia de escritores.137 no es aborto
automatico; integridad SQLite/FK y parciales deben revisarse sobre copias. Backup
consistente de todas las DB/WAL y configuracion/clave en disco local privado.
Inventario final firmado contra captura NUEVA, comparar cambios con ensayo,
retener ambiguos/invalidos y abortar si comprometen la integridad. Migrar sin
renombrar originales; verificar todos los metadatos/asociaciones/checksums.

Conservar contenedores/imagen y estado anterior sin escrituras para rollback.
Ante fallo antes de abrir admision, detener nuevos escritores y restaurar
configuracion/imagen/estado anteriores, verificar salud. Si el NAS recibio
nuevas escrituras, congelar y respaldar tanto bytes como catalogo/historial/
estado nuevo LOCAL; reconciliar las diferencias con el estado anterior antes
de reabrirlo. Nunca arrancar el sistema anterior ignorando escrituras nuevas,
copiar SQLite sobre base abierta, eliminar fuentes o activar replica remota.

Publicar resultados reales (SHA/artefacto/rutas/tiempo/funciones/rollback) solo
cuando se hayan comprobado. Este documento no acredita un corte ya ejecutado.
