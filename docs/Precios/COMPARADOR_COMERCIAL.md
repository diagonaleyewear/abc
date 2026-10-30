# Comparador comercial: competencia, planificador y gráficos

Estado: implementado en la rama `feat/price-comparator-commercial`. **No hay servicio Docker nuevo ni
planificador activado.** La integración con Odoo (PR #88) no se ha modificado salvo que su frecuencia es
ahora configurable.

## Pantalla única

`Comparador de precios` tiene tres pestañas:

- **Comparación**: indicadores (Vytria confirmado, competencia comparable, % con el mejor precio, cambios
  importantes en 7 días), tabla con filtros (marca, competidor, disponibilidad, posición), exportación
  Excel, ficha por referencia con gráfico interactivo (7, 30, 90 días y 12 meses), evolución global de
  competitividad y mayores diferencias. Solo lee datos almacenados; se ejecuta sin consultar ninguna fuente.
  Si no hay ofertas muestra «No hay datos de competencia suficientes» y remite a la importación.
- **Datos y fuentes**: catálogo, bloque Odoo (sin cambios), importación de ofertas por Excel y competidores.
- **Configuración**: retención (3/6/12/24 meses), caducidad, umbral de cambio importante, frecuencia de Odoo
  y configuración individual de cada competidor.

Botones superiores: **Actualizar precios Vytria** (Odoo, en hilo) y **Actualizar competencia** (solo
competidores habilitados, en hilo). Ninguno bloquea Streamlit.

## Reglas de datos

- Solo precios Vytria `validated` entran en la comparación. Competencia: ofertas `exact_match`, `available`,
  mismo mercado y comprobadas dentro del plazo de caducidad (7 días por defecto, configurable 1–90).
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
