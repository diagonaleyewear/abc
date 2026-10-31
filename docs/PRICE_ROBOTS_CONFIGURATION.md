# Comprobacion de robots.txt independiente del piloto

## Causa raiz

`WebPriceSearch.check_access()` aplicaba `_pilot_guard(None, competitor_id)`.
La proteccion comercial exige una ejecucion supervisada del piloto y no
admite consultas generales. Aplicarla a la configuracion de robots.txt
provocaba un bloqueo circular: faltaba comprobar el acceso de la tienda, pero
no era posible hacerlo fuera de una ejecucion del piloto.

## Solucion comun

La comprobacion de configuracion no concede un contexto de ejecucion del
piloto ni desactiva la barrera de EAN. Solo acepta el identificador de un
competidor registrado, recupera y valida su URL HTTPS contra su dominio
almacenado y solicita **el origen configurado + `/robots.txt`**. Rutas,
consultas y fragmentos de la URL base nunca se descargan. No hay parametro
de URL libre ni excepcion por tienda.

Funciona con piloto habilitado o desactivado, para las seis tiendas del
piloto, las otras ocho registradas y futuros competidores registrados. Los
permisos de acceso a la interfaz/configuracion existentes no cambian;
identificadores inexistentes se rechazan antes de HTTP.

La operacion:

- usa el bloqueo compartido por competidor para evitar consultas paralelas;
- conserva transporte HTTPS con validacion DNS de destinos publicos,
  identificacion del agente, timeout, pausa y limite de respuesta existentes;
- realiza como maximo una peticion, sin reintentos ni redirecciones, aunque
  la configuracion de consultas comerciales tenga limites superiores;
- rechaza HTML/productos como reglas y detecta controles antibot/CAPTCHA;
- conserva bloqueos HTTP/CAPTCHA anteriores: no insiste en sitios bloqueados;
- registra estado, fecha, resumen y errores en `competitor_web_config`;
- no llama al extractor, no descarga productos/sitemaps y no escribe ofertas,
  historico, EAN, presupuestos ni seleccion del piloto;
- no registra revision de condiciones ni activa planificadores.

`robots_allowed` describe reglas publicadas, no autorizacion comercial.
`robots_restricted` describe restriccion de la raiz. Si falta robots.txt,
hay error, redireccion o respuesta no valida, la comprobacion explicita queda
`unknown` / no disponible, no concede acceso. Las descargas comerciales
siguen comprobando robots.txt para cada ruta con su mecanismo existente.
La comprobacion de configuracion no sigue sitemaps ni plantillas de busqueda
al mostrarlos en el resumen.

## Archivos modificados

- [price_web.py](../price_web.py): operacion de configuracion registrada,
  validacion del origen, bloqueo compartido, persistencia de resultados.
  La verificacion manual de URL reutiliza el helper dentro del bloqueo ya
  adquirido; no hay desbloqueo general del piloto.
- [price_web_fetch.py](../price_web_fetch.py): `inspect_robots`, lectura
  aislada y limitada, sin redirecciones/reintentos/extraccion.
- [price_pilot.py](../price_pilot.py): mensaje explicito de consulta comercial
  bloqueada; limites y seleccion intactos.
- [app_pages/price_comparator.py](../app_pages/price_comparator.py):
  distingue resultado de configuracion, requisitos de autorizacion y consulta
  general bloqueada; sitios bloqueados mantienen el boton deshabilitado.
- [tests/test_price_robots_pilot.py](../tests/test_price_robots_pilot.py):
  regresiones de configuracion e aislamiento.
- [tests/test_price_pilot.py](../tests/test_price_pilot.py): la comprobacion
  especifica esta permitida, los intentos comerciales siguen rechazados.
- Guias del piloto y buscador actualizadas.

## Comprobacion manual en Cloud (solo tras autorizacion)

1. Abrir Comparador > Competidores > Configuracion web de cualquier tienda
   registrada, sin desactivar ni cambiar la seleccion del piloto.
2. Pulsar **Comprobar robots.txt**. Debe mostrarse
   **Comprobacion de acceso realizada. Resultado: permitido/restringido/no
   disponible/bloqueado**, con resumen y fecha persistidos.
3. Si falta la revision de condiciones, Cloud muestra:
   **Falta registrar la revision de condiciones de uso o completar la
   comprobacion de acceso**. Registrar condiciones es una decision humana
   independiente: robots.txt nunca basta.
4. Si el sitio esta bloqueado, no se reintenta; revisar manualmente la causa
   antes de cualquier intervencion. No eliminar bloqueos para forzar acceso.
5. Intentar una consulta general de productos debe seguir mostrando la
   necesidad de usar la prueba manual supervisada. El planificador periodico
   no ejecuta el piloto y este conserva sus 20 EAN y seis tiendas.

No se han realizado consultas reales ni cambiado SQLite de produccion para
validar esta correccion.

## Pruebas y CI

Las pruebas usan transporte simulado, bases temporales y Streamlit AppTest.
Cubren todos los 14 destinos, tiendas futuras, piloto habilitado/desactivado,
registrados excluidos, ID inexistente, URL/dominio invalidos, DNS interno,
permiso/restriccion, HTTP/timeout/CAPTCHA, redirecciones a productos o terceros,
presupuestos, bloqueos, ausencia de extraccion y consultas fuera del piloto.
Se comprueban integridad SQLite y tablas comerciales sin cambios.

El CI y build Docker deben verificarse en una PR independiente contra main.
En eventos pull_request el workflow construye sin publicar/desplegar.

Validacion local de esta rama:

- Regresiones nuevas de robots/configuracion: **40 passed**.
- Todos los tests del comparador: **373 passed, 1 skipped**.
- Comando de tests completo del CI: **1280 passed, 16 skipped, 10 subtests
  passed, 1 failed**. El fallo es
  `test_kering_image_sections.py::test_local_library_search_and_download_permissions`
  (validacion de fotografias), observado previamente en main durante las
  entregas anteriores; no se modifican esos componentes. El nuevo intento
  de contraste en un worktree limpio del commit base no obtuvo aprobacion
  de herramienta y no se ejecuto.
- Lint de errores/unused imports de archivos modificados, `py_compile` y
  `git diff --check`: correctos.
- Docker/CI remoto: **pendientes**. Docker y Podman no estan instalados en
  este entorno. No se presenta la suite global como verde ni se autoriza
  merge mientras no se revise el resultado de CI.

## Riesgos y rollback

Una tienda puede redirigir su robots.txt legitimo: deliberadamente no se
sigue, se informa no disponible para no convertir esta operacion en un
descargador arbitrario. Los bloqueos HTTP previos no se levantan.

El resultado de robots.txt puede quedar desactualizado: el acceso comercial
vuelve a comprobar reglas por ruta y mantiene autorizaciones y presupuestos.
La URL configurada esta limitada al dominio registrado; una configuracion
invalida falla explicitamente. Esta correccion no introduce nuevos permisos.

Rollback autorizado: desactivar el piloto, esperar a que terminen workers,
respaldar SQLite con el procedimiento existente y revertir el commit de esta
PR. No borrar ofertas ni tablas ni cambiar el alcance. La version anterior
volvera a bloquear robots.txt fuera del contexto del piloto; no compensarlo
desactivando su barrera comercial. No hacer merge ni desplegar sin permiso.
