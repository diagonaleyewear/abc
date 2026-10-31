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

Se conserva la regla de identidad de PR #96, sin cambios en
[price_references.py](../price_references.py). La localizacion reutiliza
`classify`, el extractor, robots por ruta y la misma cola/barrera/bloqueo y
presupuesto persistente del piloto. Es una operacion manual separada
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

### Edel-Optics

El ejemplo EAN `8056597875226` -> sugerencia Prada PR 26ZS es compatible con
la estrategia EAN si se configura y verifica un mecanismo publico soportado.
Las fixtures prueban que una sugerencia sin EAN en el enlace puede ser un
candidato, pero NO confirma el EAN de la ficha.

No se ha investigado su transporte real mediante nuevas consultas. No se
inventan endpoints AJAX ni plantillas operativas. Un HTML dependiente de
AJAX/JavaScript sin enlaces legibles se informa como mecanismo pendiente de
revision, no como producto inexistente; los endpoints no se consultan.
Un adaptador especifico futuro requiere evidencia y autorizacion previa.
No hay navegador automatico, endpoints internos, proveedores externos,
proxies, rotacion ni elusion de CAPTCHA.

## Localizacion de fichas del piloto

La interfaz muestra matriz 20x6, cobertura, tabla filtrable EAN/competidor/
metodo/estado/URL y candidatos con evidencia y referencia normalizada.

Estados:

- Ficha conocida: indice ya guardado (incluye URLs aportadas pendientes).
- Pendiente de localizar: no hay URL ni intento terminado.
- Candidato encontrado: ficha con EAN publicado e identidad exacta, aun SIN
  aprobacion humana.
- Pendiente de validar: datos incompletos/ambiguos; no puede confirmarse.
- Ficha confirmada: aprobada por una persona.
- Producto no encontrado: consulta sin candidato valido (rechazos auditados).
- Consulta no autorizada: metodo/acceso/condiciones no preparados.
- Error o bloqueo: HTTP, presupuesto, CAPTCHA o mecanismo incompatible.

Al iniciar:

1. Lee exactamente los 20 EAN y seis tiendas persistidos.
2. Encola solo combinaciones pendientes (6 conocidas -> 114 pendientes).
3. Comprueba condiciones revisadas con nota, robots permitido y metodo
   autorizado. Mantiene bloqueo global del piloto y por competidor.
4. Reutiliza presupuestos persistentes, pausa, timeout, robots y validacion
   de dominio en cada frontera HTTP. Robots/busquedas/fichas consumen
   presupuesto antes de I/O. Sin reintentos ni destinos/redirecciones ajenos.
5. Obtiene como maximo tres candidatos por combinacion, verifica con
   `classify` y conserva evidencia, contradicciones y consultas.
6. Guarda candidatos, nunca ofertas ni URLs definitivas automaticamente.

La aprobacion es offline: exige nota/revisor, vuelve a contrastar la identidad
con el catalogo actual, EAN publicado exacto y ausencia de ambiguedad.
Una revision humana NO puede dispensar atributos ausentes ni contradicciones.
Solo guarda URL/identidad en `competitor_product_urls`; no guarda precio.
El comparador volvera a extraer y validar datos comerciales.

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

Una cola interrumpida bloquea otras operaciones: reanudar la correcta o,
con piloto desactivado, volver a guardar el mismo alcance para cerrar la cola
anterior conservando su evidencia. Guardar no activa consultas generales.
Un presupuesto agotado no se recarga al reanudar; un nuevo intento requiere
revision explicita del usuario. Bloqueos anteriores no se levantan.
Planificador periodico continua desactivado; ni configuracion ni aprobacion
arrancan HTTP ni Odoo.

## Persistencia y migracion compatible

Migracion idempotente en el constructor, bajo el bloqueo SQLite existente:

- Columnas nuevas de mercado/estrategia en `competitor_web_config`.
- `pilot_runs.operation`, default `prices` para ejecuciones antiguas.
- `pilot_discovery`: estado por combinacion, ultima operacion/metodo/motivo.
- `pilot_url_candidates`: evidencia y revision por intento/URL.
- `pilot_http_log`: URL, EAN, tienda, fecha, HTTP/error por peticion.

No hay conversion/borrado de ofertas ni productos ni precios Vytria, ni
cambios de identidad/calibre, seleccion o permisos. Los resultados de precios
siguen mostrando la ultima operacion `prices`, no una localizacion.
El audit UI muestra las ultimas 500 consultas; la base conserva todo el registro.

## Procedimiento de uso (solo despues de autorizar despliegue)

1. Respaldar base y revisar alcance existente, sin cambiar los 20/seis.
2. En cada competidor, registrar mercado con evidencia y nota. Comprobar IVA
   y condiciones independientemente; robots permitido no autoriza busquedas.
3. Elegir estrategia, plantilla publica soportada y alternativas autorizadas;
   confirmar mecanismo con evidencia. Para AJAX no comprobado, no marcarlo
   como GET verificado y dejar pendiente.
4. En Localizacion revisar 20x6, pendientes y limites; marcar aprobacion
   supervisada y localizar. No pulsar repetidamente la consulta de precios
   sobre las mismas seis fichas como sustituto del descubrimiento.
5. Revisar cada candidato. Confirmar solo exactos completos; descartar dudas.
6. Reanudar interrupciones; errores/bloqueos necesitan revision y motivos.
7. Ejecutar piloto comercial manual para las URLs guardadas; comprobar
   identidad/comercio y las nuevas ofertas, sin modificar las anteriores.

## Validacion

[Tests offline](../tests/test_price_discovery.py): fuentes y conflictos de
mercado, fallback verificado y deduplicacion; 6/114 y 20x6; EAN/modelo/color;
automatico y alternativas autorizadas; sitemap; sugerencias Edel-Optics;
AJAX sin endpoints; candidatos y aprobacion; contradicciones/ambiguedad;
reanudacion/budget/bloqueos/robots/redirecciones; retirada de autorizacion;
integridad SQLite/migracion; precios/historico/alcance intactos; UI offline.

Ejecutar suite de comparador, suite completa CI y build Docker. Resultados
exactos y enlace al CI se registran en la PR independiente de esta entrega.
No se autoriza merge ni despliegue por el hecho de que los tests pasen.

Validacion local previa a la PR:

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

## Riesgos, despliegue y rollback

Riesgos: buscadores que requieren JS, formatos de paginas cambiantes y
mercados multiples quedan pendientes; los limites pueden impedir completar
114 combinaciones en un intento. No se sustituyen por busquedas externas ni
se amplian presupuestos automaticamente. Una confirmacion antigua no elimina
la verificacion de identidad en la siguiente consulta comercial.

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
