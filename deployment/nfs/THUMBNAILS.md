# Miniaturas Cloud: preparacion, no despliegue

## Base y alcance

Rama de trabajo basada en `prep/synology-nfs-images`, PR sginfoocio/abc#40,
HEAD `195b90fd6d9730c2b77d562af9dc371e1b027397`.
Comparado con el SHA documentado como desplegado
`057ca071a83e08faf9f636efd9155812fa5448d6`: solo difieren
documentacion NFS y la unidad systemd, no codigo funcional.
La identidad del servidor activo procede de [PRODUCTION.md](PRODUCTION.md);
no se ha vuelto a consultar el servidor en este trabajo.

No se fusiona, despliega, consulta Odoo/proveedores, recorre el repositorio
para mantener la cache ni modifica datos productivos.
El solicitante eligio entregar benchmark sintetico y dejar la medicion NFS
real pendiente. No interpretar los resultados de disco local como NFS real.

## Configuracion

| Variable | Valor inicial | Finalidad |
|---|---|---|
| `IMAGE_THUMBNAIL_CACHE_ROOT` | Explicita obligatoria con NFS | Directorio LOCAL persistente |
| `IMAGE_THUMBNAIL_CACHE_MAX_BYTES` | `1073741824` (1 GiB) | Payloads + indice + lock |
| `IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES` | `2147483648` (2 GiB) | Espacio local libre minimo |

Sin NFS, la ruta por defecto es `image-thumbnails` hermana del estado local.
No se aceptan NFS/SMB, tmpfs/ramfs, aliases ni solapamiento con originales,
catalogo, backups, Kering, temporales o work. Usar una raiz dedicada, por
ejemplo host `/opt/cloud-image-thumbnails`, contenedor `/cache/thumbnails`.
No usar el arbol `/opt/cloud-image-production/run-...`, `/app/data`, `/nas`,
backups o `/tmp`. Compartir este bind solo entre instancias de la app que
necesiten miniaturas; los importadores no generan miniaturas.

Clave: SHA-256 de `(checksum registrado del original, dimensiones, formato,
version del generador)`. JPEG 360x240, ajuste proporcional LANCZOS,
orientacion EXIF antes del ajuste, sin recorte. Version `exif-fit-rgb-v1`:
cambiarla cuando se modifique el algoritmo. Proveedores/mercados/EAN no
forman parte de la clave, por lo que comparten payload si los bytes coinciden.

Indice JSON LOCAL separado de SQLite con ultima utilizacion y evidencia de
verificacion por ruta/checksum/size/mtime/ctime. Una ruta nueva o modificada
se verifica una vez leyendo el original; una ruta ya verificada y sin cambios
sirve el payload sin leer original. Una representacion adicional del mismo
asset/ruta no requiere esa lectura inicial adicional.

Lock de archivo entre procesos (timeout 30s), indice/payload con fsync,
temporal fijo protegido por el lock y rename atomico. Se registra el tamaño
antes de publicar el payload: un crash deja como mucho una entrada sin bytes,
regenerable, no un payload fuera de contabilidad. LRU elimina payloads locales
antes de superar capacidad/reserva. El limite incluye indice y lock; durante
la publicacion puede existir el temporal atomico, sujeto tambien a la reserva.
Sin daemon, polling, limpieza del NAS ni recorrido de directorios de originales.

Permisos: UID:GID `1037:100`, directorio `0700`, archivos `0600`, umask `077`
del entrypoint existente. Ninguna escritura de miniaturas pasa por la funcion
de escritura de originales. Las guardas NFS siguen cerrando el acceso si falta
el montaje; nunca se crea un repositorio local sustituto.

## Acceso, catalogo y render

- Listas, filtros, contadores y paginacion se obtienen del catalogo SQLite
  local, sin `resolve`, `exists`, `stat`, checksum ni recorrido por original.
- Se generan solo las 12 fotos visibles en Kering / 24 en galeria general.
- Al cargar, cambiar pagina/filtros, reentrar o actualizar manualmente,
  se validan SOLO los originales visibles: ruta canonica, archivo, lectura y
  metadatos. Se reutiliza esa evidencia durante rerenders de la misma seleccion.
- Cada render mantiene las guardas del montaje y la autoridad del catalogo.
  Borrar/modificar una representacion en el catalogo impide servirla cacheada.
- Politica elegida explicitamente por el solicitante: los borrados/cambios de
  permisos EXTERNOS a SQLite se detectan al actualizar/cambiar seleccion, no
  inmediatamente en rerenders identicos. Detectarlos instantaneamente
  requeriria operaciones NFS por foto en cada render. Una modificacion externa
  que conserve todos los metadatos requiere una verificacion integral explicita.
- No se usa una miniatura si su validacion de acceso falla. Los fallos de
  espacio/cache/imagen producen aviso y placeholder, nunca fallback silencioso
  al original. Un indice corrupto se notifica; no se borra automaticamente.
- Ampliar lee/verifica el original al pulsar; la galeria general conserva esos
  bytes en la sesion para no releerlos en cada render. Actualizar los retira.
- ZIP general y de pedido se preparan SOLO al pulsar. Sus verificaciones,
  originales, nombres, alternativas y asociaciones no cambian. La cobertura
  mostrada sin exportar es la registrada en catalogo, no una certificacion de
  integridad de todas las fotos. El ZIP ya preparado es un snapshot en sesion:
  cambios de catalogo lo invalidan; cambios externos se recogen al actualizar
  manualmente y volver a prepararlo, no mediante stat de todos sus originales
  en cada rerender.

## Resultados reproducibles locales (2026-10-12)

Windows, Python 3.13.6, disco local; 48 JPEG sinteticos deterministas
1200x900, mismos 24 visibles en todos los escenarios. Baseline reproduce
el flujo anterior de galeria general: listado con comprobaciones por archivo,
ZIP automatico de todos los resultados y envio de originales visibles.
Render real de `st.image` con Streamlit AppTest, no navegador/red de usuario.
Una ejecucion, no percentiles ni garantia de latencia productiva:

| Escenario | Catalogo ms | Generacion ms | Lectura originales ms | Validacion acceso ms | Render ms | Total ms | Lecturas / bytes originales |
|---|---:|---:|---:|---:|---:|---:|---:|
| Antes, general | 17.03 | 0 | 20.91 | incluido en catalogo | 227.04 | 687.12 | 72 / 70,109,940 |
| Despues, fria | 1.44 | 543.89 | 6.89 | 8.16 | 6.84 | 1075.06 | 24 / 23,369,858 |
| Despues, caliente | 1.86 | 0 | 0 | 8.19 | 7.92 | 311.00 | **0 / 0** |

Total incluye administracion local de cache, ZIP baseline, AppTest y overhead;
no es suma solo de las columnas instrumentadas. La fria es mas lenta en esta
muestra: no ocultar ese coste. La caliente reduce el tiempo total un 54.7%;
la diferencia de render depende tambien de inicializacion de Streamlit.
El hot incluye una nueva validacion de pagina (metadatos), pero cero bytes
de originales. Las pruebas UI comprueban ademas cero validaciones por foto
en rerenders identicos.

Reproducir en un directorio NUEVO, local, aislado y fuera de temporales:

```sh
python -m scripts.benchmark_image_thumbnails \
  --directory /ruta-local-aislada/benchmark-nuevo \
  --output /ruta-local-aislada/resultado.json
```

La herramienta solo crea imagenes sinteticas; rechaza reutilizar directorio.
No apuntarla a fuentes productivas. El benchmark identifica
`real_nfs_measurements: null`. Medicion real NFS: **PENDIENTE**, autorizacion y
entorno no proporcionados; no se han simulado sus latencias ni sus contadores.

Validacion local completa: **337 passed, 9 skipped** (plataforma), 16 avisos
preexistentes del adaptador datetime SQLite. Tests de cache fria/caliente,
EXIF/proporcion, checksum/version/dimensiones, LRU/capacidad/reserva,
cache/codificacion corruptas, originales ausentes/permisos, catalogo eliminado,
montaje ausente, recuperacion entre commits atomicos, cuatro procesos/ocho
peticiones con UNA lectura inicial, paginas/actualizacion manual, ZIP intacto
y fallo explicito sin fallback. Lint E9/F63/F7/F82 y py_compile correctos.
Docker no existe en la maquina local: build/smoke se ejecutan en CI de la PR;
registrar su URL y resultado antes de autorizar despliegue.

## Medicion NFS real pendiente (sin modificar originales)

1. Verificar por lectura `build_info`/labels/Compose efectivo contra el SHA
   activo documentado; no reutilizar una imagen antigua sin comprobar identidad.
2. Seleccionar los mismos 24 IDs del catalogo, nombres/checksums y filtros.
   Crear una copia consistente LOCAL del catalogo con backup SQLite de solo
   lectura; no copiar una DB/WAL activa como archivos sueltos.
3. Entorno de ensayo independiente, sin red Odoo/proveedores, mismo
   UID1037:GID100, originales bind **ro** y cache LOCAL nueva fuera de produccion.
   Usar el codigo anterior y el candidato contra la misma copia de catalogo.
   No llamar a inicializadores/importadores con destinos de produccion ni
   levantar otros escritores. Mantener las guardas de identidad NFS existentes.
4. Medir por separado `perf_counter` en consulta SQL, `generate`,
   `verified_bytes`/lectura y envio de `st.image`; registrar bytes/peticiones.
   Usar AppTest para render servidor y navegador para tiempo end-to-end,
   sin equipararlos. Primera pasada fria, segunda caliente y tercera rerender
   identico; registrar tambien cambiar pagina, filtros y Actualizar.
5. Capturar `/proc/self/mountstats` o `nfsiostat` antes/despues y traza de
   `openat,read,pread64,newfstatat,getdents64` del proceso de ensayo. Separar
   operaciones de raiz/guardas de las de originales visibles. Los contadores
   globales incluyen importadores concurrentes: no atribuirlos sin aislar o
   correlacionar trazas. Cero lecturas logicas no implica cero RPC de metadatos.
6. Exigir hot con **0 open/read/pread64 de originales, 0 bytes y 0 generate**;
   catalogo sin `getdents64` ni metadatos por imagen, rerender igual sin stat
   por foto. Guardas de montaje constantes permanecen. No vaciar cache del
   kernel del servidor, desmontar NFS ni parar servicios para medir.
7. Publicar JSON/trazas anonimizadas y duraciones reales por escenario.
   Conservar el resultado sintetico separado. Si hot abre originales,
   no aprobar despliegue hasta localizar la lectura.

## Despliegue futuro, bajo autorizacion separada

1. CI verde incluyendo build y smoke Linux real `1037:100`, read-only,
   cap-drop ALL/no-new-privileges/network none; verificar SHA del artefacto.
2. Administrador prepara SOLO un directorio local nuevo
   `/opt/cloud-image-thumbnails` con owner1037:100/modo0700 y suficiente espacio
   para 1 GiB mas reserva2GiB. No cambiar permisos de originales/SQLite.
3. Exportar `CLOUD_THUMBNAIL_CACHE_HOST_PATH` y fusionar
   [thumbnail-cache.compose.yml](thumbnail-cache.compose.yml) AL FINAL del
   Compose activo verificado. `create_host_path:false` evita crear destinos
   por error. Validar el Compose efectivo sin imprimir secretos.
4. Confirmar NFS images/backups/work, estado local y dependencia systemd sin
   cambios. Solo reemplazar app por el nuevo artefacto bajo ventana autorizada;
   monitor/scheduler/alertas no requieren cache ni reinicio por este cambio.
5. Smoke de galeria fria/caliente, filtros/paginas/Actualizacion, ampliar,
   ZIP original/nombres, placeholders al retirar acceso en ensayo. Ejecutar
   medicion NFS anterior antes de dar por aceptado el rendimiento.
6. Rollback: volver a artefacto/configuracion anteriores de la app. No cambia
   esquema de SQLite; no restaurar bases ni originales para retirar cache.

Cache regenerable y fuera de migracion/replica: no se registra como asset,
backup, recibo o destino; los recorridos existentes son de originales/catalogo
y no incluyen la raiz separada. Excluida tambien de Git/contexto Docker por
nombre por defecto. Excluir la ruta personalizada en politicas externas de
backup/sincronizacion. Para recuperar un indice corrupto, parar SOLO las apps
que comparten esta cache y retirar exclusivamente ese directorio dedicado,
tras comprobar su ruta y separacion; regenerar sin tocar datos. No borrar
cache mientras hay escritores/lectores con lock abierto.
