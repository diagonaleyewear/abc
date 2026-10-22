# Despliegue de imágenes de proveedores (#59) — procedimiento preparado, NO ejecutado

Estado documental: **preparado, sin ejecutar**. Nada de lo descrito aquí se ha aplicado en producción.
Este documento no autoriza ningún despliegue, migración ni descarga. Cada paso con efecto necesita aprobación expresa.

## 1. Trazabilidad de GitHub

Las PR #70–#77 llegaron a `main` mediante **un único push directo** de una rama de integración
(`735f776`), no mediante fusiones individuales. No existe un SHA de fusión por PR. Comprobado con
`git merge-base --is-ancestor`: la cabeza de cada rama está contenida en `main`.

| PR | Rama | SHA original (cabeza) | Contenida en `main` |
|---|---|---|---|
| #77 | `ci/59-stacked-pr-checks` | `fccaa01` | sí |
| #70 | `feat/59-provider-core` | `deb8e8c` | sí |
| #71 | `feat/59-marcolin` | `2d343cf` | sí |
| #72 | `feat/59-safilo` | `b7b7f90` | sí |
| #73 | `feat/59-nuorder` | `9027198` | sí |
| #74 | `feat/59-provider-ui` | `35f00c3` | sí |
| #75 | `feat/59-odoo-orders` | `c306029` | sí |
| #76 | `feat/59-marcolin-pilot` | `cc4cef6` | sí |

Cierre de las PR abiertas (#71, #72, #73, #75, #76): pendiente de una acción humana en GitHub, con el
comentario «Contenido integrado en `main` (`735f776`) mediante push directo; SHA original `<sha>`».
Las PR cerradas por GitHub al recibir los commits (#70, #74, #77) figuran como *closed*, no *merged*.

Texto propuesto para la descripción de #71 (historial de estados del portal):

> `marcolin_portal_groups` conserva el **historial** de estados que el portal devuelve por UPC y
> referencia modelo/color. Un cambio de estado (por ejemplo, inactiva → activa) añade una fila nueva;
> la vista actual (`portal_groups`) usa el último estado. Nunca se descartan referencias inactivas y
> el estado del portal no modifica el estado de la solicitud, de modo que una solicitud pendiente
> durante días no se reenvía. Commit: `2d343cf`. Verificado: workflow #239 correcto.

## 2. Defecto detectado que bloquea el uso en producción (separado)

La imagen publicada de `735f776` **no incluye** `provider_images*.py`, `provider_odoo_orders.py`,
`repository_catalog_readonly.py`, `marcolin_images.py`, `safilo_images.py` ni `nuorder_images.py`
(el `Dockerfile` copia módulos de forma explícita) aunque la página «Proveedores de imágenes» está
en la navegación. Abrirla provocaría `ModuleNotFoundError`; el resto de la aplicación no se ve afectado
(la importación es perezosa). La comprobación de imports del workflow tampoco los cubría.

Corrección mínima propuesta, en rama aparte: `fix/59-image-provider-modules` (commit `ed0e594`):
`COPY` de los módulos y del script de migración, ampliación del smoke de imports y una prueba de
regresión (falla sin la corrección). **No desplegar `735f776` si se quiere usar la página; desplegar
un commit de `main` que contenga esta corrección.**

## 3. Auditoría previa de producción

**No se ha podido realizar desde este entorno**: no hay acceso a `s2026`, Docker, NAS, Odoo productivo
ni al portal. Se debe completar, con comandos de solo lectura, antes de autorizar:

| Comprobación | Comando de solo lectura | Resultado |
|---|---|---|
| Imagen y digest desplegados | `docker inspect abcd-app --format '{{.Image}} {{index .Config.Labels "org.opencontainers.image.revision"}}'` | pendiente |
| Contenedores activos | `docker ps --format '{{.Names}} {{.Image}} {{.Status}}'` | pendiente |
| Variables necesarias | listar solo **nombres** (`docker inspect … --format '{{range .Config.Env}}{{println (index (split . "=") 0)}}{{end}}'`) | pendiente |
| Montajes y permisos NAS | `docker inspect abcd-app --format '{{json .Mounts}}'`, `df -h <volumen>`, `stat` | pendiente |
| Almacén de solicitudes | `ls -l <root>/provider_requests.sqlite*`; simulación de la migración (§4.4) | pendiente |
| Odoo / Marcolin | solo conectividad ya existente de Cloud; **no** contactar el portal | pendiente |
| SQLite del host/imagen | `python -c "import sqlite3;print(sqlite3.sqlite_version)"` en el contenedor: exigir **≥ 3.31** (columnas generadas) | pendiente |

## 4. Procedimiento de despliegue (no ejecutar sin autorización)

Mecanismo existente (`deployment/cloud/README.md`): `deploy.yml` publica y firma la identidad de la
imagen; `deploy-cloud-production.yml` se lanza por `workflow_dispatch` (o `workflow_run` solo si
`CLOUD_AUTO_DEPLOY=true`), exige el entorno `production` con revisor y un controlador root; solo se
reemplaza `abcd-app`; un fallo restaura `cloud-production:rollback-<previous>`. **Comprobar antes que
`CLOUD_AUTO_DEPLOY` no sea `true`** (valor actual no verificado desde aquí; el último `workflow_run` quedó
*skipped*).

1. **Preflight.** Checklist de §6 completo; `main` estable; el commit a desplegar es la cabeza actual de `main`
   y contiene la corrección de §2; `tests` y `build-and-push` correctos sobre ese SHA.
2. **Identidad de la imagen.** Tomar el digest de la anotación `cloud-image-identity` del job `build-and-push`
   (`ghcr.io/sginfoocio/abc@sha256:… <commit> actions-<run>-<attempt>`) y compararlo con el del contenedor
   tras el despliegue. No usar etiquetas mutables.
3. **Copias de seguridad.** Antes de cualquier cambio: copia de `provider_requests.sqlite` con
   `sqlite3 <db> ".backup '<dest>'"` (respeta WAL), copia del catálogo del repositorio de imágenes por el mismo
   método, y `sha256sum` de cada copia. **Prueba de restauración:** restaurar en un directorio temporal y ejecutar
   `PRAGMA integrity_check` y un recuento de tablas. No sobrescribir originales del NAS.
4. **Simulación de la migración.**
   `python scripts/provider_requests_migrate.py --root <dir>` (sin `--apply`): informa del esquema y de conflictos
   UPC-12/EAN-13. Salida 3 = conflictos: cancelar uno de los pares por la máquina de estados normal y repetir.
   `--apply` solo con autorización, hace copia previa y únicamente añade una columna virtual y un índice único;
   no borra ni reescribe. La aplicación **nunca** la ejecuta sola: con un almacén antiguo lanza
   `StoreMigrationRequired` (la pantalla de proveedores falla de forma explícita, el resto no). Si el almacén
   aún no existe, se crea con el esquema nuevo.
5. **Compatibilidad de esquemas.** Verificar versión de SQLite (§3), que el dry-run no muestre conflictos y que el
   catálogo de imágenes sea legible en solo lectura (lo cubre `repository_catalog_readonly`).
6. **Actualización controlada.** Lanzar `Deploy Cloud Production` manualmente (`workflow_dispatch`) sobre la
   cabeza de `main` y aprobar el entorno `production`. Ventana acordada; una sola aprobación explícita.
7. **Comprobaciones posteriores.** Health `/_stcore/health`; digest y revisión del contenedor = los esperados;
   `abcd-app` es el único servicio cambiado; abrir «Proveedores de imágenes» sin ejecutar ninguna acción
   (la página no contacta portales al renderizarse); abrir Imágenes Kering y Luxottica (regresión); revisar
   logs sin errores ni secretos; comprobar que no hay planificadores nuevos activos.
8. **Rollback de aplicación.** Automático si fallan las comprobaciones del controlador; manual: redesplegar el
   commit anterior con el mismo flujo o restaurar `cloud-production:rollback-<previous>`. **Rollback de datos:**
   solo si se aplicó la migración: parar escrituras, restaurar la copia verificada de §4.3 (la migración es
   aditiva, por lo que la versión anterior del código sigue leyendo el almacén migrado; el índice antiguo se
   conserva) y repetir `integrity_check`.
9. **Parar de inmediato si:** falla un `integrity_check` o la restauración de prueba; el dry-run muestra conflictos
   sin resolver; el digest no coincide; SQLite < 3.31; espacio libre insuficiente; aparece un error en Kering o
   Luxottica; algún proceso intenta escribir o borrar originales; se detecta un secreto en logs.

## 5. Inventario de bloqueos

| Bloqueo | Tipo | Siguiente paso |
|---|---|---|
| Imagen sin módulos de proveedores | Defecto de código | Revisar y fusionar `fix/59-image-provider-modules` |
| Auditoría de producción sin hacer | Acceso | Ejecutar §3 con acceso a `s2026` |
| `CLOUD_AUTO_DEPLOY` no verificada | Configuración | Confirmar que no es `true` |
| Migración del almacén sin ejecutar | Datos | Dry-run (§4.4) y revisión |
| ZIP, nombres, manifiesto, vistas, caducidad | Portal Marcolin | Esperar entrega de la solicitud pendiente |
| Correspondencia imagen↔UPC, importación | Validación real | Piloto (`docs/MARCOLIN_PILOT_PLAN.md`) |
| Límite real de lote (UI: 300; probados: 6; conector: 50 provisional) | Portal | Lote escalonado |
| Script de #76 sin ejecutar en producción | Ejecución | Solo lectura, con autorización |
| Sáfilo y NuORDER sin validar en portales reales | Portal | Fuera del alcance de este plan |
| PR abiertas pendientes de cierre manual | GitHub | §1 |

## 6. Checklist de preflight

- [ ] `main` en el SHA esperado y `tests` + `build-and-push` correctos sobre él.
- [ ] La corrección de §2 está en ese SHA.
- [ ] `CLOUD_AUTO_DEPLOY` no es `true` (o el despliegue automático está aceptado expresamente).
- [ ] Digest de la imagen anotado y registrado.
- [ ] Digest/revisión actuales de `abcd-app` anotados (para el rollback).
- [ ] SQLite ≥ 3.31 en la imagen.
- [ ] Copias hechas, con `sha256sum`, y restauración de prueba con `integrity_check` correcto.
- [ ] Dry-run de la migración sin conflictos (o conflictos resueltos).
- [ ] Espacio y permisos del NAS verificados; originales en solo lectura para este proceso.
- [ ] Credenciales sin cambios y fuera del repositorio y de los logs.
- [ ] Aprobador y ventana acordados.

Nada de esto se ha ejecutado.
