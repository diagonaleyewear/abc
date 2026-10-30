# Comparador de precios Vytria — auditoría y arquitectura

## Alcance de la auditoría

Revisión de la navegación Cloud, fuentes Odoo, importaciones/exportaciones Excel,
persistencia, procesos programados, despliegue y libro piloto. No se consultaron
los dominios de competidores ni se modificaron sus datos o el libro de origen.

## Evidencia del repositorio

- Cloud es una aplicación Streamlit. `app_enhanced.py` declara las páginas y
  `app_navigation.py` construye grupos por rol; `app_pages/` contiene módulos
  de página. La sección comercial se puede añadir sin cambiar los flujos de
  imágenes, pedidos, Odoo o automatizaciones existentes.
- `pandas` y `openpyxl` ya están instalados y se usan para leer y generar Excel.
  Los importadores recientes leen campos textuales con `dtype=str`, apropiado
  para conservar ceros iniciales de EAN/UPC.
- El catálogo operativo y el análisis ABCD leen PostgreSQL de Odoo mediante
  SQLAlchemy. `db_loader.py` expone `pp.barcode`, referencia, marca/modelo y
  `pt.list_price`; además calcula `PVO` con `product_supplierinfo.price_discount`
  o `price` antes de recurrir a `list_price`. Esos importes no prueban el precio
  efectivo publicado en la tienda Vytria.
- No se encontró una integración PrestaShop ni otra integración de catálogo
  web/checkout de Vytria en el código revisado. La fuente del precio final,
  promociones, mercado, impuestos y disponibilidad del escaparate queda por
  confirmar con el responsable de la tienda.
- Cloud ya usa bases SQLite específicas bajo `/app/data` (montado desde
  `masterdata_data`), índices, bloqueo de inicialización, modo WAL, estados de
  ejecución e informes de proceso. El comparador debe tener su propio almacén
  SQLite y una ruta configurable dentro de ese volumen persistente; no debe
  reutilizar ni alterar las bases de imágenes, pedidos o actividad.
- Cloud registra páginas con `st.Page` y `build_navigation`; la biblioteca
  establece patrones visuales y de accesibilidad en `cloud_design.py` y
  `docs/CLOUD_DESIGN_SYSTEM.md`. La nueva página debe usar esos tokens y
  widgets nativos.
- Ya existe infraestructura de servicios independientes en Compose y procesos
  con bloqueos, estado durable y recuperación. No hay motivo para introducir
  otro scheduler en el MVP. No se debe habilitar un proceso periódico para el
  comparador durante el piloto.

## Libro piloto

`Prueba - revision competidores - Vytria.xlsx` contiene dos hojas:

- `100 gafas`: 100 filas de producto; las seis columnas (`MARCA`, `NOMBRE`,
  `MODELO`, `COLOR`, `CATEGORIA`, `EAN`) están completas. Hay 100 EAN distintos,
  sin duplicados; 53 identificadores UPC-12 y 47 EAN-13. Los 100 dígitos de
  control son válidos. Para UPC-12, la forma normalizada prevista es un EAN-13
  con cero inicial, sin reemplazar el identificador original.
- `Competidores`: 14 nombres y URL base. No define país/mercado, moneda,
  método autorizado, límites de consulta, disponibilidad de feed ni precio.
  Estos valores no deben inferirse como hechos confirmados únicamente a partir
  del nombre de dominio.

Competidores listados: Óptica Óptima, Óptica Bassol, Blickers, Visual-Click,
Edel-Optics, Amevista, Mia Burton, Optical H, Domanci, Óptica Universitaria,
ConGafasDeSol, Céline Opticien Lunetier, Visio Factory y Otticait Eyewear.

## Propuesta técnica

1. Añadir una página independiente bajo **Análisis comercial → Comparador de
   precios**, siguiendo los grupos, roles y componentes Cloud actuales.
2. Separar lógica de dominio, acceso SQLite y presentación Streamlit. Mantener
   un almacén propio con productos identificados por EAN normalizado, catálogo
   de competidores y ofertas append-only para conservar el histórico.
3. Importar productos y competidores sin borrar valores existentes cuando una
   columna venga vacía. Usar GTIN-12/GTIN-13 como texto, validar longitud y
   dígito de control, detectar duplicados y devolver informe de importados,
   actualizados y rechazados. La reimportación debe ser idempotente.
4. Mantener el precio Vytria con su origen y fecha. Para el piloto, usar una
   importación Excel separada si no se confirma antes una fuente de precio
   público fiable. No presentar el PVO actual de Odoo como precio de tienda.
5. Admitir ofertas manuales/importadas con mercado, moneda, precio habitual,
   precio promocional, envío, disponibilidad, URL, fecha, método, estado de
   coincidencia y evidencia. Sólo `exact_match` participa como confirmado;
   candidatos dudosos quedan para revisión manual.
6. Calcular indicadores únicamente sobre ofertas vigentes y confirmadas.
   Diferencia porcentual: `(precio Vytria - mínimo competencia) /
   mínimo competencia * 100`; resultado positivo significa que Vytria es más
   caro. No comparar mercados o monedas incompatibles ni inventar conversiones.
   Los tipos de cambio deberán estar documentados y fechados antes de comparar
   monedas no EUR.
7. Exportar resultados a Excel y mostrar oferta/evidencia e histórico. Los
   umbrales de vigencia deben ser configurables, con fecha de captura visible.
8. Mantener la consulta automática desactivada. Reutilizar infraestructura de
   ejecución sólo después de validar feeds/APIs o métodos permitidos, dominios,
   límites y pruebas técnicas por competidor.

## Viabilidad y límites conocidos

**No está verificada la viabilidad automatizada de ninguno de los 14
competidores.** El Excel sólo aporta homepages; no se ha verificado API oficial,
feed comercial autorizado, datos estructurados utilizables, reglas de acceso,
condiciones del sitio, bloqueo por bots ni cobertura de EAN. Por tanto, en esta
fase sólo se puede recomendar importación manual de ofertas, sin scraping ni
pruebas de acceso. La evaluación futura debe documentarse por dominio y detener
cualquier método que requiera eludir CAPTCHA, autenticación, controles antibot
o restricciones.

## Riesgos y decisiones pendientes

- Confirmar qué precio de Vytria se considera comparable (precio visible final,
  país, impuestos, descuentos, disponibilidad y gastos de envío) y cómo obtenerlo
  de forma autorizada. Hasta entonces, importación Excel.
- Confirmar mercado y moneda por competidor; no asumir que la URL española
  determina todos los productos o el mercado aplicable.
- Establecer qué antigüedad convierte una oferta en caducada y cómo se resuelven
  precios ambiguos, variantes de talla/color y páginas que omiten el EAN.
- Obtener revisión de términos, privacidad y límites de consulta antes de
  habilitar cualquier automatización externa.
- No desplegar ni activar programación en producción como parte del desarrollo.

## Orden de trabajo recomendado

1. Modelo de datos SQLite aislado, importación del libro, normalización GTIN,
   validación y pruebas de duplicados/idempotencia.
2. Catálogo de competidores y cargas manuales de precio propio/ofertas; estados
   de coincidencia, trazabilidad e histórico.
3. Comparación, filtros, panel, ficha de producto y exportación Excel; pruebas
   de moneda, promociones, expiración y datos incompletos.
4. Revisión independiente de viabilidad de 2–3 dominios y método autorizado;
   sólo entonces prototipo automatizado en fixtures y entorno no productivo.
5. Ampliación gradual, programación apagada por defecto, revisión de despliegue
   y aprobación explícita antes de cualquier activación.

## MVP implementado en la rama de trabajo

- Página Streamlit bajo **Análisis comercial**, con importación del libro, carga
  separada de precios Vytria y ofertas, configuración manual de mercado/moneda,
  panel filtrable, detalle e histórico, y exportación XLSX.
- Base SQLite aislada (`PRICE_COMPARATOR_DB_PATH`); Compose apunta por defecto a
  `/app/data/price_comparator.sqlite3`, dentro del volumen persistente existente.
- La importación real del libro piloto quedó comprobada en una base temporal:
  100 productos y 14 competidores importados, cero rechazados; se preservan los
  53 UPC originales de 12 dígitos y todos los identificadores normalizados son
  EAN-13. El archivo fuente no se modifica.
- Las consultas externas, el scheduler, las notificaciones y el scraping están
  ausentes y desactivados. La interfaz sólo acepta ofertas aportadas mediante
  plantilla y no marca el estado de conectividad como verificado por red.
- No se ha configurado mercado/moneda por competidor ni se ha confirmado el
  precio de tienda de Vytria; ambas tareas requieren datos/validación del
  negocio. Las conversiones fuera de EUR requieren un tipo de cambio
  proporcionado y trazable por fila.


## Incidencia P0: ruta SQLite en producción (hotfix)

**Síntoma.** Tras el despliegue de #85 la página falla en
`PriceComparatorStore.__init__` (`mkdir` de `/app/masterdata_data`, inexistente y
no escribible: `/app` es root y la app corre como `1037:100`).

**Causa raíz confirmada.** `PRICE_COMPARATOR_DB_PATH` sólo estaba en
`docker-compose.yml` del repositorio. El controlador de producción no lo usa: lee
la instantánea aprobada `/etc/cloud-production/compose.private.json` y sustituye
únicamente la imagen de `abcd-app`. La variable nunca llegó al contenedor y el
código caía en `masterdata_data` junto al módulo.

**Corrección.** `resolve_database_path()` aplica, por orden:
`PRICE_COMPARATOR_DB_PATH` → `/app/data/price_comparator.sqlite3` si `/app/data`
existe → ruta de desarrollo local. No se crean directorios bajo `/app`. Si la
ruta no es escribible se lanza `PriceComparatorStorageError` y la página muestra
un error comprensible, sin traceback. No se cambian permisos ni otras bases.

**Configuración productiva.** Con este código no se requiere editar
`compose.private.json`. Opcionalmente, para dejarla explícita, el administrador
puede añadir sólo `PRICE_COMPARATOR_DB_PATH=/app/data/price_comparator.sqlite3` al
entorno de `abcd-app` en una revisión aprobada y ejecutar
`verify_compose_transition.py`; no modificar otros servicios ni variables.

**Backup (SQLite con WAL).** No copiar sólo el `.sqlite3` con la app activa. Usar
la API de copia en línea, dentro del contenedor, hacia el volumen y luego
verificar:

```sh
docker exec abcd-control python -c "import sqlite3; s=sqlite3.connect('file:/app/data/price_comparator.sqlite3?mode=ro',uri=True); d=sqlite3.connect('/app/data/price_comparator.backup.sqlite3'); s.backup(d); print(d.execute('PRAGMA integrity_check').fetchone()[0])"
```

Restaurar siempre primero en una ubicación temporal y comprobar
`PRAGMA integrity_check` y el recuento de tablas antes de sustituir nada. Incluir
`price_comparator.sqlite3` (y su copia consistente) en el procedimiento de
backup del volumen `/app/data`; que esté en el volumen no equivale a estar
respaldado.

**Validación.** Local: 840 tests pasan, 15 omitidos (el de UID/GID 1037:100
requiere root y Linux). Docker/producción: pendientes de CI, digest y despliegue
aprobado; no se declaran resueltos hasta verificarlos.

## Sincronización automática del precio Vytria

Véase [VYTRIA_PRICE_SYNC.md](VYTRIA_PRICE_SYNC.md): el PVP se lee de Odoo (tarifa 59), en solo lectura; la importación Excel queda como contingencia.

