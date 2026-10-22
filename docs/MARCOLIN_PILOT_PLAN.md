# Piloto Marcolin (#59) — plan de pruebas, NO ejecutado

Alcance: **exclusivamente los seis UPC ya confirmados** (se conservan literales, de 12 dígitos; no se
publican aquí) y su solicitud pendiente en Marcolin. Ampliación a 300 códigos: §3, solo preparada.
Estado del piloto: **no completado**. Los simuladores no validan el portal real. Sin escrituras en
producción ni importaciones sin autorización expresa; sin nuevas solicitudes; sin descargas masivas.

## 1. Pruebas que pueden hacerse ahora (sin entrega de Marcolin)

| # | Prueba | Cómo | Resultado esperado |
|---|---|---|---|
| A1 | Confirmación de los seis UPC en Odoo | `scripts/marcolin_odoo_pilot.py` por `docker exec -i … python - --partner <id> --upc-file … --out <volumen escribible> < scripts/marcolin_odoo_pilot.py` (solo lectura, informe a volumen permitido) | informe completo; exit 0; UPC literales |
| A2 | Cobertura en catálogo | mismo script (`--catalog`, `--image-root`, `--verify-files` opcional) | «sin registros», «sin vistas válidas» o «cobertura desconocida»; catálogo inaccesible ≠ ausente |
| A3 | Identidad UPC-12 / EAN-13 | dry-run de la migración y plan con ambos formatos | un solo registro vivo por identidad; sin solicitud duplicada |
| A4 | Exportación Marcolin | `export_ean_list` | un código por línea, UPC originales conservados |
| A5 | Relación UPC ↔ modelo/color | `record_portal_groups` con los grupos del portal (4 referencias, 6 UPC) | todos los UPC con su referencia; estados inactiva/activa conservados |
| A6 | Pendiente sin reenvío | plan repetido con la solicitud en curso | no se crea otra; sin reenvío por días de espera |
| A7 | Cloud sin Odoo y sin portal | abrir la página sin acciones | no contacta portales ni Odoo al renderizar |
| A8 | Regresión Kering/Luxottica | suite completa (812 correctas / 15 omitidas en local) | sin cambios |

## 2. Pruebas bloqueadas por la entrega de Marcolin

La preparación rara vez tarda menos de 24 h y puede tardar días (dato aportado por el usuario). **Esperar;
no repetir la solicitud.** Cuando haya enlace/ZIP, con acceso humano autorizado:

| # | Prueba | Criterio de aceptación |
|---|---|---|
| B1 | Estado de la solicitud en el portal | «My pending requests» refleja la entrega; registrar el estado verbatim |
| B2 | Descarga del ZIP | descarga manual autorizada; registrar tamaño y SHA-256; **no** declarar la política de caducidad hasta observarla |
| B3 | Integridad y nombres | ZIP válido; estructura y convención de nombres documentadas tal cual |
| B4 | Manifiesto | existe o no; si existe, qué UPC/referencia declara; si no existe, ninguna asignación automática |
| B5 | Correspondencia imagen↔UPC | solo con evidencia (manifiesto/metadatos) o revisión autorizada; el origen Odoo **no** es evidencia |
| B6 | Vistas | documentar las vistas realmente presentes; las cuatro vistas de la biblioteca no son política de Marcolin |
| B7 | Normalización y duplicados | nombres normalizados; duplicados por SHA-256 detectados; ambiguos a revisión |
| B8 | Miniaturas y originales | miniaturas generadas; SHA-256 de originales sin cambios |
| B9 | Importación (**requiere autorización**) | primero en una copia/staging; luego producción solo con permiso |
| B10 | NAS | originales y miniaturas disponibles en el repositorio común; sin escrituras sobre originales existentes |
| B11 | Visualización y búsqueda en Cloud | búsqueda por UPC y por EAN-13 equivalente devuelve las mismas imágenes |
| B12 | Odoo | comparar con el producto por consulta de solo lectura; **no** actualizar imágenes en Odoo productivo |
| B13 | Idempotencia | reimportar el mismo ZIP: sin duplicados ni pérdidas (la huella SHA-256 lo impide) |
| B14 | Errores y recuperación | ZIP sin manifiesto, archivo corrupto y respuesta ambigua acaban en revisión/errores, sin pérdida |

## 3. Ampliación controlada a 300 códigos (preparada, no ejecutada)

Los 300 son el máximo que indica la interfaz; solo se han probado seis. El conector tiene 50 como valor
provisional. **Ningún código se considera validado hasta disponer de evidencia real.**

- Precondición: B1–B14 superados con los seis UPC y autorización expresa.
- Escalado: 6 → 25 → 50 → 100 → 300; un paso por solicitud, esperando entrega y verificación completa antes del siguiente.
- Validaciones automáticas antes de enviar: formato y dígito de control, UPC-12/EAN-13 normalizados a una identidad,
  duplicados eliminados, códigos ya en curso excluidos, máximo del lote, original conservado literalmente.
- Procesamiento por lotes: lotes del tamaño del paso; concurrencia 1 solicitud viva por proveedor.
- Reintentos: solo ante error comprobado de envío; nunca por «pendiente». Idempotencia por identidad.
- Registro: cada operación con identidad, estado y causa de error; sin datos comerciales reales ni credenciales.
- Aceptación por paso: 100 % de UPC con su referencia; 0 asignaciones sin evidencia; 0 modificaciones de originales;
  0 duplicados; errores listados y revisados.
- Descartes: no eliminar referencias inactivas automáticamente.
- Parada inmediata: cualquier CAPTCHA/MFA no previsto, aviso del portal, discrepancia de correspondencia o error de integridad.
