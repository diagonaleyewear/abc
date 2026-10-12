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
cuando se hayan comprobado. La seccion siguiente registra el corte ejecutado;
los pasos anteriores conservan el procedimiento y no autorizan otra parada.

## Resultado real del corte autorizado

Codigo desplegado `057ca071a83e08faf9f636efd9155812fa5448d6`;
CI38161618827 SUCCESS,331pruebas en Linux (322locales/9skips de plataforma),
lint/compilacion/build/smoke. Imagen completa local `cloud-production:057ca07`,
ID `sha256:c066595bde01fc76e7206a1cb6dbaa7c42b8b9830d1f44356179e3e2a9c67ca6`,
build `controlled-production-20261011-057ca07`, published2026-10-11T17:55:19Z.
El build servidor se comprobo con smoke1037:100 y CI valido el mismo codigo.
No se sustituyo por la imagen auxiliar de captura.

Montaje persistente instalado/habilitado por sudo, misma unidad sin desmontar.
Unidad Cloud dedicada instalada/habilitada, active(exited); los cuatro servicios
con restart=no, usuario1037:100, cap_dropALL, no-new-privileges, mounts explicitos.
No se modifico Docker global ni las dos aplicaciones ajenas.

### Captura FINAL y migracion, no promocion de staging antiguo

- Parada2026-10-11T18:05:11Z. Salidas app0/monitor137/Kering0/alertas0;
  cero escritores Cloud y cero procesos/avisos pendientes.137 se registro
  sin aborto automatico. Cero archivos parciales/journals detectados.
- Source NUEVA `/mnt/cloud-imagenes/staging/source/capture-final-20261011T180000Z`,
  todos3981archivos/18.780.099.617B, cinco rsync checksumexit0/dry-run vacio,
  SHA256/conjunto completo verificados contra originales aun parados.
  La candidata/source del ensayo anterior siguen intactas.
- Estado final en `/opt/cloud-image-production/capture-final-20261011T180000Z`:
  13archivos privados raw y6snapshotsSQLite (5bases distintas), integridad/FK/
  configuracion cifrada/clave comprobados. SQLite no se copia al NAS.
- Comparacion final contra ensayo: cero archivos imagen/documento agregados,
  modificados o ausentes. NO se reutilizaron DB antiguas: el historial/alertas/
  autenticacion/recibos se respaldaron otra vez y prepararon como1037:100.
- Catalogo anterior comun seguia vacio, comprobado explicitamente; si hubiera
  cambiado se habria bloqueado para no omitir sus metadatos.
- Plan productivo firmado
  `5b717a4257f4709252137388b0829d9b4c49617778b8aa16e8601b18a5ca3b06`:
 3981entradas, cero bloqueos nuevos. Migracion verificada de3981entradas,
 3936representaciones/2011assets/3936events/63order_eans.
  Galeria3936 y ZIP2347combinaciones EAN/nombre/checksum comprobados completos.
  Escritura aislada bajo un EAN existente, fsync/lectura/rename correcta,
  limpieza exclusivamente del archivo de prueba; checksums catalogados intactos.
  Staging excluido de galeria y exportaciones y escritura productiva rechazada.

### Activacion y duracion

Listo para arranque supervisado2026-10-11T19:59:15Z, tras1h54min4s de captura,
inventario/migracion/verificacion productiva. No se prometio pausa24min: aquella
era solo captura, sin la migracion al destino nuevo.
El responsable ejecuto `sudo systemctl start cloud-image-services.service`
el2026-10-12, servicios started09:49:08-09Z y salud confirmada09:49:18Z.
**Indisponibilidad total15h44min7s**, incluida espera de activacion sudo de
13h50min3s. Registrar tiempo total, no presentarlo como solo tiempo tecnico.

Los cuatro usan el ID de imagen exacto arriba y mounts/identidad comprobados.
App/alertas healthy; monitor/Kering no tienen healthcheck y running no acredita
por si solo una operacion de proveedor. Endpoint Streamlitok; prueba Playwright
autenticada de dashboard y navegacion a Buscar imagenes sin excepciones, sin
pulsar botones de proveedores ni modificar pedidos Odoo.
Postactivacion: snapshots consistentes LOCALES de las cinco DB, integridad/FK
correctos, recuentos historicos no disminuidos y configuracion descifrable.
NAS libre2.851.421.028.352B y local14.642.212.864B tras activacion, por encima
de las reservas; SQLite sigue exclusivamente local.
Como el monitor reanudo trabajo hubo NUEVAS representaciones/metadatos:
4303representaciones a09:51-52Z (367mas que captura), mismas2347combinaciones
exportables. Recibos/mail-state ya cambiaron: NO arrancar rollback anterior
ignorando estas escrituras. Dos correos completados y cero acuses pendientes
en esa comprobacion; no se imprimieron contenidos/credenciales.
Registros privados de los cuatro servicios desde arranque: cero tracebacks
y cero `database is locked`; monitor con trabajo real en curso y recibos
durables. Snapshot/control de09:51 no reemplaza una nueva captura ante rollback
posterior. El conteo de representaciones puede crecer sin nuevos bytes elegibles.

### Rutas efectivas y rollback

Host NAS: `/mnt/cloud-imagenes/{images,backups,work}`, privados1037:100/0700;
montaje contenedores `/nas`, raiz de imagenes `/nas/images`.
Estado operativo LOCAL:
`/opt/cloud-image-production/run-20261011T180000Z/masterdata` -> `/app/data`;
catalogo/correo/pendientes/recibos de migracion LOCAL:
`/opt/cloud-image-production/run-20261011T180000Z/image-state` -> `/app/image-state`.
Manifiestos LOCAL en `docs`; configuracion/clave/runtime privados en `config`,
raw de captura, `prior-runtime.json` y `compose.private.json` fuera de Git.
Symlink `active` selecciona ese run para la unidad dedicada.

Originales independientes del NAS conservados SIN permisos/owners modificados:
`/opt/abcd-control/repo/images`, `masterdata_data/kering`, `image_repository`,
`luxoptica_data`, `docs/Luxoptica` y estado/configuracion original.
Los cuatro contenedores antiguos permanecen DETENIDOS con sufijo
`-rollback-20261011`; imagen etiquetada `cloud-local-rollback:20261011`,
ID `sha256:5af62fade21e88c9f3fd57f23d08eb2e7b535fcb63f9933598a7a7c86ecb11e8`.
No habilitar ambos conjuntos simultaneamente.

Rollback posterior al arranque requiere cerrar admision/parar unidad nueva,
capturar otra vez SQLite/WAL/raw/clave y bytes nuevos NAS con checksums a
retencion nueva, conservar historial/avisos/mail-state y generar diferencias
contra final-plan. Importar/reconciliar representaciones/bytes y estado compatible
en un directorio LOCAL NUEVO, sin reemplazar originales. Comprobar los63links
y cualquier link posterior, recuentos/history, avisos enviados y processed/
pending_read_ids antes de reabrir para no duplicar envios/descargas.
Si hay conflictos/mapeos o acuses pendientes no compatibles con artefacto antiguo,
mantener cerrado hasta resolver; no elegir ultimo archivo ni descartar metadata.
Seleccionar override de rollback explicito (sin autoarranque NAS), imagen antigua
y estado local reconciliado; validar salud/galeria/ZIP/checksums antes de admision.
Los contenedores originales ofrecen retorno inmediato SOLO si se acredita
ausencia de nuevas escrituras; hoy esa condicion NO se cumple.

### Pendientes, sin omitir incidencias

153claves ambiguas de manifiestos sin archivos afectados en esta captura,
2187vistas Luxoptica unknown, e incidencias LOCAL previas permanecen pendientes;
no se inventaron EAN ni vistas. Fotos remoto no se activo. NASbackups comparte
fallo de NAS; los originales locales mantienen la copia independiente de
captura, pero NO contienen automaticamente nuevas escrituras posteriores.
Preparar backup independiente recurrente/replica solo con autorizacion posterior.
NFS hard ante caida real puede bloquear IO: guardas impiden fallback local,
no garantizan un timeout de kernel. No se repitio un fallo fisico de NAS.
No se fusiono ninguna PR ni se borraron originales/staging; no hubo cambios Odoo.
