# Cloud: identidad visual corporativa

## Alcance

Cambio exclusivamente visual. Se conservan las rutas, los grupos de navegación,
los permisos, los formularios, las tablas y las operaciones existentes. No se
modifican Odoo, SQLite, proveedores, jobs ni autenticación.

La cabecera A fue seleccionada por el usuario tras presentar capturas de A y B.
A alinea «Cloud» y el subtítulo a la izquierda y el logotipo original a la derecha,
en una fila compacta.
El bloque de texto usa «DIAGONAL EYEWEAR», «Cloud» y
«Gestión inteligente, información conectada.», siguiendo la referencia elegida.
B mantiene una alternativa modular con superficie neutra y separador. Ambas
usan los mismos tokens y se pueden reproducir con el harness visual.

## Fuente única y compatibilidad

- [Configuración nativa](../.streamlit/config.toml): fuente única del verde de
  interacción `#417B7B`, su variante tonal oscura `#83B9B9` y la familia Inter.
  La variante oscura tiene una función de accesibilidad, no identifica otra marca.
- [Sistema de diseño](../cloud_design.py): lee esos valores para los componentes
  propios, las cabeceras, los estados y los adaptadores nativos.
- Botones, radios, checkboxes, selectores, pestañas, indicadores y tablas siguen
  usando el tema nativo de Streamlit 1.64.0. No se cambian sus dimensiones.
- Los adaptadores CSS se limitan a enlaces y encabezados del sidebar, etiquetas
  seleccionadas y contraste de texto/checkmarks sobre rellenos primarios.
  No se usan nombres de clases generados por Emotion ni JavaScript en producción.
- El parche de etiquetas exclusivo del comparador se elimina. Sus gráficos usan
  el mismo verde importado del sistema de diseño; se conservan las series
  categóricas y los colores que expresan resultados, advertencias o errores.
- Los deshabilitados conservan colores neutros; los estados semánticos siguen
  diferenciándose mediante texto, además del color.
- El destino activo del sidebar usa texto e icono verdes y subrayado, sin fondo
  sombreado. El hover cambia el color sin rellenar el fondo.
- Docker copia exactamente la configuración y las fuentes del proyecto; ya no
  mantiene un segundo literal del verde.

## Tipografía e iconografía

Inter variable se aloja localmente en [static/fonts](../static/fonts), sin
peticiones a servicios externos en ejecución. Se distribuye sin modificar junto
con su licencia SIL OFL 1.1.

Origen: distribución oficial de [Google Fonts / Inter](https://github.com/google/fonts/tree/main/ofl/inter).
SHA-256 del archivo: `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031`.

Todos los destinos de navegación usan Material Symbols de Streamlit, una única
familia vectorial monocroma nativa. Se eligió frente a Lucide porque `st.Page`
admite los identificadores `:material/...:` sin componentes ni dependencias
adicionales. Nombres, rutas y visibilidad permanecen intactos.
Los mapas de iconos están centralizados en `NAV_ICONS`.

## Reproducir la revisión visual

```powershell
python scripts\cloud_identity_visual_harness.py C:\capturas\cloud --label after
```

El harness usa páginas reales de portada, biblioteca, comparador y proveedores
(Marcolin, NuORDER y Safilo), además de una galería de controles nativos. Usa el
constructor real de navegación y extrae los metadatos de los destinos de
producción. El contenido de la sesión es sintético; no es una sesión autenticada
de producción ni una ejecución de los procesos de esos destinos.

Todo el estado del harness se crea en una carpeta temporal. No se consultan
Odoo ni portales, ni se arrancan schedulers. Los servidores se enlazan únicamente
a loopback y se cierran al terminar, incluso si falla una captura.

Se capturan escritorio (1440 px) y móvil (375 px), en claro y oscuro.
El informe JSON comprueba fondo real del tema, carga efectiva de Inter, colores
calculados de botones y navegación, contraste y desbordamiento horizontal.
La galería también prueba checkbox, radio y cambio de pestaña.

Para reproducir el antes se usa `--label before --source C:\ruta\worktree-base`
contra un worktree limpio de `1abc90a`. No simula la apariencia antigua con CSS.

## Validación

Los resultados y las capturas finales se adjuntan a la entrega/PR. Los tests
incluyen contraste WCAG AA en ambos temas, integridad del logotipo, rutas e
iconos de todos los destinos, licencia/fuente y empaquetado del tema en Docker.

La construcción Docker se verifica mediante el job existente de PR, que construye
sin publicar ni desplegar. Este equipo no dispone de Docker ni de WSL instalado;
una validación local de sintaxis o empaquetado no sustituye ese build.

### Resultado local (30/10/2026)

- Diseño revisado y validado por el usuario en la preview aislada.
- 106 tests afectados correctos.
- Suite de CI local: 1158 correctos, 16 omitidos, 1 fallo.
  `test_local_library_search_and_download_permissions` falla al validar una
  fotografía. Se reprodujo el mismo fallo ejecutando ese test en el worktree
  limpio de `main` (`1abc90a`); no se cambia el procesamiento de imágenes.
- 28 capturas del antes y 36 del después: ambos temas, escritorio y móvil,
  sin desbordamientos horizontales. Inter realmente cargada y contraste de
  botones principales >= 4.5:1 en ambas paletas.
- Lint de errores críticos, compilación Python y `git diff --check` correctos.
- Build Docker local no ejecutable: herramienta y WSL no instalados.
  El resultado del build de CI se consulta en la PR; no se presupone correcto.

### Capturas seleccionadas

| Vista | Antes | Después |
|---|---|---|
| Portada / sidebar, claro | [Antes](visual-identity/before-light-home-desktop.png) | [Validado](visual-identity/after-light-home-desktop.png) |
| Portada / sidebar, oscuro | [Antes](visual-identity/before-dark-home-desktop.png) | [Validado](visual-identity/after-dark-home-desktop.png) |
| Portada móvil | [Antes](visual-identity/before-light-home-mobile.png) | [Validado](visual-identity/after-light-home-mobile.png) |
| Comparador, claro | [Antes](visual-identity/before-light-comparator-desktop.png) | [Después](visual-identity/after-light-comparator-desktop.png) |
| Comparador, oscuro | [Antes](visual-identity/before-dark-comparator-desktop.png) | [Después](visual-identity/after-dark-comparator-desktop.png) |

- [Alternativa A final](visual-identity/after-light-a-desktop.png).
- [Alternativa B](visual-identity/after-light-b-desktop.png).
- [Biblioteca](visual-identity/after-light-library-desktop.png).
- [Marcolin](visual-identity/after-light-marcolin-desktop.png),
  [NuORDER](visual-identity/after-light-nuorder-desktop.png),
  [Safilo](visual-identity/after-light-safilo-desktop.png).
- [Controles y estados en oscuro](visual-identity/after-dark-controls-desktop.png).

Las capturas usan datos sintéticos. ABC, Master Data, conciliación y configuración
comparten los estilos comprobados y sus rutas se verifican por tests, pero el
harness no ejecuta la funcionalidad completa de esos módulos.
