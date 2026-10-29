# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y [SemVer](https://semver.org/lang/es/).
Solo se listan cambios realmente incorporados a `main`. Que un cambio esté aquí **no** significa que esté
desplegado: el estado de cada versión (preparada, publicada, fallida, rollback) consta en
[release-history.json](release-history.json) y solo cambia con evidencia del flujo de publicación/controlador.

## [0.1.0] — preparada (sin etiqueta, sin despliegue confirmado)

Versión inicial: resume el historial real del repositorio (junio–octubre 2026, sin etiquetas ni releases
previos; la versión fija `1.0.6` retirada anteriormente nunca estuvo respaldada por un mecanismo de publicación).
Se usa `0.x` porque no se ha declarado una API estable.

### Añadido
- Exportación Excel de solicitudes Marcolin con EAN/UPC como texto y SKU completo solo con correspondencia única
  en Odoo (PR #81). *Incorporado al código; no se marca como desplegado hasta que el controlador de producción
  confirme la publicación.*
- Imágenes de proveedores (Marcolin, Safilo, NuOrder) y piloto Marcolin (#59, PR #78–#80), con persistencia de
  solicitudes en el volumen de datos.
- Biblioteca de imágenes, miniaturas, sistema de diseño y ZIP de pedidos desde búsqueda (PR #38–#58).
- Imágenes y pedidos de compra de Kering con procesamiento programado (PR #36, #37, #43, #47).
- Repositorio unificado de imágenes por EAN y preparación para NFS Synology (PR #39, #40).
- Panel de procesos de Cloud y despliegue controlado de producción con verificación de digest, commit
  autorizado y ancestros (PR #48–#65).
- Identidad de artefacto (`build-info.json`, etiquetas OCI) y control de versiones SemVer con `VERSION`.

### Corregido
- Resiliencia de Microsoft Graph y contrato de snapshots (PR #30–#32, #60, #61).
- Obligatoriedad de secreto de sesión (PR #63).
- Histórico Kering con valores `NaN` (PR #42).
- Módulos de imágenes de proveedores en la imagen de runtime (PR #78).

### Modificado
- Modularización de la aplicación (PR #33).
- El pie de página muestra `Cloud vX.Y.Z · commit abc1234`; «Información de versión» en Configuración.
