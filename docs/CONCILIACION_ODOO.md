# Conciliacion Odoo: Adyen

## Alcance y uso

La navegacion de Streamlit incluye **Conciliacion > Conciliacion Odoo** solo para
administradores. La pantalla comprueba tambien el rol al acceder directamente.
No utiliza la conexion a Odoo, no crea asientos y no concilia automaticamente.

1. Subir el informe `settlement_detail_report_batch_*.xls`, XLSX o CSV.
2. Opcionalmente subir facturas y abonos Odoo con Numero, Nombre del socio a
   mostrar en la factura, Fecha de Factura/Recibo, Total en divisa firmado y Moneda.
   Se aceptan los nombres originales en castellano (incluidos los acentos).
3. Para confirmar asociaciones, exportar tambien Referencia PSP o Referencia de
   pedido (alternativas: PSP Reference, Merchant Reference, Origen o Documento origen).
   Mantener referencias como texto para que Excel no pierda digitos.
4. Pulsar **Procesar**, revisar errores, movimientos, gastos, totales y documentos.
5. Descargar los CSV individualmente y `revision.csv`, o todos juntos en ZIP.
   No importar el archivo de revision como movimientos.

Cambiar o retirar cualquiera de los archivos invalida resultados y descargas,
incluso cuando el nuevo archivo tiene el mismo nombre. Los datos se procesan en
memoria de sesion; este modulo no los guarda en disco ni los registra en logs.
Las pruebas solo emplean datos sinteticos.

## Lectura y validacion

- XLS binario antiguo: `xlrd==2.0.2`; XLSX: `openpyxl`. No se interpretan HTML o
  CSV renombrados como XLS. Se lee la hoja activa XLSX / primera hoja XLS.
- CSV: UTF-8 con o sin BOM, o CP1252; delimitador coma, punto y coma o tabulador.
- Cabecera en la primera fila no vacia. Se conserva el numero de fila fisico,
  incluidas filas vacias intermedias. No se deduplica por PSP, pedido o importe.
- Fecha ISO o dia/mes/ano, con hora opcional. No se convierte la zona horaria.
- Importes sin separadores de miles; punto o coma decimal. Los importes Excel
  se leen como numeros y se convierten a Decimal mediante su representacion textual.
- Columnas Adyen: Merchant Account, Batch Number, Booking Date (o Creation Date),
  Type, Currency (o Net Currency), Net Credit (NC), Net Debit (NC).
  Settled y Refunded requieren Gross Credit (GC) / Gross Debit (GC) y Gross
  (o Gross Currency). **Gross es moneda original; Currency es moneda de liquidacion.**
- Tipos admitidos: Settled, Refunded, Fee, InvoiceDeduction, MerchantPayout.
  Un tipo desconocido o una fila invalida bloquea todos los CSV, no produce una
  importacion parcial. Los errores muestran origen, fila y motivo; la vista previa
  y sus totales pueden contener solo las filas validas en ese caso.
- No se admiten formulas sin valor calculado, fechas ambiguas mes/dia/ano ni
  cambios manuales de moneda o tipos de cambio inventados.

## CSV contables

Un CSV por **cuenta + lote + moneda de liquidacion**. El nombre incluye esas tres
claves y un identificador corto que evita colisiones al normalizar nombres.
Columnas: `Fecha;Concepto;Importe`. Codificacion UTF-8 con BOM, decimal punto sin
miles, fecha YYYY-MM-DD. No hay filas de resumen. Se conserva cada movimiento.

`Importe = Net Credit (NC) - Net Debit (NC)`, calculado con Decimal. MerchantPayout
con debito neto conserva el signo negativo. Ni las comisiones de los cobros ni
Commission (NC) de Fee se suman otra vez al neto. Commission, Markup, Scheme Fees
e Interchange se muestran individualmente en concepto y vista previa como gastos
informados; no se agregan entre si porque pueden ser desglose y total del mismo gasto.

El concepto contiene cuenta, lote, tipo, PSP si existe, pedido si existe y fila
original. Si las monedas difieren, conserva bruto firmado y moneda original,
pero exporta el neto en la moneda de liquidacion, sin calcular cambio.

## Cruce conservador

La clave inicial es **bruto firmado Adyen + moneda original**, comparada con
**total en divisa firmado Odoo + moneda**. No se compara neto ni moneda de
liquidacion con la factura. Solo los cobros y devoluciones buscan documentos.

- Coincidencia uno a uno solo por importe y moneda: candidata, nunca confirmada.
- Varios documentos o varios movimientos con ese importe: ambiguos.
- Una referencia PSP o de pedido compartida puede desambiguar y confirmar,
  exclusivamente cuando la referencia y el importe/moneda identifican una pareja
  uno a uno. No se aplican coincidencias parciales de referencias.
- Solo la confirmacion por referencia anade el numero de documento al concepto.
- Cobro y abono con el mismo PSP se conservan y comparan por sus importes firmados.
- `revision.csv` incluye ambas perspectivas (Adyen y Odoo), filas relacionadas,
  candidatos, ambiguos, sin coincidencia y confirmados para trazabilidad.
  Los conteos de documentos ambiguos no son conteos de movimientos ambiguos.
- El exportado no demuestra que una factura este cobrada. Una referencia confirma
  la asociacion documental, no la conciliacion bancaria ni el estado de pago.

## Importacion y compatibilidad Odoo

En Odoo usar el **diario bancario de la pasarela Adyen**, correspondiente a la
cuenta, no el diario del banco que recibe MerchantPayout. Elegir un diario cuya
moneda sea la moneda de liquidacion de ese CSV: EUR para EUR y USD para USD.
No importar un CSV USD en un diario EUR como si fueran euros. La transferencia
posterior al banco receptor y su conciliacion siguen siendo tareas manuales.

Desde las transacciones del diario, importar registros / importar archivo segun
la version y modulos instalados. Configurar UTF-8, separador `;`, decimal `.` y:

| Columna CSV | Campo Odoo |
| --- | --- |
| Fecha | Fecha |
| Concepto | Etiqueta / Concepto |
| Importe | Importe |

Ejecutar **Probar** antes de **Importar**. Comprobar numero de filas, fechas,
signos, saldo, moneda del diario y ausencia de importaciones previas del mismo
lote. Los nombres de campo traducidos pueden variar. No importar dos veces un lote.

La documentacion oficial de **Odoo 18** confirma CSV, mapeo de columnas y los
pasos Test / Import:
https://www.odoo.com/documentation/18.0/applications/finance/accounting/bank/transactions.html

**La version utilizada por esta instalacion no consta en el repositorio y no
se ha probado esta importacion en ella. Compatibilidad operativa pendiente.**
Antes de aprobar uso real: identificar version, edicion y modulos contables;
seguir la documentacion de esa version; probar en una copia con datos sinteticos
de EUR y USD; comprobar que MerchantPayout es negativo y los importes/fechas se
conservan. Registrar aqui version y resultados tras esa prueba. La referencia a
Odoo 18 es evidencia documental, no una certificacion de esta instalacion.

## Verificacion

Pruebas enfocadas:
`python -m pytest -q tests/test_adyen_reconciliation.py`

Suite offline utilizada por CI:
`python -m pytest -q tests test_transform_luxottica_masterdata.py test_validate_masterdata_odoo_dryrun.py`

Los locks se regeneran con uv 0.12.22 para Python 3.11 / Linux, como CI.
Docker copia ambos modulos y instala xlrd desde requirements.txt; xlwt solo es
dependencia de desarrollo para construir XLS sinteticos. La CI construye la
imagen de las PR sin publicar ni desplegar. No hay Docker disponible localmente,
por lo que la construccion local no se ha probado.

## Ejemplo solicitado: pendiente de archivos

No estan disponibles en el workspace el informe adjunto ni VYTRIA ENERO.xlsx.
No se afirma haberlos procesado. Estos son los criterios de aceptacion pendientes:

| Lote 100 | EUR | USD |
| --- | --- | --- |
| Movimientos | 106 | 19 |
| MerchantPayout | -9550.45 | -4550.51 |
| Saldo neto | 0.00 | 0.00 |

Total 125 movimientos; conservar seis cobros originales GBP liquidados en EUR.
Cruce por bruto firmado y moneda original, sin referencias: 97 documentos
(86 facturas, 11 abonos), 51 candidatos unicos, 24 documentos ambiguos,
22 documentos sin coincidencia exacta y 20 movimientos Adyen sin documento.
Las pruebas sinteticas reproducen la topologia de candidatos/ambiguedades para
comprobar conteos, pero no sustituyen la ejecucion del ejemplo real.

Los archivos reales deben facilitarse por un canal autorizado y mantenerse
fuera de fixtures, control de versiones y logs. Publicar solo los resultados
agregados de validacion y la version de Odoo, nunca referencias o datos financieros.