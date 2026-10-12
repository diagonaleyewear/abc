# Repositorio comun de imagenes por EAN

## Inventario de contratos previo a cambiar rutas

Revision de `main` 25a6252 (2026-10-10), sin acceso ni migracion en Cloud.
Las integraciones de imagenes presentes son Luxoptica/Graph y Kering.
Adyen exporta informes, no fotografias. No existe sincronizador NAS en el codigo
actual: cualquier tarea externa debe inventariarse antes del cambio en Cloud.

| Superficie | Contrato vigente |
| --- | --- |
| Original Luxoptica | `_sanitize_filename` conserva nombre y extension; sustituye `\ / : * ? " < > \|` por `_`. No recodifica imagenes. |
| Modelo Luxoptica | `_model_from_image_name`: texto anterior a `__`, elimina un cero inicial; solo afecta al directorio antiguo. |
| Identificacion Luxoptica | `_image_model_color_key` normaliza modelo/color, elimina `_NNNA` del color; manifiestos resuelven solo combinaciones con un EAN unico. |
| Vistas Luxoptica | `_market_view_from_image_name`: `noshad__fr` -> V1 (frontal), `noshad__qt` -> V2 (perspectiva), `shad__lt` -> V3 (lateral). Otras vistas no acreditan esas tres. |
| Farfetch | Solo originales `.png`, `<id>_V1.png`, `<id>_V2.png`, `<id>_V3.png`. |
| Miinto | Solo originales `.png`, `<id>_V1.jpeg`, `<id>_V2.jpeg`, `<id>_V3.jpeg`. Los bytes siguen siendo PNG: **no corregir ni recodificar** como parte de esta migracion. |
| Kering anterior | `ImageStore.path`: `<vista>.img`, bytes originales JPEG/PNG/WEBP, sin recodificar. Estos archivos existentes nunca se renombran por unificar o exportar. |
| Kering nuevo | Solo una vista acreditada antes de renombrar recibe `<modelo>__<color>__noshad__fr/qt` o `shad__lt`, con extension original. Sin evidencia conserva el nombre de origen saneado. |
| ZIP actual | `<EAN>/<nombre solicitado>` de todas las representaciones validas, incluidas extensiones historicas `.img` y de mercado, mas `manifest.json`; sin proveedor en carpetas. |
| Galerias | Luxoptica deducia modelo/EAN/mercado de carpetas; Kering usa `valid_views` y snapshots de pedidos por EAN. |
| Historial | Kering `history.sqlite3`: runs, attempts, snapshots/results con EAN; configuracion cifrada independiente. Graph `.mail_download_state.json` y `.market_pending.json`. |
| Colisiones | Luxoptica sobrescribia con `write_bytes`/`copy2`; Kering no reemplazaba vistas validas. No hay regla no destructiva de renombrado de colisiones. |

Las funciones de nombre anteriores se mantienen. La ubicacion nueva es
`IMAGE_REPOSITORY_ROOT/<EAN>/<nombre>`, nunca por pedido, modelo o proveedor.
Si dos contenidos distintos necesitan el mismo nombre, el primero permanece y
la alternativa usa `stem__<sha256 completo>.extension`; se registra el conflicto
y el nombre solicitado. No se renombra el primero ni se pierde ninguno.
Los nombres de mercado identicos en bytes son representaciones/alias: mantienen
su nombre y extension en metadatos y exportaciones, sin otra copia fisica.

## Configuracion y operaciones

`IMAGE_REPOSITORY_ROOT` es la unica raiz de fotografias y bloqueos compartidos.
Por defecto es `repo/images` relativo al proyecto (no al directorio de trabajo).
En Cloud usar un volumen persistente comun, montado en **app, monitor Graph y
programador Kering**. `KERING_DATA_ROOT` conserva configuracion e historial, no
fotografias nuevas. `M365_DOWNLOAD_ROOT` deja de controlar fotografias.

Para almacenamiento separado, `IMAGE_REPOSITORY_STATE_ROOT` contiene catalogo
SQLite y estado de correo/mercados, temporales de recepcion, planes y recibos,
en volumen local persistente. `IMAGE_REPOSITORY_BACKUP_ROOT` conserva backups
de imagenes y archivos comprimidos en una ubicacion independiente; los backups
de bases de datos/configuracion/metadatos siguen bajo estado local. Sin esas
variables se conserva el layout local anterior. Con NFS se exige fuente exacta
en `IMAGE_REPOSITORY_NFS_SOURCE` y, para backups remotos,
`IMAGE_REPOSITORY_BACKUP_NFS_SOURCE`; ausencia/montaje incorrecto bloquean acceso
sin crear fallback local. Preparacion y limites:
[Synology NFS](../deployment/nfs/README.md).

El catalogo registra EAN, proveedor, origen, vista canonica, mercado, fecha UTC,
SHA-256, huella visual, nombre solicitado y ruta relativa; las asociaciones
de pedidos solo contienen EAN. Las variantes de mercado no suman vistas.
Las tres vistas validas necesitan imagen verificable de al menos 600 px en ambos
ejes y huellas visuales distintas (contrato Kering existente).
Los originales menores tambien se conservan, pero no acreditan completitud.

Bloqueos de fichero compartidos por EAN cubren consulta, descarga y escritura.
SQLite usa transacciones durables; los bytes se escriben antes de publicar
metadatos. Todas las herramientas deben usar la misma raiz y sus bloqueos.
SQLite activo nunca se admite sobre NFS. Los locks de bytes permanecen junto a
las imagenes para coordinar escritores; NFS debe acreditar locking y rename.

### Granularidad Luxoptica comprobada

Kering consulta el EAN bajo bloqueo y no abre el portal si V1/V2/V3 estan completas.
Para un EAN parcial enumera todo el visor, omite URLs ya validas y conserva
vistas adicionales/desconocidas. No deduce vistas del orden ni del nombre
generado. La identificacion previa y su evidencia se describen en
[Kering](KERING_IMAGES.md#deteccion-de-vistas-y-alcance-actual-issue-38).
Los generadores de solicitudes Luxoptica excluyen EAN que ya tienen las tres
vistas. El adaptador del portal Luxoptica existente selecciona **Todas las
vistas** y Graph recibe un ZIP opaco completo: no existe en este repositorio un
API verificado de descarga por vista. Para un EAN parcial se transfiere ese ZIP,
se deduplican sus bytes al incorporarlo y se conservan alternativas distintas.
No afirmar que se evita la transferencia de entradas individuales ya validas
de ese ZIP. Cambiar la seleccion del portal requiere contrastar opciones y
contratos reales; no se inventan selectores ni se descartan alternativas.
Esta excepcion para EAN parciales fue aceptada por el solicitante durante la
implementacion: permitir ZIP completo, deduplicar al importar y documentarlo.

## CLI (ejemplos para staging, no ejecutar en produccion)

No existe migracion automatica al arrancar la app. El inventario usa solo
lectura y escribe su plan fuera de los origenes. Origen y destino deben ser
separados. En Windows usar rutas de Windows; en Cloud usar sus rutas montadas.

```powershell
python migrate_image_repository.py inventory --source "Luxoptica=E:\staging\lux-images" --source "Kering=E:\staging\kering" --target "E:\staging\common" --output "E:\staging\plan.json"
python migrate_image_repository.py apply --plan "E:\staging\plan.json"
python migrate_image_repository.py verify --plan "E:\staging\plan.json"
python migrate_image_repository.py recover --plan "E:\staging\plan.json" --target "E:\staging\recovery"
$env:IMAGE_REPOSITORY_ROOT = "E:\staging\common"
python migrate_image_repository.py sync-nas --target "E:\staging\nas-replica"
python migrate_image_repository.py recover --plan "E:\staging\plan.json" --repository "E:\staging\nas-replica" --target "E:\staging\nas-recovery"
```

`inventory` es tambien la simulacion: muestra destinos propuestos, colisiones,
checksums, alias y bloqueos. `apply` rechaza un plan alterado, originales
cambiados, EAN sin resolver o imagenes no reutilizables. Estas ultimas requieren
revision/reparacion sobre una copia de staging, conservando el backup original,
y repetir el inventario; no hay un bypass silencioso. Para resolver ambiguedades, pasar `--mapping`
al inventario con un JSON revisado, no editar el plan:

```json
{
  "files": {
    "E:\\staging\\legacy\\foto.png": {
      "ean": "0012345678901",
      "view": "frontal",
      "market": "",
      "origin": "integracion_original",
      "orders": ["6094"],
      "metadata": {"modelo": "GG0998S"}
    }
  }
}
```

Los planes nuevos v2 firman tambien las raices independientes de estado y
backups. Los planes v1 siguen validos para su layout original, no para cambiar
la ubicacion mediante variables de entorno; repetir inventario antes del corte.
Los recibos contienen la ruta efectiva si ya existian contenidos en destino.
`.migration/<plan>/originals` conserva **todos** los archivos inventariados,
incluidos JSON, ZIP, informes, configuracion e historial SQLite, sin cambiar
bytes. No exponer esta carpeta por HTTP: puede contener metadatos privados o
configuracion cifrada. La recuperacion reconstruye cada origen bajo su ID
estable (hash de su ruta), comprueba todos los checksums y nunca sobrescribe
originales. En layout legacy una replica incluye catalogo, imagenes, planes y
backups. En layout separado `sync-nas` legacy se bloquea expresamente; verificar
y recuperar usan estado local y backup independiente firmados en el plan.

Validacion offline:

```powershell
python -m pytest tests\test_image_repository.py tests\test_kering_images.py -q
```

Inventario/simulacion **local**, 2026-10-10 (no Cloud): 1.211 archivos,
1.204 imagenes, cuatro snapshots historicos de pedidos. Se detectan 388
imagenes sin EAN univoco y siete que no cumplen la validacion de reutilizacion.
El plan queda bloqueado hasta resolver mapeos y las siete imagenes no
reutilizables; no se aplico la migracion ni se
creo el destino. Estas cantidades no describen el volumen de produccion.

## Plan de migracion (no ejecutado en produccion)

1. Identificar volumen real, rutas externas/NAS, todos los proveedores y tareas.
   Pausar escritores antiguos y nuevos durante inventario y corte; guardar
   backups recuperables de volumen, configuracion e historial. No borrar origen.
2. Crear inventario/simulacion con la CLI. Incluir raiz antigua Luxoptica,
   raiz `KERING_DATA_ROOT` y cualquier otra integracion. Un EAN ambiguo necesita
   un mapeo revisado: no se asigna a un EAN inventado. Revisar archivos sin EAN,
   corrupcion, colisiones, alias, metadatos y pedidos.
3. Revisar el plan JSON. Completar mapeos y repetir el inventario si cambia
   cualquier origen. Primero ensayar con copias en staging.
4. Aplicar **explicitamente** el plan revisado. Copiar, no mover. Cada archivo
   tiene checksum, backup y recibo durable. Una repeticion retoma entradas
   verificadas; cambios en el origen invalidan el plan.
5. Verificar todos los destinos, backups, referencias por EAN y restaurar a
   otro directorio. Comparar bytes/checksums con inventario y comprobar galeria,
   pedidos historicos, ZIP y pendientes. Conservar snapshots/results originales.
6. Configurar la raiz comun en todos los servicios y desplegar el codigo.
   Recuperar estado de correo migrado antes de reactivar Graph. El historial
   Kering sigue en su raiz persistente original. Los pendientes se recalculan
   desde representaciones, no desde rutas antiguas.
7. En el layout legacy, replicar al NAS mediante copia verificada del catalogo
   y archivos. En el layout NFS separado, el catalogo operativo permanece local
   y `sync-nas` legacy esta bloqueado: usar backups independientes y el ensayo
   de recuperacion de `deployment/nfs/README.md`, nunca copiar la exportacion
   de imagenes sobre si misma. Revisar jobs externos para que lean `<raiz>/<EAN>`
   y no rutas por proveedor/modelo.
8. Reactivar procesos y validar en piloto. Rollback: pausar, volver a codigo/
   configuracion anteriores y usar originales intactos o la recuperacion
   verificada. **Esta herramienta nunca elimina originales**. Retirada posterior
   solo con aprobacion humana y politica de retencion tras verificar recuperacion.
