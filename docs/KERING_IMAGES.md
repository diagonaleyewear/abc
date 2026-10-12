# Imagenes Kering (PR borrador, adaptador real)

## Deteccion de vistas y alcance actual (issue #38)

Esta seccion sustituye el contrato anterior que aceptaba detalle como tercera
vista. Los resultados historicos de las pruebas reales mas abajo NO prueban
completitud V1/V2/V3 bajo el contrato nuevo. La validacion local real posterior
se describe a continuacion; no se ha desplegado esta implementacion en produccion.

### Prueba funcional local REAL, 10/10/2026

Se leyo de nuevo la issue #38 y se uso la configuracion cifrada local y su clave
fuera de Git. Automatizacion desactivada y modo piloto activo; repositorios
aislados bajo `%LOCALAPPDATA%\Diagonal\kering-functional-test`. No se inicio
programador, no se escribio NAS, no se modifico Odoo ni se migro/desplego.
Conexion Odoo verificada con `transaction_read_only=on`; las consultas de
pedidos existentes usan transaccion READ ONLY y rollback.

Se encontraron 20 candidatos con EAN dentro del corte persistido 01/09/2026.
Se selecciono explicitamente uno con una linea y un EAN, autorizado en
`test_order_ids`: alias **pedido-A**, **EAN-A**. Pedido del 03/09/2026, dentro
del corte y antes del dia de la prueba Madrid. IDs/EAN reales y seleccion
se conservan SOLO localmente en `selected-order.local.json` y configuracion
cifrada; no se publican nombres de productos, pedido, proveedor ni cuenta.

#### Inspeccion real antes del renombrado

Tres originales en el visor, todos asociados a la referencia exacta de la ficha.
Se inspeccionaron `src`, `alt`, todos los nombres de atributos y senales opcionales
`title`, `aria-label`, `data-filename`, `data-view`, `data-angle`, `data-image-type`;
atributos del contenedor, clase, `data-img`, caption de figura y metadatos PNG.
No se capturo login, DOM completo, cookies, cabeceras de autenticacion ni secretos.
La inspeccion detallada permanece local; lo siguiente es una lista anonimizada:

| Imagen del EAN-A | Bytes | Formato / dimensiones | Senal real | Vista / regla |
| --- | ---: | --- | --- | --- |
| foto-A | 271516 | PNG, 2400x1286 | `alt` = referencia modelo-color; URL opaca sin extension | unknown / insufficient_evidence |
| foto-B | 171956 | PNG, 2400x1286 | Igual contrato de senales, archivo distinto | unknown / insufficient_evidence |
| foto-C | 215472 | PNG, 2400x1286 | Igual contrato de senales, archivo distinto | unknown / insufficient_evidence |

Las URLs reales pertenecen a `picture.kecdn.net`, cuatro segmentos de ruta,
sin query, nombre final opaco y sin extension. No contienen patrones Luxoptica.
Los atributos presentes son `alt`, `class`, `data-img`, `loading`, `src`;
`data-img` esta vacio, no hay caption ni senales semanticas en la clase.
No hay `title`, `aria-label`, `data-filename` o atributo de vista util.
Metadatos de bytes PNG: `icc_profile`, `transparency`, sin etiqueta de vista.
La referencia identifica el producto, NO el angulo ni el tipo de sombra.
La posicion del visor y el hecho de tener tres archivos NO se usan como evidencia.

Se comprobo el mapeo TECNICO actual del renombrado compartido:
`noshad__fr` -> V1/frontal, `noshad__qt` -> V2/perspectiva,
`shad__lt` -> V3/lateral. **No se verifico el significado semantico de
`noshad`/`shad`** en esta muestra ni hay una especificacion del proveedor
que lo acredite. No se supone "sin/con sombra" por el texto abreviado y
no se generan esos patrones para estas fotos. Se conservan los nombres
de origen saneados SIN extension (los bytes son PNG); no se anade una extension,
no se transforma el contenido ni se renombra para exportarlo.

#### Adquisicion, repeticion, estado, historial y ZIP

| Comprobacion real despues de corregir el estado | Primer intento | Segundo intento |
| --- | ---: | ---: |
| Archivos validos disponibles | 3 | 3 |
| Solicitudes GET de imagen | 3 | 0 |
| Archivos descargados / reutilizados | 3 / 0 | 0 / 3 |
| Busquedas de ficha | 2 (reintento acotado) | 1 |
| Vistas acreditadas V1/V2/V3 | 0 | 0 |
| Vistas pendientes | frontal, perspectiva, lateral | las mismas |
| Estado EAN y pedido | Parcial; clasificacion pendiente | Parcial; clasificacion pendiente |
| Contenido ZIP | 3 archivos + manifest.json | igual |

El primer ensayo reprodujo un defecto: tres fotos desconocidas adquiridas
correctamente se etiquetaban como `Error` al no haber vistas acreditadas.
Se corrigio el productor: los resultados separan `acquisition` (archivos
validos, descargados/reutilizados) de `views` (cobertura). Estado `Parcial`
sin declarar completitud; motivo `vistas_sin_identificar`, explicado en UI.
La regresion falla antes de la correccion y pasa despues.
Se repitio desde otro repositorio VACIO: tres descargas y luego cero, dejando
dos intentos durables, ambos Parcial. El ensayo inicial se conserva por separado,
sin reescribir sus intentos Error. No se renombraron archivos existentes.

ZIP verificado por nombres exactos y bytes/checksums contra el repositorio,
agrupado por EAN real (no proveedor/pedido), manifiesto con procedencia y
tres vistas pendientes; parcial explicito. Las entradas conservan el nombre
opaco y la ausencia de extension de origen. El nombre visible del pedido
se sanea para el ZIP. El manifiesto completo no se publica porque contiene
IDs/EAN/origen reales; no contiene credenciales.

Streamlit AppTest ejecuto la UI REAL con consultas reales a Odoo limitadas al
pedido seleccionado, imagenes reales y los dos intentos reales. No se sustituyo
el backend Odoo por datos simulados; solo se instrumentaron llamadas y limites
de alcance. Renderizo las nueve miniaturas (tres por superficie: detalle,
busqueda e historial), sin excepciones, y genero un ZIP con el boton.
Busqueda, galeria y exportacion provocaron **cero llamadas al portal**.

| Accion UI con datos reales | Consultas acumuladas listado | Preparaciones ZIP |
| --- | ---: | ---: |
| Entrada inicial | 1 | 0 |
| Rerender, seleccion de pedido, busqueda EAN | 1 | 0 |
| Preparar ZIP y rerender posterior | 1 | 1 |
| Cambiar filtro (aviso pendiente) | 1 | 1 |
| Actualizar pedidos | 2 | 1 |
| Salir y reentrar en la ruta (hook de navegacion en AppTest) | 3 | 1 |

Separadamente: una consulta de seleccion inicial de candidatos, una consulta
de alcance antes del piloto y DOS consultas Odoo de validacion del procesamiento,
una por intento. No son refrescos periodicos del listado. Los valores anteriores
son contadores reales, no los de las pruebas offline.

Una inspeccion adicional sufrio `PortalTimeout` transitorio; el siguiente
intento acotado obtuvo la ficha y verifico los atributos restantes con CERO
descargas, conservando tres archivos. No se atribuye ese timeout a una causa
no observada ni se aumenta el presupuesto de acceso.

#### Limites y siguiente paso

Esta muestra valida adquisicion, reutilizacion, galeria, historial, ZIP y
consultas UI, **NO deteccion automatica de V1/V2/V3**. Falta un identificador
semantico por recurso y una especificacion verificada del significado sombra/
angulo. Siguiente paso: obtener del proveedor un contrato de etiquetas o
equivalencia estable recurso-vista, con ejemplos etiquetados y criterio sobre
sombra; despues validar contra esas muestras antes de renombrar.
Alternativa: revision explicita por una persona que conozca el contrato,
registrando motivo/evidencia, sin sobrescribir ni renombrar el original.

No se entreno ni habilito un clasificador visual: un solo producto no permite
evaluar precision o cobertura representativa; haria falta un conjunto etiquetado
independiente por familia, sombra, angulo y ambiguos, incluyendo lateral ausente.
No se declara precision ni se acredita lateral usando detalle.

**Separacion de evidencia:** esta subseccion describe ejecuciones reales locales.
Los tests de portal con patrones artificiales, multiproveedor/mercados/colisiones,
limites grandes y capturas de abajo siguen siendo SIMULADOS. No se presentan como
observaciones del proveedor ni como prueba de deteccion funcional real.

### Senales y limites de evidencia

Se revisaron `PRODUCT_SNAPSHOT`, `_find_product`, `_download` y la evidencia
historica documentada. El visor ofrece `img.src` (URL opaca del CDN) e `img.alt`
(referencia modelo-color); EAN/UPC y talla son datos de la ficha, NO de la vista.
Se capturan tambien `title`, `aria-label` y `data-filename` cuando existen,
pero no se afirma que el portal real publique etiquetas semanticas en ellos.

| Ejemplo anonimizado / senal | Evidencia | Regla actual |
| --- | --- | --- |
| `https://picture.kecdn.net/<identificador-opaco>`, alt `MODELO-001` | CDN y referencia observados; la documentacion no conserva una senal semantica por imagen | unknown; no permite determinar frontal/perspectiva/lateral |
| Posicion 0/1/2 del visor | Dos muestras historicas mostraban perspectiva/frontal/detalle; no es una garantia por imagen | No usar posicion; detalle nunca acredita lateral |
| `MODELO__001__noshad__fr.png` en nombre original | Patron real del contrato Luxoptica; presencia en Kering solo probada en fixtures offline, NO observada en las URLs opacas historicas | Adaptador acepta el patron exacto previo al renombrado como frontal |
| `...__noshad__qt.png`, `...__shad__lt.png` | Mismo limite de evidencia | perspectiva, lateral respectivamente |
| Dos nombres/atributos con patrones distintos | Fixture de contradiccion | unknown; revision explicita |
| Etiqueta `front` sin contrato verificado | No hay mapeo real confirmado | unknown, no se inventa una equivalencia |

`kering_media.identify_media` inspecciona SOLO senales de origen, antes de
`kering_filename`. Guarda `original_name`, referencia, senales, fuentes, regla
y `normalized_view` en `view_detection`. No lee el nombre generado para decidir
la vista. Un nombre generado no se usa como evidencia en la siguiente descarga:
la omision usa origen URL y checksum. Una senal nueva acreditada puede clasificar
un archivo valido sin transferirlo ni cambiar su nombre, dejando evento auditado.

Para las URLs opacas reales las senales disponibles no bastan. Se evaluo la
viabilidad de clasificar por contenido: las dos muestras historicas no forman
un conjunto etiquetado representativo ni permiten medir precision/ambiguedad
por familia; no se entrena ni habilita un clasificador visual sin ese conjunto.
**No se declara una precision no medida.** Las fotos quedan desconocidas y las
vistas pendientes hasta revision explicita de cada imagen. El panel de galeria
permite confirmar/corregir una vista con revisor y motivo, conserva los mismos
bytes/nombre y registra cada cambio en `events`; no sobrescribe alternativas.
La correccion tiene prioridad sobre la inferencia automatica posterior.

Se conservan TODAS las imagenes del visor con referencia y CDN permitidos,
tambien editoriales y mas de tres fotos. URLs validas existentes se omiten,
archivos invalidos/ausentes se recuperan; checksum y bloqueo EAN evitan perdida.
Varias fotos de una misma vista y variantes de mercado no completan otras.
Solo frontal/perspectiva/lateral acreditadas, validas y visualmente distintas
completan el EAN; desconocidas y detalle se muestran/exportan sin acreditacion.

### Pantalla, consultas y exportacion

Tres pestanas: Procesar pedidos, Buscar imagenes, Historial. El listado realiza
una consulta al entrar realmente en la ruta y una por Actualizar pedidos. Guarda
fecha, filtros aplicados y resultados por sesion; cambiar filtros solo muestra
aviso pendiente. Indicadores/paginacion/seleccion/fotos no consultan Odoo.
Una tabla paginada de 20 pedidos sustituye las acciones duplicadas; seleccion
por ID estable, filtros y pedido abierto sobreviven a rerenders. La validacion
Odoo antes del procesamiento y el programador son independientes.

El progreso refresca solo el estado local del lote activo y termina el refresco
cuando finaliza. Corte, roles, credenciales cifradas, piloto de uno/dos pedidos,
bloqueos y automatizacion no cambian. Un acceso no confirmado deshabilita la
adquisicion, nunca buscar/exportar.

Buscar por EAN conserva ceros iniciales y no aplica corte al repositorio.
Pedido busca en el listado cargado y snapshots; modelo/nombre usa metadatos
disponibles, sin llamadas al portal. La galeria abre un producto por vez,
pagina de 12 miniaturas reducidas y ampliacion explicita, filtros origen/vista/
mercado y revision auditada. Fechas de pedidos/historial en Europe/Madrid.

ZIP preparado SOLO con el boton, con todos los archivos validos de sus EAN,
todos los proveedores/mercados y alternativas; EAN repetido no repite archivos.
Conserva nombres/extensiones, usa colisiones del repositorio, nombre de pedido
saneado y manifiesto de procedencia/faltantes. Parcial requiere confirmacion;
vacio se deshabilita. Historial usa los EAN del snapshot y fotos actuales, no
promete versionado historico. Planes ligeros validados se cachean por pedido,
metadatos y stat de archivos (maximo cuatro); una sola descarga preparada por
sesion, invalida ante cambios. Preparacion verifica checksum y firma antes/
despues, spool de 8 MiB, limite 256 MiB/2000 archivos. Streamlit mantiene bytes
de la descarga bajo ese limite; no hay ZIPs persistentes por pedido/proveedor.

Pruebas nuevas: `tests/test_kering_image_sections.py`; regresiones de nombres,
repositorio/migracion/NAS y piloto siguen en sus suites existentes. Docker local
no disponible: la compilacion de imagen se valida en CI de esta misma PR.
El inventario de migracion permanece bloqueado; originales sin eliminar.

Validacion visual local con 25 pedidos y fotografias **sinteticos**, sin acceso
Odoo/Kering: 1366x900 y 390x844, pestanas y busqueda por teclado, campos
adaptables, mensajes de vistas pendientes y ausencia de excepciones.
[Detalle escritorio](images/kering-desktop.png) y
[Busqueda movil](images/kering-mobile.png). No son capturas del sitio desplegado.
No se encontro un token verde de marca en el codigo/configuracion actual;
se conserva la accion `primary` del tema Cloud/Streamlit, sin inventar colores.

## Repositorio comun por EAN

Las rutas `images/kering/<EAN>` de las secciones historicas siguientes describen
el almacenamiento anterior. Las nuevas descargas, galerias y ZIP usan
`IMAGE_REPOSITORY_ROOT/<EAN>` y reutilizan originales de cualquier proveedor.
El historial/configuracion permanece en `KERING_DATA_ROOT`. Consultar las
[reglas de nombres y plan de migracion](IMAGE_REPOSITORY.md) antes del despliegue.

## Correccion de acceso no confirmado (2026-10-10)

La PR original #36 ya esta fusionada. El commit eb519f9 no estaba en main.
Se prepara una PR nueva desde main 7c12daa en `copilot/kering-access-phases`,
con solo las correcciones pendientes. Se conserva la correccion posterior de
Compose de main. Publicar la rama no actualiza el codigo desplegado.

### Causa comprobada en codigo y limite de la evidencia

`render_access_probe` muestra `Acceso no confirmado: timeout_portal` al recibir
el codigo de `KeringPortal.check_access`. Antes, este ultimo agrupaba cualquier
`BrowserTimeout`: apertura, espera/fill del formulario, click o navegacion.
No habia fase ni duracion que permitiera atribuir el incidente real.
El login exigia `expect_navigation` y comprobaba inmediatamente el marcador:
una respuesta sin navegacion podia agotar la espera, y una sesion cuyo marcador
apareciera tarde podia etiquetarse incorrectamente como `login_fallido`.
No se usaba `networkidle` en Kering y no se introduce ahora.

La validacion posterior en el contenedor Cloud identifica **envio_login**:
los campos se rellenan (17/14 ms), pero el click agota 10 s porque lo intercepta
el banner OneTrust. Se observa `#onetrust-banner-sdk`,
`#onetrust-reject-all-handler` y `.onetrust-pc-dark-filter`; el boton por texto
`Rechazarlas todas` no existe en ese runtime. Se registra un handler de Playwright
para rechazar por ID observado tambien cuando el consentimiento aparece tarde.
No se fuerza el click ni se aumenta el timeout.

Tras corregirlo pasan envio y redireccion, pero la comprobacion de sesion
agota 10 s: logout esta dentro de menus ocultos. Se contrasta DOM anonimo
(0 enlaces logout y 0 busquedas visibles) frente al autenticado (enlaces logout
y control de catalogo visible). Se exige **ambos**: logout presente y
`.showSearchBar` visible, en host HTTPS permitido. No se acepta logout oculto
aislado ni busqueda aislada. Se selecciona tambien la busqueda visible para
evitar el primer control oculto del DOM.

### Contrato corregido

| Fase | Limite |
| --- | --- |
| Validacion de configuracion | 1 s |
| Arranque Chromium | 15 s |
| Apertura portal (`domcontentloaded`) | 20 s |
| Carga formulario | 10 s |
| Envio login (fill y click) | 10 s |
| Redireccion/respuesta observable | 15 s |
| Comprobacion sesion | 10 s |

El presupuesto compartido de autenticacion es **60 s**, no la suma de los
limites de la tabla. Cada operacion bloqueante recibe el menor tiempo restante;
las comprobaciones del DOM se repiten de forma acotada. El presupuesto corresponde
al intento de autenticacion, no a la busqueda/descarga de todos los productos
ni al cierre de recursos de Playwright.

Se esperan MAIL, CONTRASENA y el boton Iniciar Sesion visible/habilitado
(observados anteriormente), o una sesion ya autenticada. Se usa exclusivamente
la pareja `a[href="/es/logout"]` presente y `.showSearchBar` visible como
marcadores autenticados observados; se exige permanecer en el host HTTPS permitido.
Cambio de URL, desaparicion del formulario o un marcador oculto aislado no
confirman acceso. Se admite autenticacion
sin navegacion. Los desafios CAPTCHA/MFA interrumpen tambien las esperas.

Codigos separados: `timeout_portal`, `login_fallido`,
`intervencion_captcha_o_mfa`, `fallo_red`, `fallo_chromium`, `fallo_portal` y
`sesion_caducada`. Se observan fallos de solicitudes de navegacion del frame
principal y HTTP >=400; fallos de recursos secundarios no invalidan una sesion
confirmada. Un rechazo requiere un aviso visible de credenciales incorrectas,
no la mera ausencia de logout. **El aviso de rechazo `role=alert` y sus textos
son una deteccion defensiva simulada, pendiente de contrastar con un rechazo
real autorizado; no se afirma que ese DOM se haya observado en produccion.**
Un fallo del API de Chromium anterior a la navegacion se distingue de la red.

La sonda devuelve y la UI muestra eventos `{phase, duration_ms, code}`.
El log de cada fase contiene exclusivamente esos campos, sin URL, usuario,
contrasena, cookies, tokens, cuerpos de respuesta ni texto de excepciones.
No activar capturas, trazas, videos ni DEBUG de Playwright para diagnosticar login.

Si falla el acceso, no se busca ni descarga ningun producto, no se reintenta
tres veces ese login en el mismo EAN y se interrumpe el lote. Se conserva el
resultado pendiente del EAN actual, el pedido con error y los siguientes pedidos
en `Pendiente`, junto con todos los archivos previos. El programador informa
`Interrumpido` y mantiene intervalo/minimo de cinco minutos antes del reintento.
Dos caducidades consecutivas de sesion tambien detienen el lote.

### Validacion disponible y procedimiento en servidor

Ejecutar **en el mismo runtime, usuario y entorno de la aplicacion**, con la
configuracion cifrada y la clave ya montadas; no copiar secretos a argumentos:

```sh
python -m scripts.validate_kering_portal --diagnostics --allow-network
python -m scripts.validate_kering_portal --access-only --allow-network
```

La primera sonda no lee credenciales: verifica DNS, TCP/443, TLS con comprobacion
de certificado/hostname para portal y CDN, y Chromium con un DOM local visible.
La segunda abre el portal y autentica, sin descargar productos. Compartir solo
el JSON saneado. El codigo de salida es distinto de cero si falla.

**Solo despues de acceso confirmado**, probar un unico pedido elegido y ya
guardado en `test_order_ids`, nunca un EAN libre para el piloto de pedidos:

```sh
python -m scripts.validate_kering_portal --order-id ID_AUTORIZADO --allow-network
```

La orden valida exactamente un ID seleccionado; usa `configured_orders`,
`prepare_selection`, `make_loader`, el bloqueo compartido y el historial durable.
Respeta el corte configurado y el dia actual Madrid, relee permisos/proveedor/
corte/pedido antes de procesarlo y reutiliza imagenes validas. No cambia Odoo,
la seleccion ni la activacion de la automatizacion. Un fallo de acceso previo
no inicia descarga; un fallo posterior conserva pendientes en historial.

Evidencia local Windows (no servidor), 2026-10-10:
DNS portal 172 ms, TCP 20 ms, TLS 389 ms; DNS CDN 158 ms, TCP 11 ms,
TLS 116 ms; Chromium 1543 ms: todos `ok`.
No equivale a validar el runtime Linux/contenedor desplegado.

Pruebas offline cubren timeout en cada fase, presupuesto total, autenticar sin
navegacion, marcador demorado/oculto, rechazo, CAPTCHA/MFA, fallo de red/Chromium,
recursos secundarios fallidos, saneamiento, parada del lote, pendientes durables,
espera del programador y piloto CLI limitado a un ID con corte compartido.
La prueba CLI rechaza automatizacion activada y confirma exactamente frontal,
perspectiva y detalle, dimensiones, identidad exacta EAN/UPC/modelo/color/talla
y asociacion al pedido. En reutilizacion recupera la evidencia de identidad del
historial previo; no inventa una nueva comprobacion de portal.

### Evidencia real en Cloud, 2026-10-10

SSH desde Windows local a rubensg@s2026; runtime `abcd-control` en /app,
Python 3.11, imagen `sha256:73b129ad852db398adfe33682f51c596071c32850e5a81ca5d3c26919b7b1f44`.
Checkout servidor sigue main 7c12daa. Los modulos de la rama se cargaron solamente
en memoria en procesos `docker exec`: **no se despliega ni reemplaza codigo**.
Las imagenes y dos intentos de validacion del pedido si quedan persistentes.

| Sonda del runtime | Duracion ms | Codigo |
| --- | ---: | --- |
| DNS portal | 282 | ok |
| TCP portal | 19 | ok |
| TLS portal (certificado/hostname verificados) | 61 | ok |
| DNS CDN | 186 | ok |
| TCP CDN | 15 | ok |
| TLS CDN | 54 | ok |
| Chromium | 898 | ok |

Acceso con credenciales cifradas ya configuradas, sin imprimir secretos:
`acceso_autenticado`, 7907 ms. Fases: configuracion 0, Chromium 695,
apertura 2502, formulario 176, envio 1886, redireccion 1934, sesion 714 ms,
todas ok. No se exportaron cookies, tokens, respuesta de cuenta ni capturas.

Se procesa exclusivamente el ID **6094**, fecha **2026-09-03 08:50:40 UTC**,
posterior al corte vigente **2026-09-01**. Seleccion guardada: 6094/6095;
6095 no se procesa durante la validacion. Auto desactivado antes y despues.

| EAN/UPC de linea | Vistas originales | Dimensiones por vista | Identidad | Repeticion |
| --- | --- | --- | --- | --- |
| 889652494821 | frontal, perspectiva, detalle | 2400x1286 | exacta, modelo/color/talla verificados | tres reutilizadas |
| 889652494838 | frontal, perspectiva, detalle | 2400x1286 | exacta, modelo/color/talla verificados | tres reutilizadas |

Primera pasada: 6 descargas y 3 invocaciones de busqueda para dos EAN
(primer EAN requirio dos intentos; segundo uno). Resultado Completo.
Segunda: **0 busquedas, 0 descargas, tries=0** en ambas lineas; Completo.
La sonda de acceso independiente de la segunda pasada si abre el portal,
pero el procesamiento del pedido no lo hace. Auditoria posterior con el codigo
desplegado confirma `Procesado`, 2 lineas completas, 0 pendientes, sin cambios,
dos intentos finalizados y ZIP valido con exactamente 6 PNG correspondientes.
La identidad de la pasada reutilizada se acredita por el intento anterior;
el historial de reutilizacion no contiene una identidad recapturada.
No se modifica Odoo ni se activa automatizacion ni se procesa otro pedido.

**Pendientes**: revision/fusion de la PR y despliegue autorizado para que Cloud
use de forma persistente la correccion. La prueba en memoria no actualiza
produccion. CAPTCHA/MFA y rechazo explicito siguen validados solo por simulacion.
Resultados locales y checks remotos se detallan en la PR.

## Estado real de la integracion

El 2026-10-09 se inspecciono una sesion autorizada del portal
https://my.keringeyewear.com/keringeyewear/es/login, sin leer ni exportar cookies,
credenciales o datos de cuenta. Solo se capturaron las fotografias del producto.
Se implemento `KeringPortal` y se inyecta desde la UI con configuracion cifrada;
`UnverifiedPortal` queda como fallback de llamadas sin adaptador, no de la pantalla.

Evidencia real:

- Login publico: MAIL, CONTRASENA, Iniciar Sesion; autenticacion indicada por
  enlace `/es/logout`. No se deduce acceso solamente porque desaparezca el login.
- Busqueda observada: `.showSearchBar`, `#hard-js-site-search-input`, Enter,
  formulario `#search_form`. El portal construye la consulta; no se inventan URLs.
- Buscando EAN **8056376560527**, los resultados incluyeron **MB0412S-001**,
  referencia interna 30016317001. La ficha confirma EAN 8056376560527 y UPC
  distinto 889652542942. No se asume conversion entre ambos. Se acepta solo
  coincidencia textual exacta de EAN o UPC en `.characteristics-title` y valor
  hermano; se registra modelo, color y talla de esa misma ficha.
- Originales del visor `#imageModal .itemModal img`: indices 0/1/2 son
  perspectiva/frontal/detalle en la muestra. El cuarto es editorial con modelo,
  no se cuenta. **No hay lateral independiente: se guarda detalle, no lateral.**
  El orden y el detalle se han contrastado tambien en BV1012S-004; conviene
  ampliar muestras si se incorporan familias con convenciones distintas.
- Los tres originales respondieron HTTP 200, image/png, **2400x1286** frente a
  galeria 512x512 y miniaturas 128x128. Bytes: perspectiva 116124, frontal 99353,
  detalle 159631. Huellas de pixels distintas. Se descarga la mayor resolucion
  que el visor publica, sin modificar la URL ni inventar transformaciones.
- Host de originales observado: `picture.kecdn.net`; las URLs opacas se leen
  directamente del DOM, no se generan por patron.

Se ejecuto ademas `scripts/validate_kering_portal.py` con credenciales introducidas
por el administrador en Configuracion y leidas solo desde almacenamiento cifrado:

| EAN | Modelo/color/talla | Acceso | Primera descarga | Repeticion | Repositorio parcial |
| --- | --- | --- | --- | --- | --- |
| 8056376560527 | MB0412S / 001 / M | Confirmado | Frontal, perspectiva, detalle | 0 busquedas, 0 descargas | 1 busqueda, 1 descarga |
| 8056376294347 | BV1012S / 004 / XL | Confirmado | Frontal, perspectiva, detalle | 0 busquedas, 0 descargas | 1 busqueda, 1 descarga |

En ambos casos, la segunda ejecucion reutilizo tres archivos validos. El parcial
se preparo en un repositorio temporal con frontal y perspectiva y recupero
unicamente detalle. Las carpetas de validacion se eliminaron al terminar.
Se corrigio durante la prueba el cierre explicito de conexiones SQLite, necesario
para poder limpiar temporales en Windows. No se imprime config ni secretos.

La PR permanece en borrador para revision. CI y build Docker remotos pasaron
para el codigo validado: run 37903991259, commit 06f6170, el 2026-10-09.
Verify dependency locks, pruebas offline, lint y sintaxis: success;
build-and-push: success (build de PR, sin publicacion de imagen ni despliegue).
Deploy to Server: skipped. No se han fusionado PR ni desplegado cambios.
CAPTCHA/MFA y caducidad
se prueban con simuladores, no con desafios reales. No se ha encontrado todavia
una ficha real con menos de tres originales; el parcial manual es un repositorio
incompleto y no debe presentarse como producto incompleto real de Kering.

## Arquitectura y permisos

- Streamlit: `kering_images_ui.py`, integrado en `app_enhanced.py`.
- Tanto configuracion como pantalla de pedidos, historial y ZIP requieren el rol
  `admin` existente; el rol `masterdata` no tiene acceso.
- Odoo: reutiliza `get_db_engine()` y la configuracion PostgreSQL `DB_*` existente.
  Solo ejecuta SELECT; la transaccion de pedidos se marca READ ONLY y se revierte.
  No modifica pedidos, lineas ni fichas. El usuario DB debe tener solo permisos
  de lectura sobre `purchase_order`, `purchase_order_line`, `product_product` y
  `res_partner`. La conexion SQL no aplica reglas de registro de Odoo: conceder
  acceso DB solamente al conjunto de datos autorizado para esta aplicacion.
- Consulta por `partner_id` exacto por defecto, `date_order`, excluyendo `cancel`.
  Una opcion explicita, desactivada por defecto, incluye contactos con el mismo
  `COALESCE(commercial_partner_id,id)` de la entidad seleccionada. No se comparan
  nombres. Ver contactos asociados permite revisar la relacion real antes de ampliar
  alcance. Contactos archivados pueden tener pedidos historicos validos.
  `order_ids` se filtra en SQL con `bindparam(expanding=True)` y valores enteros,
  sin releer el intervalo entero por pedido. Lista vacia no consulta; datos no
  enteros se rechazan. Requiere columnas Odoo `display_type`,
  `product_id`, `barcode`, `commercial_partner_id` y `supplier_rank`; el esquema real no se ha validado
  contra produccion. Secciones y notas no participan en la comprobacion.
  Se reviso mediante SELECT la relacion comercial del proveedor guardado:
  solo una entrada asociada a su propia entidad, sin contactos adicionales.
  No se exportaron nombres, emails ni datos de cuenta. La ampliacion de alcance
  sigue siendo explicita por si esta relacion cambia en Odoo.
- Fechas: dias completos Europe/Madrid, intervalo UTC semiabierto desde medianoche
  hasta medianoche del dia posterior al final. Incluye ambos dias, incluso en DST.
  Se presupone el almacenamiento estandar Odoo `date_order` UTC sin zona.
  La tabla de seleccion presenta fechas locales de Madrid.

## Configuracion segura

Usuario y contrasena iniciales vacios. URL predeterminada: la indicada arriba.
En Configuracion > Kering, seleccionar proveedor Odoo y guardar los datos.
La contrasena se introduce enmascarada, nunca se rellena con el secreto guardado;
dejarla vacia conserva la anterior. La opcion Eliminar credenciales las borra.
Comprobar configuracion verifica clave, proveedor y campos obligatorios;
**no prueba el login ni la descarga real**.

Probar acceso a Kering es una accion distinta: login real en segundo plano y
comprobacion del marcador autenticado; no descarga productos. Las credenciales
no se toman de constantes ni parametros CLI. Configuraciones cifradas anteriores
sin `include_commercial_contacts` siguen funcionando con la opcion false.
No activar DEBUG/trazas/videos de Playwright ni capturas de login/cuenta.

La configuracion existente de otras integraciones usa `.env` sin cifrado; no se
reutiliza ese almacenamiento para usuario/contrasena Kering. Se guarda el documento
completo en `config.enc` cifrado y autenticado con Fernet. Los errores de interfaz
y de producto son mensajes/codigos fijos; no incluyen excepciones del proveedor.
No se exportan credenciales en ZIP ni en historial.

Configurar en el servidor una clave Fernet aleatoria de 32 bytes codificada URL-safe:

- Preferido: archivo externo al repositorio y al volumen de datos; montar como
  Docker secret y establecer `KERING_ENCRYPTION_KEY_FILE=/run/secrets/kering_key`.
- Alternativa: `KERING_ENCRYPTION_KEY` inyectada por el gestor de secretos del servidor.
- Sin clave valida, la configuracion no se puede guardar/descifrar. No hay clave
  predeterminada, derivada de la contrasena de la app ni guardada en Git.
- No imprimir la clave, contrasenas ni documentos descifrados. Restringir archivos
  de clave a su propietario (0600) y directorio de datos a usuarios autorizados
  (0700). Mantener la clave en un backup separado; perderla impide recuperar
  las credenciales. Para rotar, descifrar con la clave anterior y guardar con la
  nueva en una ventana administrativa antes de retirar la anterior.

Ejemplo de override de Docker Compose (solo rutas, ningun valor secreto):

```yaml
services:
  abcd-app:
    environment:
      KERING_ENCRYPTION_KEY_FILE: /run/secrets/kering_key
    secrets:
      - kering_key
secrets:
  kering_key:
    file: /etc/abcd-secrets/kering.key
```

Provisionar ese archivo con el gestor de secretos. Fernet.generate_key() es la API
para generar una clave nueva; escribirla directamente al archivo protegido,
sin mostrarla ni introducirla en chat. No incluir datos reales en CI.

## Persistencia, procesamiento y galeria

`KERING_DATA_ROOT` es `/app/data/kering` en Docker. En local se usa
`masterdata_data/kering`. El volumen `./masterdata_data:/app/data` existente
permite escrituras incluso con el contenedor read_only y sobrevive a despliegues.
No eliminar ese volumen al desplegar. Copiarlo junto con un backup coherente
de SQLite (usar sqlite3.Connection.backup, no copiar solamente el .sqlite3
durante escrituras WAL). Los datos estan excluidos de Git y del contexto Docker.

- `config.enc`: configuracion cifrada.
- `history.sqlite3`: ejecuciones, intentos, snapshots de lineas, resultados y
  metadatos de archivos; WAL y synchronous FULL.
- `images/kering/<EAN>/<vista>.img`: bytes originales sin transformaciones ni
  imagenes sinteticas; extension interna neutra, se exporta extension real en ZIP.
- `locks/<EAN>.lock`: bloqueo FileLock entre procesos sobre almacenamiento local.
  No desplegar este mecanismo sobre NFS/varios servidores sin validar bloqueos
  y SQLite o sustituirlos por almacenamiento/bloqueo compartido adecuado.

Antes de consultar el adaptador se validan archivo, checksum, decodificacion,
formato JPEG/PNG/WebP, tamano minimo 600x600 y huella RGB normalizada para rechazar
duplicados exactos o cambios simples de resolucion. El umbral es conservador y
no equivale a confirmar la resolucion maxima del portal. La huella no demuestra
semanticamente las vistas; esa prueba depende de la evidencia del adaptador real.
Se admite detalle cuando no hay lateral disponible, sin etiquetado falso.
Un archivo ausente/corrupto vuelve a ser pendiente. Se guardan archivos con
tempfile, fsync y reemplazo atomico, sin reemplazar vistas ya validadas. Los mismos
EAN reutilizan archivos entre pedidos. La galeria existente muestra el mercado
Kering a partir de metadatos y archivos comprobados, no mezclado con Luxottica.

Un lote de procesamiento compartido entre procesos manuales/automaticos,
en ThreadPoolExecutor fuera de Streamlit para la via manual.
Chromium se inicia de forma lazy solo para EAN pendientes y se cierra por lote;
un EAN completo no provoca ni busqueda, ni descarga, ni login.
`batch.lock` impide solapamientos de lotes; se mantienen ademas los locks por EAN.
Un EAN ocupado queda pendiente con `ean_en_proceso`; reintentar cuando termine.
Los EAN repetidos se consultan una vez por lote. El heartbeat mantiene viva una
ejecucion larga. Tras 5 minutos sin heartbeat, al abrir historial o iniciar lote,
los pedidos sin terminar se marcan Interrumpido, conservando vistas ya escritas.
No hay reanudacion automatica tras reiniciar: usar Reintentar pendientes y errores.

Estados del intento: Pendiente, En proceso, Completo, Parcial, Error, Interrumpido.
Completo exige EAN y tres vistas validas para todas las lineas aplicables.
Un pedido sin lineas aplicables es completo de forma vacua. Lineas sin EAN
quedan registradas y evitan completar. Cada intento conserva pedido, proveedor,
fecha, rango, usuario, ejecucion y resultados por EAN/vista. Los intentos anteriores
son inmutables como evidencia; una corrupcion posterior no reescribe el resultado
historico, pero el visor actual la muestra pendiente y el proximo intento la repara.
Se releen pedidos al ejecutar cada intento y al consultar fechas; nunca se omite
un pedido solo por su estado anterior. Un cambio de proveedor, cancelacion o salida
del rango provoca error de lectura del intento, sin procesar datos fuera del filtro.

La pantalla incluye seleccion multiple, progreso automatico cada 3 segundos,
detalle, tres espacios por producto, motivos, ZIP con archivos validos, filtros
de historial por numero, fecha de pedido, fecha de ejecucion y estado, y reintento.
El ZIP de historial corresponde al snapshot de ese intento, no a futuras lineas.

## Adaptador y limites

`fetch(ean,pending)` reproduce la busqueda observada y revisa hasta 40 candidatos
en la pagina de resultados. Solo devuelve originales de una ficha cuyo EAN/UPC
coincida exactamente, cuya referencia modelo-color sea coherente y tenga talla.
Se usan recursos HTTPS de hosts confirmados, sin seguir redirects de imagen.
HTML, miniaturas, duplicados y editoriales no cuentan como tres vistas.
El mapeo de indices del visor requiere contraste adicional si Kering cambia
su layout/orden o publica familias con convenciones distintas.

Timeout de navegacion/HTTP 30 s. Una reautenticacion por fetch si caduca sesion,
conservando bytes obtenidos para no repetir descargas; motor maximo tres intentos
por EAN. Login fallido y producto no encontrado no se reintentan. CAPTCHA/MFA
visible requiere intervencion, sin eludirlo. Deteccion conservadora de campos OTP,
iframes habituales y mensajes de verificacion; desafios distintos pueden acabar
como timeout y requerir revision manual. No se guardan perfiles/cookies.
Errores y repr usan mensajes fijos, sin excepciones crudas ni configuracion.

Validacion manual opt-in (no ejecutar desde CI), credenciales cifradas guardadas:

```bash
python scripts/validate_kering_portal.py --ean 8056376560527 --allow-network
```

Usa carpeta temporal aislada: acceso, descarga completa, segunda ejecucion con
cero busquedas/descargas, repositorio parcial con frontal/perspectiva y reintento
que debe recuperar solo una vista. No imprime credenciales ni las recibe por CLI.
El parcial corresponde a datos locales, no demuestra una ficha real incompleta.
El flujo con credenciales configuradas ya paso para las dos referencias arriba.
Pendientes: ampliar familias y encontrar una ficha realmente incompleta;
build Docker local no disponible. Build/CI remoto ya comprobados en verde.

## Validacion offline

```bash
python -m pytest -q tests/test_kering_images.py
python -m pytest -q tests test_transform_luxottica_masterdata.py test_validate_masterdata_odoo_dryrun.py
python -m flake8 kering_images.py kering_images_ui.py kering_portal.py scripts/dependency_locks.py scripts/validate_kering_portal.py tests/test_kering_images.py --select=E9,F63,F7,F82
python -m py_compile kering_images.py kering_images_ui.py kering_portal.py scripts/dependency_locks.py scripts/validate_kering_portal.py
```

Las pruebas usan SQLite como Odoo simulado, portales FakePortal y fotografias
sinteticas exclusivamente dentro de tests. Navegador y HTTP tambien simulados.
Cubren inyeccion de config, login, caducidad, timeout, intervencion, identidad exacta,
SQL por IDs/contactos, pendientes con detalle existente y errores sin secretos;
fechas/DST, proveedor/cancelados,
notas/secciones, ceros, EAN ausentes/duplicados, completos/parciales, reutilizacion,
archivos ausentes/corruptos, concurrencia, interrupciones, reintentos, cambios,
persistencia, permisos y secreto no pre-rellenado. Un guard impide conexiones
socket externas e inicio de Playwright en estas pruebas.
**Ninguna prueba automatizada valida el portal Kering real**.

Comprobaciones locales (2026-10-09): 30 pruebas Kering y 101 en la suite exacta
del workflow, todas aprobadas; lint estricto y py_compile pasan. Diez avisos de
deprecacion SQLite datetime en Python 3.13. Evidencia real separada arriba.

## Locks y dependencia de PR

El fallo original de Verify dependency locks, run 37106371795, se produjo por
cabeceras/anotaciones --constraint distintas y nueva resolucion sin pins:
MarkupSafe 3.0.3 -> 3.0.4, SQLAlchemy 2.1.2 -> 2.1.3, Streamlit 1.64 -> 1.65 y
WebSockets 16.1.1 -> 17.1. Se conservan las versiones fijadas del commit anterior.
Generacion/verificacion compartida con uv 0.12.22, Python 3.11/Linux x86_64,
constraints de los locks actuales, --no-annotate y cabecera canonica. CI conserva
comparacion de bytes estricta; no se ha eliminado la comprobacion.

```bash
python scripts/dependency_locks.py --write  # regenerar sin actualizar pins
python scripts/dependency_locks.py         # verificar: codigo 1 si difiere
```

PR #36 sobre PR #35 (`copilot/adyen-odoo-reconciliation-pr`), ambas abiertas
el 2026-10-09. #35 depende de `copilot/order-alerts-scheduler`. Orden: integrar
las bases de #35, despues #35, despues #36. Solo cuando #35 se integre en la rama
destino, ajustar base de #36 y repetir CI. No se han fusionado ni desplegado PR.
#36 se mantiene en borrador mientras falte validar el flujo real o falle CI.

## Listado de pedidos, corte y piloto inicial

Fecha de corte persistente en configuracion cifrada: **01/09/2026** por defecto,
editable en Configuracion > Kering > Procesamiento Kering. Se interpreta como
medianoche Europe/Madrid (2026-08-31 22:00 UTC para este corte). Un filtro desde
anterior se eleva al corte; hasta anterior devuelve vacio. No se borran historico,
metadatos ni archivos al cambiarlo. El listado y los loaders de ambos modos usan
`configured_orders`; se relee configuracion y pedido antes de cada ejecucion.

La primera pantalla es el listado Odoo actual, incluyendo pedidos nunca
procesados. Cruza por ID con el ultimo intento y comprueba archivos persistentes:
numero, fecha local, estado Odoo, No procesado/En proceso/Parcial/Procesado/Error/
Interrumpido, productos completos y pendientes, ultimo procesamiento y cambios.
Procesado exige intento completo, firma de lineas/productos/EAN/cantidad/nombre
sin cambios y tres vistas validas por linea. Nuevas lineas o archivos ausentes/
corruptos provocan pendientes; un estado historico no evita la comprobacion actual.

Acciones: Procesar por pedido, seleccion multiple y Procesar seleccionados.
Listado/estado se actualizan cada 5 segundos y progreso cada 3 segundos.
Detalle muestra productos, EAN, tres vistas disponibles/pendientes, motivos y ZIP
del pedido actual, sin depender de que tenga historial. El historial de intentos
continua accesible, incluidas ejecuciones sin pedidos o con error de lectura.
Fotos compartidas por EAN: no se duplican por pedido.

**Modo de prueba activo por defecto**, sin pedidos autorizados inicialmente.
Seleccionar explicitamente uno o dos pedidos en Pedidos autorizados para prueba
y guardar. Los no autorizados no pueden procesarse manual ni automaticamente.
Los controles por pedido permanecen visibles, deshabilitados hasta autorizacion.
Un lote completo no se habilita por defecto: se exige evidencia en el historial
del piloto (dos intentos completos, uno con reutilizacion de todas las vistas,
al menos una descarga y archivos actuales validos), y accion explicita de admin
para desactivar modo de prueba. Si se invalida esa evidencia, se vuelve a bloquear.

### Validacion real de pedidos autorizados (2026-10-09)

El administrador autorizo explicitamente **P06097/6105** y **P06110/6118**,
ambos con una linea; se guardaron solo esos IDs. Automatizacion **desactivada**.
Consulta de Odoo desde corte: 19 pedidos, incluidos No procesado. Dos intentos
manuales por cada piloto quedaron persistidos:

| Pedido | Primera ejecucion | Segunda ejecucion | Estado actual |
| --- | --- | --- | --- |
| P06097 | Codigo Odoo 889652356099 resuelto exactamente a UPC de GG0998S-001, talla M; tres originales descargados | Tres vistas reutilizadas, tries=0, sin llamadas Kering para el EAN | Procesado |
| P06110 | Codigo 2532000985790 no encontrado en el portal; producto_no_encontrado, sin inferir equivalencia | Mismo error acotado, sin fotografias falsas | Error |

El piloto es **parcial**, no una validacion masiva operativa. No se corrige Odoo
ni se inventa una relacion para el codigo no encontrado. El lote completo sigue
bloqueado porque uno de los pilotos no se completo. Cambios de corte, frontera
del propio dia 01/09, EAN compartidos, concurrencia y programador se validan
offline con simuladores; no se ha habilitado el servicio real ni activado auto.

## Procesamiento automatico cada X horas

Configuracion compartida: auto_enabled=false inicial; auto_interval_hours=6
inicial, finito y >0; mismo cutoff_date, proveedor/contactos y piloto que la via
manual. Se muestra ultima ejecucion, proxima ejecucion y resultado persistidos.
Modificar intervalo recalcula el siguiente vencimiento sobre el final previo;
desactivar evita proximos lotes, no aborta una descarga que ya este en curso.

`kering_jobs.py` es un proceso servidor independiente de Streamlit. Servicio
Docker **abcd-kering-scheduler**, misma imagen y volumen `/app/data`, sin crontab
ni necesidad de navegador/app abiertos. Inicio del servicio no activa descargas:
es obligatorio auto_enabled en la configuracion. No se ha desplegado en esta PR.

Cada vencimiento consulta desde corte hasta el dia actual completo de Madrid;
en piloto consulta solo los IDs autorizados. Compara lineas/fotos/historial y
procesa nuevos, incompletos o cambiados. Pedidos completos sin cambios se omiten;
un EAN completo reutilizado no provoca busqueda ni descarga. Cada pedido se relee
antes de ejecutarlo y se revalida proveedor/corte/alcance. Origen **Manual** o
**Automatico** en runs/historial (la interfaz muestra el acento).

SQLite conserva ultima/proxima ejecucion, ID y resultado. `batch.lock` se adquiere
antes de leer decisiones de programacion; no se solapan procesos automaticos
entre si ni con los manuales. `schema.lock` protege migraciones simultaneas y
locks por EAN siguen protegiendo imagenes. No usar NFS/multiples servidores sin
sustituir el mecanismo de coordinacion; ambos contenedores deben compartir volumen.

Al recuperar el bloqueo libre tras reinicio, intentos abandonados se marcan
Interrumpido. Una ejecucion automatica interrumpida espera otro intervalo antes
de reintentarse. Errores de acceso/consulta esperan intervalo, con minimo 5 min
para Error/Parcial/Interrumpido; errores de configuracion esperan 5 min.
El daemon revisa configuracion como maximo cada minuto, no repite continuamente
logins ni busquedas ante errores. Fallos por producto no detienen otros pedidos.
Resultados y errores usan codigos fijos sin credenciales.

Usar el mismo Docker secret/clave en **ambos servicios**:

```yaml
services:
  abcd-app:
    environment:
      KERING_ENCRYPTION_KEY_FILE: /run/secrets/kering_key
    secrets: [kering_key]
  abcd-kering-scheduler:
    environment:
      KERING_ENCRYPTION_KEY_FILE: /run/secrets/kering_key
    secrets: [kering_key]
secrets:
  kering_key:
    file: /etc/abcd-secrets/kering.key
```

Si se usa gestor de secretos por variable, inyectar la misma KERING_ENCRYPTION_KEY
en ambos. No copiar claves/credenciales a Git ni a imagen Docker. El servicio
recibe DB_* del proyecto y solo hace lectura Odoo. Filesystem read_only, /tmp
escribible para Chromium y volumen de datos persistente. Signal TERM/INT permite
terminar ordenadamente entre iteraciones; si hay terminacion forzosa, se recupera
el historial en el proximo arranque. `python kering_jobs.py --once` ejecuta una
revision controlada respetando configuracion/alcance, sin activar el programador.

### Pruebas de esta ampliacion

42 pruebas Kering offline; **113** en la suite exacta del workflow, aprobadas.
Lint estricto, py_compile, locks y YAML Compose pasan localmente. Dieciseis avisos
de deprecacion SQLite datetime en Python 3.13. Casos: frontera UTC/Madrid del corte,
persistencia sin perdida, No procesado visible, accion Procesar/detalle/ZIP,
segunda ejecucion sin llamadas, EAN compartidos, cambios/corrupcion, intervalo,
desactivacion, modo piloto, bloqueos manual/auto, decisiones dentro del bloqueo,
reinicio/interrupcion, errores de consulta persistidos y espera acotada.
No se ejecuta Kering/Odoo real en CI; las validaciones reales autorizadas se
describen aparte. Build Docker local no disponible: validar build remoto de PR.

## Diagnostico del listado

El listado identifica la etapa que falla sin mostrar excepciones ni datos
sensibles: KERING_CONFIG, KERING_ALMACENAMIENTO, ODOO_CONEXION, ODOO_CONSULTA,
KERING_HISTORIAL o KERING_ESTADO. Un fallo de acceso PostgreSQL no se presenta
como un problema de fotografias ni inicia descargas nuevas.

Revision del 2026-10-09: conexion rechazada al PostgreSQL remoto configurado
en el puerto estandar 5432, antes de consultar pedidos. La URL interpretaba
correctamente la configuracion; tambien fallo la prueba con URL.create.
No era un tunel local ni un error del historial. Debe revisarse disponibilidad
del servicio remoto, listen_addresses/puerto y firewall/acceso de red con su
responsable. No se reiniciaron servicios de produccion ni se modifico Odoo.
Los archivos e intentos persistentes se conservan. Pruebas offline de UI verifican
los codigos de error y que SQL, usuarios y secretos no aparezcan en el aviso.

Dependencias nuevas: cryptography (Fernet), filelock (bloqueo entre procesos),
tzdata (zonas en Windows). Pillow, SQLAlchemy, Streamlit y Playwright ya existian.
Los locks de produccion/desarrollo se generan con uv para Python 3.11/Linux.
El Dockerfile se ha actualizado para copiar los modulos; no se ha probado un
build Docker local porque no hay motor Docker disponible. Chromium local si esta
instalado y se utilizo en las dos validaciones reales anteriores. El build Docker
remoto de GitHub Actions si paso; no equivale a desplegar ni a probar el portal
real desde dentro del contenedor de produccion.