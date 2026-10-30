# Comparador comercial: competencia, planificador y gráficos

Estado: implementado en la rama `feat/price-comparator-commercial`. **No hay servicio Docker nuevo ni
planificador activado.** La integración con Odoo (PR #88) no se ha modificado salvo que su frecuencia es
ahora configurable.

## Pantalla única

`Comparador de precios` muestra un encabezado fijo de indicadores y cinco pestañas. Todo se calcula con
datos almacenados; nunca consulta ninguna fuente al abrirse. Si un indicador no puede calcularse muestra
«Sin datos» (nunca 0 %).

- **Encabezado**: referencias con precio Vytria, con competencia comparable, competidores con datos
  (x/14), diferencia media del mercado frente a Vytria y referencias donde Vytria es más caro / más barato.
- **Resumen**: gráfico de barras con la diferencia media de cada competidor frente a Vytria
  (`(competidor − Vytria) / Vytria`; negativo = competidor más barato), solo con las mismas referencias
  exactas, disponibles, vigentes, con IVA y del mismo mercado; tabla con precio medio, referencias
  comparadas, % más barato, última actualización y señal (umbral configurable); mayores diferencias y
  evolución global.
- **Comparativa**: tabla con filtros (marca, competidor, disponibilidad, posición), exportación Excel y
  referencias que requieren atención.
- **Histórico**: selector de referencia, periodo y competidores; línea escalonada con puntos de cambio y
  comprobación, Vytria en verde corporativo (#417B7B) y paleta apta para daltonismo; una sola observación
  se dibuja como punto, sin línea.
- **Competidores**: los 14 con enlace a su web, estado, método, restricciones, referencias localizadas,
  ofertas confirmadas y comparables, última comprobación y, por competidor, **qué necesitamos de Vytria**
  (datos en `competitor_requirements.py`). Ninguno figura como operativo sin conector verificado y una
  oferta confirmada.
- **Configuración**: retención, caducidad, umbral, frecuencia de Odoo, configuración individual de cada
  competidor e importación de catálogo, precios Vytria y ofertas.

Al abrir la página por primera vez se guarda la configuración inicial (`seed_defaults`, idempotente, nunca
sobrescribe): los 14 competidores (mercado y moneda vacíos hasta verificarlos; ES/EUR es solo el mercado
objetivo de la comparación), retención 12 meses, caducidad 7 días, umbral 5 % y, por
competidor, 24 h, 20 peticiones, pausa 2 s y 3 reintentos, todos deshabilitados.

Botones superiores: **Actualizar precios Vytria** (Odoo, en hilo) y **Actualizar competencia** (solo
competidores habilitados, en hilo). Ninguno bloquea Streamlit.

## Reglas de datos

- Solo precios Vytria `validated` entran en la comparación. Competencia: ofertas `exact_match`, `available`,
  con **IVA incluido confirmado** (`price_includes_tax = 1`; NULL o 0 quedan fuera), mismo mercado que el
  precio Vytria, tipo de cambio a EUR y comprobadas dentro del plazo de caducidad (7 días por defecto,
  configurable 1–90). Solo cuenta la última observación de cada EAN/competidor/mercado: una posterior sin
  stock o con IVA no confirmado no rescata un precio antiguo. Las ofertas excluidas se conservan (tabla de la
  pestaña Histórico, columna «Uso en comparación»).
- Estados del precio: **vigente** (≤ caducidad), **último conocido** (≤ 2 × caducidad, visible, no
  comparable), **caducado** (más antiguo).
- Cada cambio de precio crea una oferta nueva (histórico). Una comprobación sin cambio solo actualiza
  `offer_checks.last_ok_at` y `checks`: no duplica filas. Fechas en UTC.
- La purga por retención conserva siempre la última oferta por EAN/competidor/mercado y no toca el histórico
  de Odoo.

## Conectores

Solo existe `feed_csv` (feed/CSV autorizado, HTTPS o fichero en `competitor_feeds/` del volumen de datos;
columnas `ean, precio, moneda, disponibilidad, url`, opcionales `precio promocional, precio anterior,
coste envío, mercado`). Rechaza URL con credenciales o tokens, direcciones internas y redirecciones, y limita
el tamaño. **No hay scraping ni conectores simulados.** Un competidor sin conector figura como «Pendiente de
configurar» y no puede habilitarse; para habilitarlo hace falta nota de autorización y verificación correcta.

### Verificación de dominios (portada, una petición, sin insistir)

200: optica-optima.com, blickers.com/es-es, edel-optics.es, amevista.com/es, miaburton.com/es,
domanci.com/es, opticauniversitaria.es, congafasdesol.com, visiofactory.es, otticait.com/es.
429 (protección anti-bot; no se insistió y no se evalúa ninguna consulta automática): opticabassol.com,
visual-click.com, opticalh.com, celine-opticien-lunetier.com/es.
Ninguno ha demostrado disponer de API o feed autorizado: eso debe solicitarse a cada proveedor.

## Planificador (comando independiente)

Guía operativa completa (ejecutar, verificar, detener, recuperar): [PLANIFICADOR_COMPETIDORES.md](PLANIFICADOR_COMPETIDORES.md).

Búsqueda por EAN, modelo y color, importación JSON y cobertura de competidores: [BUSQUEDA_POR_REFERENCIA.md](BUSQUEDA_POR_REFERENCIA.md).
Resumen:

Tablas en el SQLite persistente: `competitor_schedule`, `scheduler_jobs` (cola persistente, un job por EAN),
`scheduler_runs`, `offer_checks`, `comparator_settings`.

Ejecutar (desde el contenedor actual, no hay disparador configurado):

```
python price_scheduler.py --status ; python price_scheduler.py --runs 20
python price_scheduler.py --plan <id>
python price_scheduler.py --configure <id> --connector feed_csv --source <fichero|URL> --interval-hours 12
python price_scheduler.py --verify <id> ; python price_scheduler.py --enable <id>
python price_scheduler.py --once [--competitor <id>] [--workers N] [--force] [--with-odoo]
python price_scheduler.py --loop --tick-seconds 300
```

Funcionamiento: cada competidor tiene intervalo en horas, peticiones por ejecución, pausa, reintentos. Al
vencer el intervalo se encola un job por EAN repartiendo los lotes en el 80 % del intervalo (más productos
→ más espaciado). Una «petición» es una llamada al conector con un lote. Un fallo transitorio aplica backoff
y, tras los reintentos, el job pasa a `error`. Un bloqueo (HTTP 401/403/407/429) **suspende** el conector con
motivo y deja los jobs pendientes. Un solo proceso por competidor (bloqueo de fichero).

- **Verificar**: `--status`; pestaña Configuración; últimas ejecuciones en `scheduler_runs`.
- **Detener**: `--pause <id>` / `--disable <id>`; para `--loop`, SIGTERM/SIGINT (termina la pasada actual).
- **Recuperar tras reinicio**: relanzar `--once`/`--loop`; los jobs `running` huérfanos vuelven a `pending`
  y la cola continúa. Tras una suspensión: revisar la incidencia y `--resume <id>`.
- **Activarlo en producción** requiere aprobación administrativa (cron/servicio Compose externo); no se incluye.

## Próximas fases

Conectores por competidor cuando haya feed/API autorizado, alertas por correo, comparación con PrestaShop
(pendiente), sustitución del hilo de la sincronización Odoo por ejecución independiente.
