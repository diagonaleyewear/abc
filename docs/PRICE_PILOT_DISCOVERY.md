# Mercado comercial verificado y localizacion supervisada

Base de esta entrega: main `77ab8bd`, con PR #95, #96 y #97.
No se han consultado competidores ni leido/modificado SQLite de produccion.
Las cifras 6/114 proceden del contexto aportado; las pruebas reproducen esa
cobertura offline. No se afirma haber localizado realmente las 114 fichas.

## Causas y separacion de responsabilidades

El piloto de precios solo utilizaba el indice de URLs conocidas, por diseno.
No habia una cola de descubrimiento autorizada ni revision de candidatos.
Ademas, el extractor interpretaba el idioma/regionalizacion como mercado,
pero una web en espanol no demuestra que venda en Espana.

La localizacion reutiliza `classify`, el extractor, robots por ruta y la misma
cola/barrera/bloqueo y presupuesto persistente del piloto. La identidad exacta
es EAN sin contradicciones explicitas o, si no hay EAN, marca + modelo +
codigo de color. El calibre no participa en la identidad. Es una operacion manual separada
(`pilot_runs.operation=discovery`), no una excepcion a las consultas generales.
La ejecucion comercial sigue usando solo el indice y registra ofertas con
el importador existente.

## Mercado de venta

En Configuracion web de cualquier competidor:

- Mercado sin comprobar, Espana ES u otro codigo ISO-2.
- Verificacion pendiente o confirmada manualmente.
- URL HTTPS de condiciones/entrega del dominio registrado.
- Nota de quien/cuando verifico; fecha de confirmacion persistida.

La configuracion se guarda en `competitor_web_config` (`sales_market`,
`market_verified`, `market_evidence_url`, `market_note`, `market_verified_at`).
No se rellenan productos ni se reescriben los valores antiguos de
`competitors.market`; un valor antiguo sin evidencia no concede el nuevo
fallback. Las importaciones no-web conservan sus reglas existentes.

Fuentes explicitas soportadas:

- JSON-LD Offer `eligibleRegion` ISO-2.
- JSON-LD Offer `shippingDetails.shippingDestination.addressCountry` ISO-2.
- Metadato `product:market` ISO-2.

No se usan `.es`, `/es/`, `html lang` ni el locale PrestaShop como prueba.
Si solo hay un mercado comercial explicito, se usa. Si falta, se permite
el configurado con verificacion, URL y nota completas. Si hay contradiccion
entre fuentes explicitas o con la configuracion verificada, se requiere
revision; no es comparable. Multiples mercados publicados quedan en revision
conservadora, sin elegir arbitrariamente uno.

`resolve_market` es compartido por comparador web, piloto e importacion del
conector web/planificador. La observacion registra origen/evidencia y conflicto.
Un mercado desconocido se guarda como `UNKNOWN` cuando el importador puede
registrarlo, sin entrar en comparativas; contradicciones se marcan
`manual_review`, no exactas. Los requisitos de EAN/identidad, stock, precio
positivo, EUR e IVA permanecen.

Confirmar configuracion no recalcula ni modifica ofertas historicas: en la
siguiente ejecucion manual se extrae una observacion nueva con el mecanismo
existente de deduplicacion. No se autoactivan procesos.

## Busqueda configurada por tienda

- Automatico: preferente EAN o modelo/color; alternativas explicitamente
  autorizadas, en orden, sin repetir metodos ni candidatos.
- EAN: termino exacto del catalogo.
- Modelo/color: marca + modelo de fabricante normalizado + codigo de color.
  Ejemplo `PRADA PR 27ZS 16K08Z`, no `Negro`.
- Solo URLs conocidas: no inicia descubrimiento.

Las plantillas publicas HTTPS GET admiten solo `{ean}`, `{model}`, `{color}`,
`{query}`, en ruta/consulta, nunca host. Se rechazan formatos, conversiones,
acceso a atributos, variables desconocidas, credenciales/puertos/dominios
ajenos. Valores escapados y URL generada validada antes de HTTP.
`{query}` permite ambos terminos; `{ean}` no se usa para modelo/color ni
plantillas de modelo/color para busquedas EAN.

Una casilla y evidencia humana deben confirmar el mecanismo publico GET y
su autorizacion. La migracion NO convierte plantillas antiguas en mecanismos
verificados. Sitemap publico debe estar habilitado y ser alternativa
autorizada en descubrimiento automatico. Nunca se invoca busqueda externa.

### Edel-Optics: tarjetas de resultados

La extraccion reconoce cada contenedor `eoViewsListviewItemClassic` como una
tarjeta independiente. Conserva su URL de producto, marca, modelo, codigo de
color, precio y moneda, y guarda por separado la URL de busqueda como fuente.
Una pagina de resultados con varias tarjetas no se interpreta como una ficha
unica ni sus metadatos/precios globales se asignan al EAN consultado.

La fixture sanitizada aportada contiene Prada PR 65ZS/ZVN09T a 187,99 EUR,
PR 27ZS/16K08Z a 243,50 EUR y PR 67ZS/1BC5W1 a 243,50 EUR. Para el EAN
`8056597877329`, solo la tarjeta PR 65ZS/ZVN09T corresponde a la referencia;
las otras dos no se mezclan ni se visitan. El fragmento no publica EAN ni
disponibilidad: el EAN queda vacio y el stock `unknown`. Aunque incluye un
`<p class="textPoint"></p>` vacio por tarjeta, la regla CSS recibida solo
declara `color: green`; no incluye la regla que genera el punto ni una
variacion HTML que codifique su estado. Por tanto, ni la clase vacia ni ese
fragmento CSS prueban stock estatico por tarjeta. El extractor solo acepta
disponibilidad explicita dentro de la tarjeta (por ejemplo, Schema.org
`InStock`/`OutOfStock`); no hereda indicadores de la pagina ni de otras
tarjetas. Para el HTML recibido conserva `unknown`, que no permite una oferta
comparable. El descubrimiento solo guarda el candidato y su evidencia; no crea
ofertas. Una URL solo se confirma tras aprobacion humana. Con EAN publicado
contradictorio, la tarjeta queda pendiente de revision y no puede aprobarse
como coincidencia exacta.

La prueba usa exclusivamente la fixture HTML y transporte simulado. No se ha
consultado el sitio en directo ni se declara verificado el mecanismo GET. El
estado de acceso registrado sigue bloqueado por el 403 documentado; no se
reintenta ni elude. No se inventan endpoints AJAX ni plantillas operativas.
HTML dependiente de AJAX/JavaScript sin tarjetas o enlaces legibles se informa
para revision, sin consultar endpoints. No hay navegador automatico,
proveedores externos, proxies, rotacion ni elusion de CAPTCHA.

## Localizacion de fichas del piloto

La interfaz comercial concentra las fichas que requieren revision en
«Pendientes»: cada elemento muestra primero marca, modelo, color, competidor
y URL, con acciones de confirmar/descartar junto al candidato. El JSON,
identificador interno, metodo y ejecucion se muestran solo en «Detalles
tecnicos». «Administracion > Competidores y ejecuciones» conserva la matriz
20x6, el alcance, presupuestos, filtros, colas, resultados y auditoria. Los
filtros de cobertura son solo visuales y nunca cambian la cola.

Estados:

- Ficha conocida: indice ya guardado (incluye URLs aportadas pendientes).
- Pendiente de localizar: no hay URL ni intento terminado.
- Candidato encontrado: EAN exacto o marca/modelo/codigo de color exactos;
  aun SIN aprobacion humana.
- Pendiente de validar: datos incompletos/ambiguos; no puede confirmarse.
- Ficha confirmada: aprobada por una persona.
- Producto no encontrado: solo cuando la consulta no devuelve candidatos y no
  existe una ficha confirmada ni candidatos abiertos.
- Consulta no autorizada: metodo/acceso/condiciones no preparados.
- Error o bloqueo: HTTP, presupuesto, CAPTCHA o mecanismo incompatible.

Antes de iniciar, el selector de alcance permite:

- Una referencia y un competidor: un EAN y una tienda.
- Varios productos de un competidor: varios EAN y una tienda.
- Todos los pendientes: todas las combinaciones pendientes del piloto; exige
  una confirmacion separada que indica el numero de trabajos.

La vista previa no realiza HTTP ni escribe en la base. Muestra combinaciones
seleccionadas, trabajos autorizados realmente pendientes, URLs conocidas y
candidatos pendientes excluidos, combinaciones no autorizadas, metodo
configurado y presupuesto. Una seleccion fuera de los 20 EAN/seis tiendas se
rechaza. Solo el conjunto efectivo autorizado se guarda y encola. Por ejemplo,
Edel-Optics mas `8056597875226` crea como maximo un trabajo; ningun otro EAN o
competidor se consulta.

Al iniciar:

1. Valida la seleccion contra los 20 EAN y seis tiendas persistidos.
2. Encola exclusivamente las combinaciones solicitadas que aun esten
   pendientes y autorizadas; excluye conocidas y candidatos sin revision.
3. Comprueba condiciones revisadas con nota, robots permitido y metodo
   autorizado. Mantiene bloqueo global del piloto y por competidor.
4. Reutiliza presupuestos persistentes, pausa, timeout, robots y validacion
   de dominio en cada frontera HTTP. Robots/busquedas/fichas consumen
   presupuesto antes de I/O. Sin reintentos ni destinos/redirecciones ajenos.
5. Obtiene como maximo tres candidatos por combinacion, verifica con
   `classify` y conserva evidencia, contradicciones y consultas.
6. Guarda candidatos, nunca ofertas ni URLs definitivas automaticamente.

La aprobacion exige nota/revisor, vuelve a contrastar la identidad con el
catalogo actual y requiere ausencia de ambiguedad. Puede aprobarse por EAN
exacto sin contradicciones o por marca/modelo/codigo de color exactos cuando
la ficha no publica EAN. Un EAN contradictorio o atributos insuficientes NO
pueden aprobarse como coincidencia exacta.
Solo guarda URL/identidad en `competitor_product_urls`; no guarda precio.
El comparador volvera a extraer y validar datos comerciales.

Descartar registra el estado, fecha y nota en el candidato individual. No
marca el producto como inexistente ni altera una URL confirmada de la misma
combinacion EAN/competidor. La cobertura agregada conserva `confirmed` si hay
una ficha validada; si quedan candidatos abiertos, refleja su estado; si se
descarta el ultimo candidato, vuelve a `pending` para permitir otra
localizacion, no a `not_found`. Una revision repetida con la misma decision es
idempotente. Las URL descartadas no vuelven a aparecer como candidatos
pendientes en ejecuciones posteriores; si se vuelven a encontrar, se omiten.

En el detalle de producto, «Actualizar precios» solo se habilita para
competidores del alcance supervisado que tengan URL confirmada, autorizacion
registrada y acceso verificado. La vista previa no hace HTTP. El usuario elige
las tiendas y confirma; la cola persistida contiene solo esos pares
EAN/competidor y reutiliza presupuesto, pausas, robots y validaciones
comerciales existentes. La operacion de precios no inicia descubrimiento ni
crea ofertas duplicadas. Si falta una ficha, «Localizar ficha» inicia por
separado una localizacion supervisada y explicita; la revision de la URL sigue
en «Pendientes». No se consultan webs al cargar el comparador.

Las fichas conocidas/confirmadas y candidatos pendientes no se repiten en
nuevas ejecuciones de localizacion. Errores/no encontrado requieren preparar
un nuevo intento con motivo humano, sin HTTP. Un intento nuevo conserva
candidatos/revisiones antiguos; nunca borra evidencia.

## Interrupcion, bloqueo y presupuestos

Desactivar piloto detiene el siguiente HTTP; no puede retirar uno ya enviado.
Reanudar exige el mismo alcance/limites/operacion. Trabajos completados no se
repiten, solicitudes consumidas no se devuelven y la pausa se conserva.
Un candidato guardado antes de interrumpirse se reutiliza sin volver a visitar
la ficha. Si el proceso muere entre HTTP y guardar el candidato, su presupuesto
permanece consumido; no se promete idempotencia de una peticion ya enviada.

La seleccion solicitada y la cola efectiva se guardan en la fila de ejecucion.
Al reanudar, se recupera esa cola, se rechaza un alcance distinto y no se
recrean trabajos. Trabajos terminados/candidatos no se vuelven a consultar;
presupuestos e intentos permanecen asociados a la ejecucion.

Una cola interrumpida bloquea otras operaciones: reanudar la seleccion original
o cerrarla desde la interfaz con una nota. El cierre marca los trabajos
pendientes como cancelados y la ejecucion como cancelada, conservando
seleccion, candidatos, consultas, intentos y presupuestos; no elimina datos.
Guardar/cerrar no activa consultas generales.
Un presupuesto agotado no se recarga al reanudar; un nuevo intento requiere
revision explicita del usuario. Bloqueos anteriores no se levantan.
Planificador periodico continua desactivado; ni configuracion ni aprobacion
arrancan HTTP ni Odoo.

## Persistencia y migracion compatible

Migracion idempotente en el constructor, bajo el bloqueo SQLite existente:

- Columnas nuevas de mercado/estrategia en `competitor_web_config`.
- `pilot_runs.operation`, default `prices` para ejecuciones antiguas.
- `pilot_runs.selection` y `pilot_runs.selection_request`, con JSON de la cola
  efectiva y de la seleccion original; defaults vacios para ejecuciones antiguas.
- `pilot_discovery`: estado por combinacion, ultima operacion/metodo/motivo.
- `pilot_url_candidates`: evidencia y revision por intento/URL.
- `pilot_http_log`: URL, EAN, tienda, fecha, HTTP/error por peticion.

No hay conversion/borrado de ofertas ni productos ni precios Vytria, ni
cambios de identidad/calibre, seleccion o permisos. Los resultados de precios
siguen mostrando la ultima operacion `prices`, no una localizacion.
El audit UI muestra las ultimas 500 consultas; la base conserva todo el registro.

La correccion del parser de tarjetas no requiere una migracion adicional:
`source_url`, los campos extraidos y la evidencia se guardan en el JSON de
candidato existente. Los registros anteriores se leen con los valores por
defecto del modelo y no se reescriben.

## Procedimiento de uso (solo despues de autorizar despliegue)

1. Respaldar base y revisar alcance existente, sin cambiar los 20/seis.
2. En cada competidor, registrar mercado con evidencia y nota. Comprobar IVA
   y condiciones independientemente; robots permitido no autoriza busquedas.
3. Elegir estrategia, plantilla publica soportada y alternativas autorizadas;
   confirmar mecanismo con evidencia. Para AJAX no comprobado, no marcarlo
   como GET verificado y dejar pendiente.
4. En Localizacion seleccionar primero una tienda y uno o varios EAN,
   revisar la vista previa y marcar aprobacion supervisada. Usar "Todos los
   pendientes" solo despues de confirmar el total mostrado.
5. Revisar cada candidato. Confirmar solo exactos completos; descartar dudas.
6. Reanudar la cola original o cerrarla con nota; errores/bloqueos necesitan
   revision y motivo antes de preparar un nuevo intento.
7. Ejecutar piloto comercial manual para las URLs guardadas; comprobar
   identidad/comercio y las nuevas ofertas, sin modificar las anteriores.

## Validacion

[Tests offline](../tests/test_price_discovery.py): fuentes y conflictos de
mercado, fallback verificado y deduplicacion; 6/114 y 20x6; alcance selectivo,
vista previa sin HTTP, reanudacion inmutable y cierre no destructivo;
EAN/modelo/color;
automatico y alternativas autorizadas; sitemap; sugerencias Edel-Optics;
AJAX sin endpoints; candidatos y aprobacion; contradicciones/ambiguedad;
reanudacion/budget/bloqueos/robots/redirecciones; retirada de autorizacion;
integridad SQLite/migracion; precios/historico/alcance intactos; UI offline.

Ejecutar suite de comparador, suite completa CI y build Docker. Resultados
exactos y enlace al CI se registran en la PR independiente de esta entrega.
No se autoriza merge ni despliegue por el hecho de que los tests pasen.

Validacion local de PR #98 (historica):

- Suite completa del comparador: **432 passed, 1 skipped**.
- Validacion final del piloto/localizador: **87 passed**.
- Comando completo del CI: **1339 passed, 16 skipped, 10 subtests passed,
  1 failed**. Fallo de fotografias:
  `test_kering_image_sections.py::test_local_library_search_and_download_permissions`,
  ya observado localmente en la entrega #97; ese componente no se modifica.
- Lint de errores e imports de los archivos nuevos/modificados, sintaxis
  `py_compile` y `git diff --check`: correctos.
- CI remoto y Docker pendientes al publicar este commit; sus resultados
  finales se documentan en la PR. Docker/Podman no disponibles localmente.

Validacion local de ejecucion selectiva:

- Regresiones de localizador/piloto: **92 passed**, incluidos selector de un
  EAN/competidor, seleccion multiple, preview sin escrituras/HTTP, caso
  Edel-Optics, reanudacion inmutable y cierre no destructivo.
- Suite CI completa: **1343 passed, 16 skipped, 10 subtests passed,
  1 failed**. El unico fallo es
  `test_kering_image_sections.py::test_local_library_search_and_download_permissions`,
  en validacion de fotografias Kering, fuera de los archivos modificados.
- `compileall` y `git diff --check`: correctos.
- Docker/Podman no estan instalados localmente. La build Docker y smoke test
  de la imagen quedan para el workflow de CI de esta PR.

Validacion de extraccion de tarjetas Edel-Optics en esta correccion:

- Suite completa `tests/test_price*.py`: **442 passed, 1 skipped**.
- Regresiones focalizadas de tarjetas, revision supervisada y conservacion de
  ofertas: **6 passed**.
- Lint CI de errores/imports, `py_compile`, diagnosticos Pylance y
  `git diff --check`: correctos.
- No se consulto Edel-Optics ni ningun competidor, no se leyo/escribio SQLite
  de produccion y no se activo ningun planificador.
- El workflow de PR debe ejecutar la suite CI y la build Docker; Docker no
  esta disponible localmente. El workflow de `pull_request` construye la
  imagen sin publicarla ni desplegarla.

## Riesgos, despliegue y rollback

Riesgos: buscadores que requieren JS, formatos de paginas cambiantes y
mercados multiples quedan pendientes; los limites pueden impedir completar
114 combinaciones en un intento. No se sustituyen por busquedas externas ni
se amplian presupuestos automaticamente. Una confirmacion antigua no elimina
la verificacion de identidad en la siguiente consulta comercial.

Para la correccion de tarjetas Edel-Optics, rollback significa revertir solo
el commit de codigo; no hay migracion nueva ni consultas o cambios de datos
de produccion que deshacer.

Despliegue autorizado: revisar CI/tests/build, detener workers, desactivar
piloto, esperar bloqueo libre, respaldar SQLite mediante backup existente,
desplegar imagen de la PR aprobada y arrancar sin programaciones.
La migracion agrega tablas/columnas, sin escribir hechos comerciales.
Comprobar cobertura/configuracion en lectura antes de cualquier HTTP.

Rollback autorizado: parar workers, desactivar piloto y revertir la imagen/
commit. Las columnas/tablas nuevas son aditivas y compatibles con el lector
anterior, pero este no comprende colas discovery ni el nuevo mercado: no
reanudar colas de localizacion con la version anterior. Revisar que no haya
colas interrumpidas antes del rollback. No eliminar ofertas ni tablas ni
restaurar una copia antigua sobre observaciones comerciales nuevas; usar
restauracion solo con aprobacion y respaldo consistentes.
