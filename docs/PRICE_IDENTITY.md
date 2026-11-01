# Identidad del comparador: EAN o modelo y color

## Revision conjunta del 1 de noviembre de 2026

Base cotejada: `9d240daf38ae5a9629d370d32641986cf9ef4cad`. Al iniciar
esta correccion, `origin/main` seguia en ese commit; no habia diferencias
posteriores que resolver. El paquete comercial aportado se leyo completo:
informe HTML, Excel, matriz, identidades, historico, prioridades, referencias
tecnicas y cotejo original. Los archivos ya descomprimidos coincidian byte a
byte con el ZIP. Excel y JSON coinciden en las 84 combinaciones, identidades,
precios fechados y EAN/colores como texto.

El alcance comercial es seis EAN por 14 competidores: 69 identidades exactas,
dos variantes distintas y 13 busquedas sin ficha exacta localizada. Las otras
94 referencias del original no se han revisado. Las tres lecturas comparables
del informe son observaciones puntuales, **no una regla por tienda ni precios
vigentes garantizados**. El acceso manual no acredita automatizacion y no
levanta bloqueos ni modifica la configuracion.

| Clasificacion | Evidencia y resultado |
|---|---|
| Defecto reproducible | `Product` y `ProductGroup` con misma ruta y distinto `?variant=` elegian el primer nodo por ruta, mezclando identidad, precio y stock. Se selecciona una unica URL completa, incluida query; tambien se comprueba la URL de `Offer`. Una URL de oferta no puede contradecir la variante declarada por su `Product`. Sin vinculacion unica, no se toma precio/stock ni se acepta identidad exacta. |
| Defecto reproducible | Un selector global podia suplir un precio ausente de la variante activa con el precio transitorio de otra variante. En paginas multivariante, precio, EAN, IVA por precio sin impuestos y mercado globales no se heredan. Se conservan las URLs declaradas, el identificador `variant`, SKU/MPN y la evidencia. |
| Defecto reproducible | `FT1044/S 93E` no coincidia con `FT1044 93E`; la normalizacion compartida de modelos FT de cuatro digitos admite el sufijo `/S`, sin eliminar ni flexibilizar el color. `52S` y `01E` siguen rechazados. |
| Defecto reproducible | El parser podia ignorar `OPT` tras `SL 596 DUNE`, incluso con EAN objetivo. Ese sufijo se conserva como parte del modelo: no se acepta DUNE OPT como DUNE de sol. |
| Defecto reproducible | Varios GTIN del mismo `Product` ocultaban la contradiccion tomando el primero. Se conserva la lista en evidencia y se requiere revision. UPC equivalente y ceros iniciales siguen usando la normalizacion existente. |
| Defecto reproducible | El campo JSON-LD `model` no se transmitia al clasificador. Ahora se conserva, de modo que un modelo explicito incorrecto no puede esconderse tras un titulo y EAN coincidentes. |
| Defecto reproducible | CTA y «disponible bajo pedido» se trataban como stock; un selector «Agotado» no se contrastaba con JSON-LD `InStock`. Se guardan las señales y el plazo por separado. CTA, canal OnlineOnly/InStoreOnly y plazos no acreditan stock. Contradicciones quedan `unknown`, sin borrar precio ni identidad. |
| Defecto reproducible | Una `priceSpecification` condicionada por cantidad o membresia podia suplir el precio de venta; IVA del PVP podia atribuirse a la venta. Solo especificaciones de venta no condicionada sustentan el precio/IVA. PVP, historico y cupones no se extraen como precio de venta de un selector. |
| Ya resuelto, conservado | Calibre informativo, UPC/EAN, rechazo de EAN/modelo/color contradictorios, tarjetas Edel independientes, precio de venta frente a PVP, ausencia de precio estatico, historial y reutilizacion de URLs verificadas. |
| Ya resuelto, conservado | Descubrimiento guarda candidatos y razones y evalua un segundo candidato tras rechazar el primero. La regresion FT1044 demuestra 52S rechazado y 93E candidato, con cuatro peticiones simuladas dentro del presupuesto existente y aprobacion humana sin HTTP. |
| Falta de evidencia comercial | IVA, mercado ES y stock no publicados siguen sin acreditarse. EUR, idioma y seleccion regional (incluida Austria en `/en-eu`) no prueban entrega ES. No se rellena configuracion ni se importan precios del informe. |
| Restriccion de autorizacion/configuracion | Robots, bloqueos persistentes, limites, reanudacion, alcance del piloto y permisos existentes no cambian. No se activan planificadores ni metodos nuevos. |

### Stock y plazo: criterio conservador

Las señales de disponibilidad de las ofertas se conservan separadas del
precio y de `deliveryTime`. Dos estados explicitos incompatibles requieren
revision sin convertir una identidad exacta en un producto distinto.
El selector de disponibilidad ya autorizado conserva su texto completo como
evidencia. «En Stock» junto a «entrega/envio en N dias» positivos, o junto
a «bajo pedido», no acredita stock inmediato coherente: se marca conflicto
para revision, sin afirmar que no haya existencias. No se introduce un
umbral arbitrario de dias, ni se deduce stock de un plazo. Un tiempo de
transito estructurado por si solo no se interpreta como falta de stock.

### Limites de esta correccion

Las nuevas fixtures son sinteticas, con importes de prueba, no una copia
de precios comerciales supuestamente actuales. Los 17 fallos iniciales de
regresion reproducen riesgos del codigo base; no prueban que el precio
transitorio 207/204 EUR de Celine fuera causado por JSON-LD en Cloud.
El paquete no contiene los HTML crudos de cada estado de sus controles:
no se inventan selectores de tienda ni mecanismos de espera/recarga.
Si una variante solo puede identificarse tras JavaScript o su JSON-LD no
publica una URL unica y coherente, requiere revision con una fuente
autorizada. No se automatiza navegador ni se usan endpoints privados.

Los campos adicionales de `ProductPage` se guardan en la evidencia JSON y
las notas comerciales existentes; **no hay migracion SQLite**. El codigo
actual admite evidencia anterior mediante valores por defecto. Riesgo de
reversion: versiones antiguas que construyen `ProductPage(**evidencia)`
no conocen los nuevos campos de candidatos; antes de revertir codigo con
datos generados por esta version debe evaluarse esa compatibilidad, sin
borrar ni reescribir historicos automaticamente.

Pruebas nuevas:
[regresiones conjuntas offline](../tests/test_price_reviewed_variants.py).
La suite existente sigue verificando robots, bloqueos, presupuesto,
reanudacion y retirada de autorizaciones. No se cambian esos componentes
para hacer pasar casos comerciales pendientes.

### Validacion local de esta revision

- Regresiones nuevas: 55 casos; focalizadas con identidad, referencias,
  estructuras existentes y tarjetas Edel: **164 correctos**.
- Suite final `tests/test_price*.py`: **515 correctos, 1 omitido**.
- Suite general offline durante la implementacion: **1422 correctos,
  16 omitidos y 10 subtests correctos**; esa pasada precede al ultimo caso
  adicional de `model`, validado despues con las suites focalizada y del
  comparador. La PR debe ejecutar tambien la suite general sobre su commit
  final en Linux.
- Flake8 fatal, compilacion Python, sintaxis Pylance y `git diff --check`:
  correctos. No hay errores de tipos en los componentes comunes consultados.
- El verificador local de locks devuelve 1 por finales CRLF del checkout
  Windows frente a LF regenerados. Los contenidos normalizados de ambos
  locks son identicos (53/62 CRLF frente a cero); no se han cambiado locks
  ni dependencias. Linux CI debe verificar sus bytes exactos.
- Docker no esta instalado localmente. El workflow existente de una PR
  puede construirlo en Linux: `push: false`, sin publicacion ni despliegue.

## Causa raiz demostrada

La fila real ya almacenada para `8056597875349` publica el titulo
`Prada Symbole PR 27ZS 16K08Z 54-21 Negro`, marca Prada, EAN
`8056597875349` e identificadores `PR27ZS` y `PR 27ZS 16K08Z 54-21`.
El campo estructurado de color esta vacio y el calibre extraido es `54-21`.
El catalogo contiene `PR 27ZS`, `16K08Z` y calibre vacio.

Reproduccion offline del parser anterior: modelo `SYMBOLE PR 27ZS 16K08Z`,
color `NEGRO`, calibre `54-21`. El parser solo buscaba modelos conocidos al
inicio de la referencia; al encontrar una coleccion comercial, caia en la
regla de ultima palabra como color. El piloto volvia a validar solo el titulo
y exigia un calibre del catalogo, aunque el EAN publicado fuese identico.
`compare_calibers('54', '54-21')` ya devuelve `equal`: no era un problema
de esa funcion, sino de un dato ausente y del criterio anterior.

## Nueva regla general

No hay excepcion por marca, producto ni tienda. Un EAN publicado valido e
identico al EAN del catalogo hace que el calibre sea **informativo**, tanto
si falta como si es diferente. Se registran ambas medidas, su comparacion y
la razon por la que no bloquean. No se completan ni modifican dimensiones
del catalogo. No se incorpora un formulario ni un proceso de confirmacion
de calibre: ya no es necesario para este caso.

Sin EAN publicado, una coincidencia exacta requiere marca, modelo y codigo de
color del fabricante. El calibre se conserva como dato descriptivo, pero no
participa en la identidad ni bloquea una coincidencia; no sustituye el codigo
de color.

Un EAN distinto nunca se acepta automaticamente. Si marca/modelo/color
coinciden, el resultado queda probable para revision manual; si tambien hay
contradicciones en esos atributos, se rechaza. Un EAN identico permite omitir
campos no publicados, pero cualquier contradiccion explicita mantiene el caso
en revision. Los nombres comerciales (`Negro`, `Nero`) nunca sustituyen un
codigo de fabricante. Las referencias incompletas sin EAN quedan probables,
no exactas.

Precio positivo, moneda/mercado comparables, IVA confirmado y disponibilidad
verificada siguen sujetos a las comprobaciones comerciales existentes. Una
coincidencia de identidad no garantiza una oferta comparable.

## Implementacion centralizada

- [Parser y clasificacion](../price_references.py): busca secuencias completas
  del modelo conocido en cualquier posicion, normaliza espacios/separadores
  sin eliminar ceros ni aceptar subcadenas de otros modelos. Extrae el codigo
  numerico/alfanumerico siguiente, sin incorporar palabras comerciales.
  Referencias multiples incompatibles requieren revision.
- [Extraccion](../price_web_extract.py): conserva separados titulo,
  identificadores y ambiguedad para la clasificacion compartida.
- [Buscador](../price_web.py): propaga identificadores y registra observaciones
  de calibre. No cambia robots, autorizaciones ni limites.
- [Importador](../price_comparator.py): verifica las declaraciones exactas con
  la misma regla y registra valores normalizados y observaciones en evidencia.
  Se reutiliza el historial y su deduplicacion. Las ofertas antiguas no se
  reescriben ni se reclasifican automaticamente.
- [JSON](../price_observations.py), [conectores](../price_connectors.py) y
  [planificador](../price_scheduler.py): conservan la evidencia de identidad.
  Un precio sin cambios pasa por el importador antes de actualizar la ultima
  comprobacion; no puede ocultar un EAN/color/modelo incorrectos nuevos.
- [Piloto](../price_pilot.py): usa la clasificacion comun, valida referencia
  exacta por EAN o marca/modelo/color y mantiene las validaciones comerciales
  de stock, EUR, IVA y mercado. La localizacion sigue requiriendo aprobacion
  humana antes de guardar una URL definitiva; no crea ofertas.
- [Interfaz](../app_pages/price_comparator.py): sustituye el aviso obsoleto de
  calibre obligatorio por la nueva regla.

Los codigos internos opacos de tiendas no se toman automaticamente como
modelos de fabricante. Evidencia incompleta o no interpretable queda en
revision, no se inventan atributos a partir del EAN o de la URL.

## Resultado esperado para el caso real

Con los atributos extraidos ya documentados y sin tocar el calibre vacio:

| Campo | Valor |
|---|---|
| Producto | Prada PR 27ZS 16K08Z |
| EAN publicado | 8056597875349, identico |
| Calibre catalogo / ficha | vacio / 54-21, informativo |
| Vytria / Visio Factory | 312 EUR / 239 EUR |
| Diferencia frente a Vytria | -73 EUR; -23,4 % |
| Mercado / IVA / stock | ES / incluido 21 % / disponible |
| Resultado | Comparable si todas esas evidencias estan verificadas |

El titulo y JSON original se conservan. Cada nuevo resultado del piloto
incluye `validation`: catalogo y ficha normalizados, `verified_ean`,
`caliber_informational`, `caliber_comparison`, razones y observaciones.
Los rechazos comerciales permanecen separados de las notas dimensionales.

## Pruebas y riesgos

[Regresiones offline](../tests/test_price_identity_regression.py): caso exacto
de produccion, PR27ZS/PR 27ZS, coleccion y color comercial, calibres vacios y
distintos, EAN ausente/invalido/distinto, color/modelo incorrectos, contradicciones
de identificadores, ambiguedad y estados comerciales no verificables.
Comprueban importacion JSON/SQLite, revalidacion antes de deduplicar,
historico intacto, 20 productos por seis tiendas y catalogo sin modificaciones.
Solo transporte simulado y bases temporales; no consultas reales.

Riesgo principal: un sitio puede publicar un EAN incorrecto. Un EAN
contradictorio obliga a revision incluso si el modelo/color parecen coincidir.
No se garantiza autenticidad de una ficha por su URL. Los codigos no
interpretables no se fuerzan a coincidir. Las diferencias de calibre se
auditan, pero no cambian la identidad.

Validacion local del comparador: **333 tests correctos, 1 omitido**, ejecutando
todos los archivos `tests/test_price*.py`. Lint de errores Python/imports y
`git diff --check` correctos.

Suite completa final: **1214 correctos, 15 omitidos y 1 fallo** en
`test_local_library_search_and_download_permissions` (fotografias de Kering,
ya observado antes de esta correccion). No se modifica ese modulo ni se
oculta el fallo: la suite local completa no se presenta como verde.

**CI/Docker pendientes**: la rama esta publicada, pero la PR aun no se ha
creado, segun confirmacion del usuario. GitKraken no tiene autorizacion para
crearla y GitHub no devuelve runs de esta rama. Docker no esta disponible
localmente; debe construirse en el workflow de la PR, sin publicar ni
desplegar la imagen. No se ha hecho merge ni despliegue.

## Despliegue y reversion

1. Revisar la PR independiente desde main y exigir CI completo, incluida
   construccion Docker. No se autorizan merge ni despliegue por esta entrega.
2. Antes de un despliegue autorizado, desactivar el piloto y esperar a que no
   haya workers. Conservar respaldo SQLite por el procedimiento existente.
3. Desplegar el commit aprobado sin activar programadores ni hacer consultas
   automaticas. No hay migracion de datos, calibres ni historicos.
4. Una consulta manual futura expresamente autorizada podra usar la nueva
   regla. Las observaciones antiguas rechazadas no se convierten en
   comparables retroactivamente; no repetir HTTP para validar esta PR.
5. Reversion: detener workers, revertir el commit de la PR y restaurar el
   codigo aprobado anterior. No borrar ofertas/historico ni cambiar la
   seleccion del piloto. El codigo anterior volvera a exigir calibre en el
   piloto y a usar el parser anterior; mantenerlo desactivado hasta revision.
