# 🎯 Guía de Flujo Completo: Solicitud de Imágenes Luxoptica

## 🌐 Acceso a la Aplicación

1. **Abre tu navegador**
   - URL: `http://localhost:8501`

2. **Login en la App**
   - Usuario: `diagonal`
   - Contraseña: (como está configurada)

---

## 📊 Fase 1: Transformación MASTERDATA

### Paso 1: Ir a Masterdata
1. En la barra lateral, selecciona **"📊 ABCD"**
2. Luego en el submenú: **"📥 Importador Masterdata"**

### Paso 2: Subir Excel Origen
1. **Sección 1**: "Cargar Excel origen Luxottica"
2. Click en "Sube el Excel origen"
3. **Opción A**: Subir archivo nuevo
   - Buscar: `docs/MasterData/Prueba grande.xlsx`
   - O cualquier otro Excel de Luxottica

4. **Opción B**: Usar existente
   - La app mostrará archivos disponibles

### Paso 3: Revisar Advertencias
- ⚠️ La app mostrará:
  - Productos procesados
  - Registros exportados
  - Incidencias detectadas
  - Pérdida de ceros iniciales

### Paso 4: Descargar Resultados
**Sección 2**: "Descargar resultados"
- 📥 **Excel transformado** (.xlsx)
- 📊 **JSON de validación** (.json)
- 📋 **Resumen ejecutivo** (.md)
- 🔍 **Auditorías CSV**:
  - Descartes
  - Ceros iniciales
  - Marcas no resueltas

### Paso 5: Obtener los IDs de canales de venta desde el EAN

Cada gafa se identifica por su EAN, guardado en `product_product.barcode`.
Para obtener el ID de producto que usa cada canal de venta, se busca primero la
variante de Odoo y después sus registros en `diagonal_product_website`.

```sql
SELECT
      pp.barcode AS ean,
      pp.id AS product_id_odoo,
      dpw.name AS canal_venta,
      dpw.product_website_id AS id_producto_canal,
      dpw.published AS publicado,
      dpw.published_website_date AS fecha_subida,
      dpw.updated_website_date AS fecha_actualizacion,
      dpw.archived_website_date AS fecha_baja
FROM product_product pp
JOIN diagonal_product_website dpw ON dpw.product_id = pp.id
WHERE pp.barcode = '8056262672563'
ORDER BY dpw.siteweb_id;
```

Resultado comprobado para el EAN `8056262672563` (`RAY BAN WAYFARER RB2140 129431`):

| Canal de venta | ID de producto en el canal |
| --- | --- |
| Cettire | `8056262672563` |
| Poizon | `8056262672563` |
| Deipe | `S0017840` |
| Farfetch | `31715787` |
| Miinto | `70eb4458-0cdb-48be-99fc-afc72667624a` |
| Prestashop | `8056262672563` |

`diagonal_product_website.id` es el identificador interno del registro de
relación en Odoo. Para operaciones en el canal debe usarse
`diagonal_product_website.product_website_id`.

---

## 🎁 Fase 2: Solicitud de Imágenes Luxoptica

### Paso 1: Generar Lotes
**Sección 3**: "Solicitud de imágenes (Luxoptica)"

1. **Especificar email**:
   - Campo: "Email para la solicitud"
   - Valor: `images@diagonaleyewear.com` (ya predeterminado)

2. **Click**: "Generar archivos para pedir imágenes"
   - Sistema genera automáticamente lotes de 250 EAN máximo
   - Si hay 50 EAN: genera 1 archivo
   - Si hay 1000 EAN: genera 4 archivos

3. **Resultado**:
   - Se crean archivos `.txt` en `docs/Luxoptica/`
   - Formato: `upc-products-images-request-YYYYMMDD_HHMMSS-lote-XXX.txt`
   - Junto a cada lote se crea un manifiesto `.manifest.json` con EAN, modelo y color.
     Se usa internamente para identificar las imágenes recibidas.

### Paso 2: Descargar Lotes
1. Se muestra tabla con lotes generados
2. Para cada lote:
   - 📄 **Botón Descargar**: Obtener archivo .txt
   - ✅ **Botón Marcar procesado**: Cuando hayas enviado a Luxoptica

3. **El archivo contiene**:
   - 1 EAN por línea
   - Sin prefijo "es."
   - Listo para copiar/pegar en portal Luxoptica

---

## 📧 Fase 3: Enviar a Luxoptica

### Paso 1: Portal Luxoptica
1. Ir a: `https://portal.luxottica.com/` (o tu instancia)
2. Login con credenciales

### Paso 2: Solicitar Imágenes
1. **Servicios** → **Recursos Digitales** → **Imágenes de Producto**
2. Click **"Cargar archivo"**
3. Seleccionar el `.txt` descargado
4. **Opciones**:
   - ✅ Seleccionar "Todas las vistas"
   - ✅ Email: `images@diagonaleyewear.com`
5. Click **"Enviar solicitud"**

### Paso 3: Confirmar en App
1. Vuelve a **Solicitud de imágenes** en la app
2. Para cada lote enviado:
   - Click ✅ **"Marcar como procesado"**
   - App registra el estado

---

## 🤖 Fase 4: Descarga Automática (Sin Intervención)

### Lo que sucede automáticamente:
1. **Luxoptica procesa** la solicitud (24-48 horas)
2. **Luxoptica envía email** a `images@diagonaleyewear.com`
3. **Script Graph API** detecta el email cada hora
4. **Descarga automática** de adjuntos (imágenes)
5. **Organización**:
   ```
   repo/images/
   ├── 8056262672563/
   │   ├── 0VE4526U__108_71__P21__noshad__fr.png (original)
   │   ├── 0VE4526U__108_71__P21__noshad__qt.png (original)
   │   └── 0VE4526U__108_71__P21__shad__lt.png   (original)
   └── commercial_exports/
       ├── Farfetch/8056262672563/
       │   ├── 31715787_V1.png
       │   ├── 31715787_V2.png
       │   └── 31715787_V3.png
       └── Miinto/8056262672563/
           ├── <id-miinto>_V1.jpeg
           ├── <id-miinto>_V2.jpeg
           └── <id-miinto>_V3.jpeg
   ```

   Los originales se conservan en `<EAN>/`; las publicaciones comerciales
   son copias separadas y no rompen la deduplicación del repositorio. Los IDs
   se consultan en Odoo por EAN. V1/V2/V3 corresponden a frontal,
   perspectiva y lateral. Solo se publican vistas acreditadas. Miinto conserva
   sus bytes PNG originales bajo la extensión histórica `.jpeg`; los originales
   no se renombran ni se modifican.

   Para reconstruir archivos comerciales existentes, primero ejecutar
   `python image_exports.py --dry-run`; revisar IDs, vistas, conflictos y
   espacio estimado. Solo tras aprobarlo se ejecuta `python image_exports.py
   --apply`. El comando usa IDs vigentes de Odoo y no sobrescribe destinos en
   conflicto.

### Monitoreo:
- Ver archivo: `docs/Luxoptica/descargas/.mail_download_state.json`
- Contiene: IDs de correos procesados, adjuntos descargados

---

## ✅ Checklist - Flujo Completo

### En la Aplicación:
- [ ] Login exitoso
- [ ] Ir a Masterdata
- [ ] Subir Excel origen
- [ ] Revisar advertencias de transformación
- [ ] Descargar Excel transformado + auditorías
- [ ] Especificar email: images@diagonaleyewear.com
- [ ] Click "Generar archivos"
- [ ] Descargar archivo .txt del lote

### Manual (Luxoptica Portal):
- [ ] Acceder a portal Luxoptica
- [ ] Cargar archivo .txt
- [ ] Seleccionar "Todas las vistas"
- [ ] Especificar email
- [ ] Enviar solicitud

### En la Aplicación (Confirmación):
- [ ] Marcar lote como "Procesado"

### Automático (Graph API):
- [ ] Esperar respuesta de Luxoptica (24-48h)
- [ ] Verificar `docs/Luxoptica/descargas/`
- [ ] Imágenes descargadas y organizadas ✅

---

## 🐛 Troubleshooting

### La app no carga
```powershell
# Verificar que venv está activado
& ".\.venv\Scripts\Activate.ps1"

# Verificar que requirements están instalados
pip install -r requirements.txt

# Iniciar app
streamlit run app_enhanced.py
```

### No puedo generar lotes
- Verificar que hay datos en la transformación
- Verificar que no hay errores críticos en advertencias

### Descarga de imágenes no funciona
- Verificar que .env tiene credenciales Graph API
- Ejecutar: `python test_graph_api_config.py`
- Esperar 5-10 minutos después de otorgar permisos

---

## 📞 Soporte Rápido

**Archivo de configuración**: `.env`
**Variables M365 requeridas**:
```env
M365_TENANT_ID=...
M365_CLIENT_ID=...
M365_CLIENT_SECRET=...
M365_MAILBOX=images@diagonaleyewear.com
M365_DOWNLOAD_ROOT=docs/Luxoptica/descargas
```

**Logs útiles**:
- App: Terminal donde arrancó streamlit
- Descargas: `docs/Luxoptica/descargas/.mail_download_state.json`
- Transformación: `docs/MasterData/*.json` (reporte de validación)

---

## 🎯 Resumen del Flujo

```
┌─────────────────────────────────────────────────┐
│  1. SUBIR EXCEL LUXOTTICA                       │
│     ↓                                           │
│  2. TRANSFORMAR A MASTERDATA                    │
│     ↓                                           │
│  3. GENERAR LOTES (250 EAN máx)                 │
│     ↓                                           │
│  4. DESCARGAR ARCHIVO .TXT                      │
│     ↓                                           │
│  5. ENVIAR A LUXOPTICA (manual)                 │
│     ↓                                           │
│  6. MARCAR COMO PROCESADO (app)                 │
│     ↓                                           │
│  7. ESPERAR RESPUESTA LUXOPTICA (24-48h)        │
│     ↓                                           │
│  8. DESCARGA AUTOMÁTICA (Graph API)             │
│     ↓                                           │
│  9. IMÁGENES EN docs/Luxoptica/descargas/       │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Tiempo total**: ~5-10 minutos (pasos 1-6) + 24-48h respuesta + 5 minutos descarga automática
