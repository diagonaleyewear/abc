# Prompt Operativo Masterdata Luxottica -> Odoo

## Rol
Actúa como especialista en transformación de catálogos ópticos para Odoo.

## Objetivo
Transformar el Excel original de Luxottica a un Excel MASTERDATA limpio, normalizado y listo para importar en Odoo.

No limitarse a renombrar columnas: interpretar, limpiar, enriquecer y normalizar.

Este documento refleja el comportamiento actualmente implementado en la app y en el script de transformación.

## Usuarios y permisos

La aplicación admite dos perfiles:

- **Administrador**: credenciales `APP_USERNAME` y `APP_PASSWORD`; acceso a
  todas las áreas.
- **Master Data**: credenciales `MASTERDATA_USERNAME` y
  `MASTERDATA_PASSWORD`; acceso únicamente a Importador Masterdata,
  Diccionario y Dry-run Odoo.

En Docker, las cuatro variables deben estar definidas en el `.env` del host.
No se guardan contraseñas en el repositorio:

```env
APP_USERNAME=usuario_administrador
APP_PASSWORD=********
MASTERDATA_USERNAME=usuario_masterdata
MASTERDATA_PASSWORD=********
```

Las sesiones recordadas requieren además `AUTH_COOKIE_SECRET`, independiente
de las contraseñas y con al menos 32 caracteres. Genera un valor aleatorio
criptográficamente seguro (por ejemplo, 32 bytes aleatorios codificados como
hexadecimal) y guárdalo únicamente en el gestor de secretos o `.env` protegido
del host. Si falta o no alcanza la longitud mínima, el inicio de sesión normal
sigue disponible, pero no se emiten cookies de sesión recordada; no se usa
`APP_PASSWORD` como alternativa. La revocación y el límite de intentos se
guardan en el SQLite persistente configurado por `AUTH_STATE_PATH`.

El usuario limitado no verá en el menú Análisis ABC, Alertas ni Configuración.
Si intenta acceder directamente a una página restringida, la aplicación lo
bloquea igualmente.

## Entradas
- Archivo Excel de Luxottica.
- Diccionario personalizado editable desde la sección **Master Data > Diccionario**.
- Diccionarios desde BD:
  - diagonal_product_color
  - diagonal_product_color_dictionary
  - diagonal_product_material
  - diagonal_product_forma

## Diccionario personalizado

La aplicación incluye un apartado **Diccionario** para crear reglas de
transformación específicas del negocio. Cada regla tiene exactamente estas
columnas:

| Columna | Valor | Transformado |
|---|---|---|
| Color | BLACK | Negro |
| Forma | Cat Eye | Cat Eye |
| Género | KIDS | Niño |

El campo **Columna** se selecciona mediante un desplegable con el esquema fijo
de cabeceras del Excel original, antes de transformar el fichero. No es
necesario cargar un Excel adicional en este apartado y así se evitan errores de
escritura o referencias a nombres de columnas transformadas.

Las reglas se guardan en:

`docs/MasterData/masterdata_dictionary.json`

En producción Docker, el diccionario se guarda fuera de la imagen en:

`masterdata_data/masterdata_dictionary.json`

Esta carpeta está excluida de Git y se monta como volumen en `/app/data`, por
lo que los rebuilds y despliegues no sobrescriben las reglas existentes. Para
migrar una instalación que todavía tiene el JSON dentro del contenedor,
ejecutar una sola vez, antes de recrearlo:

```bash
mkdir -p masterdata_data
docker cp abcd-control:/app/docs/MasterData/masterdata_dictionary.json \
  ./masterdata_data/masterdata_dictionary.json
```

Después ya se puede actualizar y reconstruir:

```bash
git pull origin main
docker compose up -d --build --force-recreate
```

El valor se compara de forma exacta ignorando mayúsculas y espacios exteriores.
Las reglas personalizadas se aplican antes de los diccionarios de Odoo, por lo
que tienen prioridad para resolver excepciones del negocio. Después se aplican
las normalizaciones estándar de color, forma, género y demás campos.
Excepción: la categoría se obtiene siempre de la `Colección` original de
Luxottica, antes de cualquier diccionario. Ninguna regla personalizada de
`Colección` o `Categoría` puede cambiar esta clasificación.

Se han incorporado inicialmente las relaciones detectadas en la revisión:

- `Categoría`: `Gafas de vista` -> `MONTURAS`.
- `Color de las lentes`: `multiples` -> vacío.
- `Forma`: Aviador -> `AVIATOR`; Cuadrada, Irregular, Pillow, Rectangular y
  Square -> `RECTANGULAR/CUADRADA`; Mariposa y Ojo de gato -> `CAT EYE`;
  Ovalada, Pantos y Redonda -> `REDONDA/OVALADA`.
- `Material del frente`: `Acero` -> `METAL`; `Nylon` -> `ACETATO`; `Acetato y Metal` -> `ACETATO/METAL`.
- `PVP sugerido`: `Todo vacio` -> vacío.

Si una regla guardada no aparece en el XLS cargado, la aplicación muestra una
alerta con las reglas no encontradas. Esto permite detectar valores obsoletos,
errores de escritura o cambios en el fichero de Luxottica.

Además, la aplicación revisa los valores presentes en el XLS de las columnas
`Color`, `Descripción del color`, `Color del frontal`, `Color de las lentes` y
`Forma`. Si un valor no tiene relación ni con los diccionarios de Odoo ni con
una regla personalizada, se muestra una alerta con la columna, el valor y el
número de filas afectadas. Estos valores no se descartan automáticamente: el
usuario puede añadir la relación desde el apartado **Diccionario** y volver a
transformar el fichero.

El diccionario no modifica el Excel original. Sus transformaciones solo afectan
al resultado de la ejecución actual y a las descargas generadas.

## Reglas de depuración
- Eliminar accesorios.
- Eliminar marca Ralph Lauren.
- Mantener Polo Ralph Lauren.
- Mantener solo productos válidos para importación.
- No eliminar filas válidas.

## Reglas de marca
- Unificar:
  - Dolce & Gabbana
  - Dolce e Gabbana
  - Dolce y Gabbana
  - a: Dolce Gabbana
- Ray Ban siempre sin guion.
- Distinguir:
  - Ray Ban
  - Ray Ban Junior
  - Ray Ban Meta
- Ray Ban Junior:
  - Junior Sol -> RJ
  - Junior Vista -> RB
  - Género -> Niño
- Reemplazar RX y RY por RB (código de marca y código/modelo cuando aplique).
- Oakley: normalizar códigos OO/OJ/OY/OX.

## Reglas de modelo
- Eliminar "/" y "-".
- Concatenar resultado.
- Mantener formato texto.
- Extraer un "0" inicial cuando venga como prefijo del modelo.
- Mantener letras finales.
- Conservar espacios (incluido Prada y Miu Miu).

## Reglas de color
- La columna D `Color` contiene el código de color de Luxottica: conservarlo
  como texto, manteniendo ceros iniciales y códigos alfanuméricos, pero
  eliminar `/` de los códigos compuestos (por ejemplo, `C395/3` -> `C3953`).
- No consultar `Color` contra Odoo ni incluirlo en la alerta de valores no
  relacionados.
- Las columnas O y Q del origen, `Color del frontal` y `Color de las lentes`,
  sí se normalizan mediante los diccionarios de Odoo y el diccionario
  personalizado.
- Consultar primero el diccionario `diagonal_product_color_dictionary` unido a
  `diagonal_product_color`.
- Incluir también los nombres base de `diagonal_product_color` como alias.
- Resolver tanto valores alfabéticos como alfanuméricos antes de aplicar el
  fallback.
- Ejemplos: `BLACK` -> `Negro`, `GREEN` -> `Verde`.
- Unificar colores español/inglés.
- Regla especial: NEGRO/TALCO -> BLANCO.

## Reglas de lentes
- Monturas:
  - Vaciar Color de las lentes.
  - Vaciar Material de las lentes cuando corresponda.
- Gafas de sol:
  - Mantener Color de las lentes.
  - Normalizar Color de las lentes.

## Reglas de género
Mapear solo a:
- Hombre
- Mujer
- Unisex
- Niño

Todo producto infantil debe quedar en Niño.

## Reglas fotocromático/polarizado
- Valores permitidos: SI o NO.
- No se permiten vacíos.

## Reglas de categoría
Mapear solo a:
- MONTURAS
- GAFAS DE SOL

Usar exclusivamente la columna con cabecera `Colección` del Excel original:

| Colección original | Categoría final |
|---|---|
| Gafas de vista | MONTURAS |
| Gafas de sol | GAFAS DE SOL |
| Gafas de vista junior | MONTURAS |
| Gafas de sol junior | GAFAS DE SOL |

La comparación ignora mayúsculas, acentos y espacios repetidos. No se deduce
la categoría por marca, modelo, tipo, color o lente. Las lentes transparentes
con filtro de luz azul no implican gafas de sol, y `Productos finales: Gafas`
es una categoría genérica insuficiente.

Si la colección falta o no coincide con un valor reconocido, conservar la fila
con categoría vacía y mostrar un aviso bloqueante de revisión. El reporte JSON
incluye `category_review_rows` con fila Excel, UPC, modelo, colección original y
motivo. No importar estas filas sin resolverlas. La vista previa y las descargas
XLSX/CSV usan exactamente las mismas categorías en mayúsculas.

La `Colección` exportada sigue viniendo de la columna L; no confundirla con la
colección original utilizada para clasificar. Se conservan las demás reglas,
incluido el vaciado de campos de lentes de las monturas.

Regresión del cliente: EAN `8056266003066`, DG5116, color 3091, calibre 54:
`MONTURAS`. Para ejecutar la prueba del fichero real de 902 referencias, definir
`LUXOTTICA_REGRESSION_XLSX` con su ruta y ejecutar
`python -m pytest -q test_transform_luxottica_masterdata.py tests/test_app_exports.py`.
Sin esa variable, la prueba del lote real queda explícitamente omitida; no se
sustituye por 902 filas simuladas.

### Auditoría posterior de categorías en Odoo (solo lectura)

Esta corrección solo transforma ficheros; no modifica productos históricos.
La revisión de históricos requiere autorización independiente.

1. Conservar el Excel original y su hash. Derivar la categoría esperada de
   `Colección`, no de un MASTERDATA generado por el clasificador anterior.
2. Obtener con una cuenta de solo lectura una instantánea consistente de Odoo
   en una transacción `READ ONLY`. Cruzar UPC/EAN como texto con
   `product_product.barcode`, incluyendo productos archivados. No emparejar por
   modelo ni convertir EAN a número; separar duplicados y EAN sin coincidencia.
3. Leer categoría de `product_template.categ_id` y `product_category`. Incluir
   identificadores de variante y plantilla, ya que la categoría afecta a la
   plantilla y puede compartirse entre variantes.
4. Consultar por variante la existencia y estado de `stock_move`,
   `stock_move_line`, `stock_valuation_layer` y `account_move_line` unido a
   `account_move`. Confirmar antes el esquema real/version de Odoo. Distinguir:
   sin movimientos ni contabilidad; con movimientos pero sin apuntes; con
   operaciones contables (separar borrador y publicado). Si una consulta no
   puede verificarse, marcar estado desconocido; nunca asumir ausencia.
5. Exportar CSV/XLSX con EAN, modelo origen/Odoo, colección original, categoría
   actual y correcta, IDs, estado activo, duplicados, contadores de movimientos
   y apuntes, estado contable y discrepancia. Conservar fecha de extracción,
   hash del origen y versión del clasificador; no exportar credenciales.
6. Revisar discrepancias y variantes de la misma plantilla antes de proponer
   una corrección. No ejecutar UPDATE, importaciones, RPC de escritura ni
   cambiar impuestos, facturas, asientos, valoración o inventario. La autorización
   de este hotfix no autoriza esas correcciones.

## Reglas de forma
- Consultar el catálogo `diagonal_product_forma` de Odoo.
- Normalizar el valor de entrada y buscarlo en el diccionario de formas.
- Conservar el nombre canónico definido en Odoo.
- Ejemplos: `Aviator` -> `AVIATOR`, `Cat Eye` -> `CAT EYE`.
- Si no existe correspondencia, aplicar el mapa de respaldo y usar
  `RECTANGULAR/CUADRADA` cuando no sea posible determinar la forma con
  precisión.

Formas canónicas actualmente disponibles en la transformación:
- AVIATOR
- CAT EYE
- RECTANGULAR/CUADRADA
- REDONDA/OVALADA

## Reglas de materiales
Mapear a:
- ACETATO
- METAL
- ACETATO/METAL
- TITANIO
- BIOPOLIAMIDA

Normas vigentes en la transformación:
- `Nylon` -> `ACETATO`
- `Acetato y Metal` -> `ACETATO/METAL`
- `Acero` -> `METAL`

## Columna de colección
- Se toma de la columna L (`Nombre del modelo`) del archivo origen.

## Regla de PVP
- Priorizar `PVP sugerido`.
- Si el origen trae `PVP` (columna W) en lugar de `PVP sugerido`, usar `PVP` como fuente.

## Duplicados
- Dedupe por UPC.

## Salida técnica transformada (18 columnas)
Orden exacto:
- Marca
- Colección
- Modelo
- Color
- Calibre
- Ancho Puente
- Longitud Varilla
- Género
- Color Frontal
- Color Lente
- Forma
- Material Principal
- Fotocromático
- Polarizado
- PVO
- PVP
- Barcode
- Categoría

## Descarga XLS/CSV en la web
- Las descargas del transformado conservan las 18 columnas.
- El CSV usa `;` como separador, igual que el fichero MASTERDATA de referencia.
- Orden exacto del XLSX y CSV descargado:
  - Barcode
  - Categoría
  - Marca
  - Colección
  - Modelo
  - Color
  - Calibre
  - Ancho Puente
  - Longitud Varilla
  - Género
  - Color Frontal
  - Color Lente
  - Forma
  - Material Principal
  - Fotocromático
  - Polarizado
  - PVO
  - PVP

## Validaciones obligatorias
- Verificar que no se pierdan registros válidos.
- Verificar integridad UPC.
- Verificar ceros iniciales.
- Verificar Fotocromático y Polarizado con SI/NO.
- Detectar valores anómalos.

## Entrega final
1. Excel transformado.
2. Resumen de validación.
3. Número de registros procesados.
4. Número de registros descartados.
5. Incidencias detectadas.
