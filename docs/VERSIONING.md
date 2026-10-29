# Política de versiones

## Reglas
- SemVer `MAJOR.MINOR.PATCH`: MAJOR = cambio incompatible; MINOR = funcionalidad nueva; PATCH = corrección compatible.
- Fuente única: el fichero [VERSION](../VERSION) (sin prefijo `v`). La versión inicial es `0.1.0` (historial sin
  etiquetas previas; `0.x` porque no hay API estable declarada). No se asume `1.0.0`.
- Cada número de release es único y no se reutiliza: la etiqueta `vX.Y.Z` solo puede existir una vez y nunca se
  mueve ni se borra. `VERSION` **no** hay que subirla en cada PR: solo antes de una nueva release. Entre releases,
  los builds repiten `VERSION` y se distinguen por commit y digest (CI no falla por ello; solo emite un aviso
  informativo si `vX.Y.Z` ya existe). Antes de etiquetar se ejecuta
  `RELEASE_COMMIT=<sha> RELEASE_TAG_COMMIT=<commit de la etiqueta existente, o vacío> python scripts/release_version.py --check-release`,
  que falla si el número ya está etiquetado en otro commit. Se recomienda además una regla de protección de
  etiquetas `v*` en GitHub (no actualizar ni borrar).
- Preparar una versión = PR que cambia `VERSION` y `CHANGELOG.md`. Etiquetar (`vX.Y.Z`) es posterior y manual
  tras validar la política; **crear una etiqueta no dispara ningún workflow ni despliegue** (`deploy.yml` solo
  escucha `push` a ramas).

## Identidad de un release
| Dato | Dónde consta |
|---|---|
| Versión semántica | `VERSION` → `build-info.json` y etiqueta OCI `org.opencontainers.image.version` |
| SHA completo | `build-info.json`, etiqueta `org.opencontainers.image.revision` |
| Digest GHCR (inmutable) | anotación `cloud-image-identity` y resumen del workflow; el controlador sigue desplegando `ghcr.io/...@sha256:...` |
| Identificador de workflow | `actions-<run_id>-<attempt>` |
| Fecha UTC | `build-info.json`, `org.opencontainers.image.created` |
| Estado de despliegue | Solo el controlador de producción lo confirma; se refleja en `release-history.json` |

No se publican etiquetas mutables de versión como referencia de despliegue.

## Historial
[release-history.json](../release-history.json) registra por versión un estado: `prepared`, `published`
(requiere commit, digest, run y fecha), `failed` o `rolled_back` (requiere commit y `rolled_back_to`).
Se actualiza con evidencia (resumen del workflow / recibo del controlador), nunca de antemano.
Tras un rollback la imagen anterior arranca con sus propios metadatos, por lo que Cloud muestra la versión y el
commit realmente en ejecución.

## Visualización
Pie: `Cloud vX.Y.Z · commit abc1234`. Configuración (admin) → «Información de versión». Solo lee los metadatos
incluidos en la imagen (no consulta GitHub); lo desconocido se muestra como «No disponible».
