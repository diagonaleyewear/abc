# Piloto de precios: 20 productos y seis competidores

## Muestra real propuesta

[Los 20 EAN](price-pilot-20.csv) proceden de la exportacion real de Cloud
`2026-10-31T10-47_export.csv` facilitada por el usuario. Se comprobaron todos
contra las 100 filas: marca, modelo, color y precio Vytria positivo con estado
`validated`. No se crean ni importan productos al pulsar la propuesta.

La muestra incluye las tres Prada exigidas, diez marcas, precios de 82 a
1100 EUR, monturas y gafas de sol, y parejas de variantes de color de Gucci,
Miu Miu, Saint Laurent y Tom Ford. **Calibre pendiente**: ni la exportacion
ni el Excel original disponible incluyen calibre. El usuario ha autorizado
continuar documentando esta limitacion. No se inventan medidas. Con la regla
actual, un EAN publicado identico y verificado hace que el calibre sea
informativo; no bloquea si falta o difiere. Marca, modelo y codigo de color
siguen siendo obligatorios. Sin EAN verificado se mantiene la validacion
dimensional estricta. Los precios del CSV
son evidencia de seleccion, no precios eternamente validos: al guardar y
ejecutar se verifica de nuevo su vigencia en la base de Cloud.

## Alcance y plan

Solo Visio Factory, Optica Optima, Domanci, Amevista, Edel-Optics y
ConGafasDeSol; se conservan los ocho competidores restantes sin cambiar su
configuracion. Exactamente 20 EAN y los seis competidores forman una
configuracion SQLite persistente. Sin configuracion previa, Cloud conserva
su funcionamiento habitual. Configurar el piloto instala una barrera que
bloquea las actualizaciones generales y las colas heredadas, incluso al
desactivar el piloto.

El plan muestra 120 combinaciones, fichas conocidas y condiciones de acceso.
No promete 120 consultas: sin URL registrada no hay peticion ni descubrimiento
por sitemap, buscador interno o servicio externo. Se registra
`Ficha no localizada`. Sin condiciones revisadas y anotadas, o con acceso
no verificado, se registra falta de autorizacion sin HTTP.

Valores iniciales editables por tienda: pausa de 3 segundos y presupuesto
de 10 peticiones HTTP. **No representan permiso de las tiendas**: se deben
ajustar a sus condiciones de acceso antes de confirmar. Los presupuestos
incluyen robots.txt y redirecciones, se reservan antes de HTTP y sobreviven
a interrupciones. Concurrencia 1 y cero reintentos en el transporte;
403/429/CAPTCHA bloquean el resto de consultas de esa tienda.
Robots.txt es una comprobacion adicional, nunca autorizacion suficiente.

La localizacion supervisada es una operacion manual separada del piloto
comercial: cola/bloqueos/presupuestos comunes, solo combinaciones pendientes,
candidatos con revision humana, nunca ofertas ni ampliacion del alcance.
Ver [mercado y localizador](PRICE_PILOT_DISCOVERY.md). La consulta comercial
de precios sigue reutilizando exclusivamente URLs guardadas.

## Activacion manual

1. Sin ejecutar descargas generales, comprobar en Configuracion las
   condiciones y permisos de cada tienda, moneda y mercado. Verificar su
   acceso desde Configuracion web, incluso con el piloto ya guardado o
   desactivado. La comprobacion explicita solo lee robots.txt del competidor
   registrado; no desbloquea consultas comerciales. No activar programaciones.
   Ver [aislamiento y comprobacion](PRICE_ROBOTS_CONFIGURATION.md).
2. En **Administracion > Competidores y ejecuciones > Piloto de comparacion**,
   revisar el catalogo y precios
   vigentes. Proponer la muestra o seleccionar los 20 EAN, incluyendo las
   tres Prada. Si falta un dato requerido, Cloud rechaza el guardado indicando
   el EAN; no lo reemplaza silenciosamente.
3. Seleccionar las seis tiendas y establecer pausas/presupuestos autorizados.
   **Guardar configuracion** deja el piloto desactivado; cancela las colas
   generales pendientes/con arrendamiento, conservando su evidencia.
4. Revisar el plan persistido, los EAN y productos excluidos (80 para el
   catalogo inicial de 100). Marcar la confirmacion supervisada y pulsar
   **Ejecutar prueba manual**. No se activa ningun planificador ni Odoo.
5. Revisar resultados, motivos de exclusion, contadores, datos comerciales
   y exportacion CSV. Una ejecucion interrumpida solo se reanuda con el mismo
   alcance y presupuestos; usar **Reanudar prueba interrumpida**.

## Flujo comercial en Cloud

La navegacion principal del comparador es **Resumen**, **Productos**,
**Pendientes** y **Administracion**. Los filtros de marca, competidor, estado,
periodo y busqueda se comparten entre Resumen y Productos. El detalle del
producto concentra ofertas, vigencia e historico.

Desde un producto incluido en el piloto, **Actualizar precios** presenta las
fichas confirmadas y autorizadas, permite elegir competidores y requiere una
confirmacion humana antes de encolar exclusivamente esos pares. La
actualizacion no vuelve a descubrir URLs, no consulta al cargar la pagina y
mantiene la barrera, autorizaciones, robots y presupuesto del piloto. Si falta
una ficha, **Localizar ficha** es una operacion distinta; las candidaturas
resultantes se revisan desde **Pendientes**. Los reintentos, ejecuciones,
configuracion web, importaciones y diagnosticos siguen en **Administracion**.

El CLI `price_scheduler.py --once` tambien respeta la barrera y usa la cola
dedicada cuando hay piloto habilitado. No usarlo como activacion inicial:
la interfaz muestra el plan y exige confirmacion. `--force` no elimina
la barrera, ni los presupuestos. `--loop` se rechaza con piloto configurado;
los triggers periodicos no ejecutan consultas, aunque existiesen ajustes
de programacion antiguos habilitados.

## Observaciones e historico

Cada trabajo guarda la observacion original y su motivo, incluso si no es
comparable. Se exige EAN publicado, marca, modelo y codigo de color coincidentes,
precio positivo, EUR, evidencia de mercado ES, IVA incluido confirmado y
stock disponible. Una incoherencia con mercado/moneda configurados tambien
excluye. El calibre vacio o diferente es informativo con EAN publicado
identico verificado y se registra para auditoria; nunca se actualiza el
catalogo automaticamente. Los precios extraidos se importan con las funciones existentes;
identidades o mercados no verificados quedan en revision, nunca comparables.
Las importaciones rechazadas permanecen visibles en el resultado del piloto.

No se duplica una oferta con contenido comercial sin cambios. Los errores
temporales no borran ofertas ni su historico. La diferencia frente a Vytria
solo se calcula para ofertas comparables. Los resultados incluyen peticiones
HTTP, productos/tiendas consultados, fichas, precios, comparables, sin stock,
IVA sin confirmar, bloqueos y ultima consulta con precio extraido.

## Desactivacion y rollback

**Desactivar piloto** bloquea la siguiente frontera HTTP, incluida una
ejecucion en curso. Una peticion ya enviada no se puede retirar. Se conservan
el alcance, presupuestos, ofertas, errores e historico; desactivar no libera
los otros 80 EAN. Para cambiar la muestra, esperar que terminen los workers
y guardar una nueva seleccion validada: las colas anteriores se cancelan.

Rollback de codigo: desactivar primero, detener exclusivamente los workers
del comparador, verificar ausencia de ejecuciones y respaldar SQLite mediante
la herramienta de backup existente. Revertir la PR solo cuando no haya
workers activos; codigo anterior no conoce la barrera del piloto. Revisar y
mantener deshabilitadas las programaciones antes de arrancar una version
anterior. No borrar tablas/historicos ni retirar el alcance mientras haya
workers. No se autoriza merge, despliegue ni prueba contra tiendas reales
por esta entrega.

## Validacion

`python -m pytest tests/test_price_pilot.py tests/test_price_scheduler.py
tests/test_price_web.py tests/test_price_settings_ui.py -q`

Las pruebas usan bases temporales de 100 productos y transporte HTTP
simulado: nunca tiendas reales. Incluyen alcance/persistencia, colas
heredadas, ejecucion forzada/manual, recuperacion, desactivacion durante HTTP,
presupuesto persistente, bloqueos sin reintento, variantes/IVA/mercado/moneda,
historico, integridad SQLite y confirmacion en la interfaz Streamlit.
La suite completa y el build Docker se deben comprobar antes del merge.

Validacion focalizada: **93 tests pasaron**. Incluye seis tiendas autorizadas
simuladas, bloqueo de la siguiente peticion si se retira la autorizacion,
worker de interfaz con errores persistidos y trabajos antiguos inyectados
que se cancelan al reanudar. No se ha realizado ninguna consulta a tiendas
reales ni guardado/activado la seleccion en la base de produccion.

Suite completa final: **1176 tests pasaron, 15 omitidos y 1 fallo** en
`test_local_library_search_and_download_permissions`, validacion de
fotografias de Kering. El mismo fallo ya se habia observado antes del piloto;
no se ha modificado su logica ni su test. No se presenta la suite como verde.
Compilacion Python y `git diff --check`: correctos.

Docker/CI: **pendientes**, no verificados. Docker/Podman no estan disponibles
en este equipo. La rama se publica sin merge; crear la PR ejecuta el workflow
existente, que construye y carga la imagen sin publicarla/desplegarla en eventos
`pull_request`. El build depende de que el job de tests pase. No activar
despliegues para sustituir esta validacion.

## Archivos modificados

- [price_pilot.py](../price_pilot.py): alcance, plan, cola manual, bloqueo HTTP,
  presupuestos, verificacion comercial y resultados.
- [price_comparator.py](../price_comparator.py): tablas nuevas y migracion
  idempotente; almacenamiento de ofertas e historicos existente reutilizado.
- [price_scheduler.py](../price_scheduler.py): aislamiento de colas generales,
  triggers, concurrencia y ejecuciones forzadas.
- [price_web.py](../price_web.py): barrera de EAN/competidor y fichas conocidas
  sin descubrimiento general cuando hay piloto.
- [app_pages/price_comparator.py](../app_pages/price_comparator.py): panel del
  piloto, confirmacion, progreso, desactivacion y resultados.
- [Dockerfile](../Dockerfile) y [CI](../.github/workflows/deploy.yml):
  empaquetado del modulo y comprobacion de imports en la imagen.
- [tests/test_price_pilot.py](../tests/test_price_pilot.py): pruebas offline
  con SQLite temporal, HTTP simulado y Streamlit AppTest.
- Esta guia y [la muestra CSV](price-pilot-20.csv).
