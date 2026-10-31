# Identidad del comparador y calibre informativo con EAN confirmado

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

Un EAN distinto rechaza la identidad, incluso si modelo, color y precio
coinciden. Un EAN ausente o no valido no recibe la excepcion. Se conserva la
via estricta de referencia completa (marca, modelo, color y calibre
verificados) en el comparador general; el piloto sigue exigiendo EAN publicado.

En ambas vias son imprescindibles marca, modelo y codigo de color correctos,
sin evidencia contradictoria o ambigua. Un EAN solo no rellena esos atributos
ni corrige contradicciones. Los nombres comerciales (`Negro`, `Nero`) nunca
sustituyen un codigo de fabricante. Una ficha antigua de Mia Burton sin
codigo publicado queda ahora en revision, aunque publique el EAN: es un
endurecimiento intencional que conserva el requisito de color.

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
- [Piloto](../price_pilot.py): usa la clasificacion comun, conserva la exigencia
  adicional de EAN publicado, muestra validacion normalizada, observaciones
  dimensionales y diferencia absoluta/porcentual. La lista de 20 EAN y los
  seis competidores no cambia.
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

Riesgo principal: un sitio puede publicar un EAN incorrecto. La coincidencia
del EAN no suprime la comprobacion de marca/modelo/color ni la ambiguedad.
No se garantiza autenticidad de una ficha por su URL. Los codigos no
interpretables no se fuerzan a coincidir. Las diferencias dimensionales se
auditan pero ya no descalifican un EAN verificado.

Validacion local del comparador: **333 tests correctos, 1 omitido**, ejecutando
todos los archivos `tests/test_price*.py`. Lint de errores Python/imports y
`git diff --check` correctos. La suite completa y el CI se registran al
finalizar; Docker no esta disponible localmente y debe comprobarse en la PR.

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
