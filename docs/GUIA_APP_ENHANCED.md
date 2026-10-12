# 📦 Control ABCD de Productos - Guía de Uso

## ¿Qué es esta aplicación?

Una interfaz web (Streamlit) para gestionar y analizar la clasificación ABCD de 13,454 productos de Diagonal Eyewear, considerando:
- **Demanda real** (ventas últimos 180 días)
- **Períodos de agotamiento** (stock = 0)
- **Valor económico** (PVO y capital bloqueado)

## 🚀 Cómo ejecutar

```bash
cd "c:\Users\Ruben\OneDrive - Diagonal Eyewear\Proyectos\ABC"
streamlit run app_enhanced.py
```

Abre **http://localhost:8501** en tu navegador.

---

## 📋 Funcionalidades por Sección

### **1. 📊 INICIO (Página por defecto)**

**KPIs principales:**
- **Total Productos**: 13,454 (con stock actual)
- **Capital Bloqueado**: €3,859,335 (dinero invertido en inventario)
- **Categoría A**: 1,100 (Alta demanda - REPONER)
- **Categoría D**: 4,565 (Sin demanda - LIQUIDAR)

**Gráficos:**
- **Distribución por Categoría** (pastel): % de productos por ABCD
- **Capital Bloqueado por Categoría** (barras): Dinero invertido por clase

**Tabla Resumen:**
- Cuenta de productos por categoría
- Stock total acumulado
- Capital bloqueado
- PVO promedio

---

### **2. 🔍 BUSCAR PRODUCTO**

**Uso:**
1. Escribe en el campo de búsqueda: nombre, código de barras o ID
2. Haz clic en "🔍 Buscar"

**Resultados:**
- Si hay **1 solo resultado**: Se muestra el detalle completo
- Si hay **múltiples resultados**: Se muestra tabla con los primeros 10

**Detalle de Producto (resultado único):**

| Sección | Información |
|---------|-------------|
| **Métricas** | ID, Clasificación ABCD, Stock actual, Capital bloqueado |
| **Comercial** | Código de barras, PVO, fechas de compra/venta |
| **Actividad** | Última venta, ventas en 180 días, última reposición |
| **Agotamientos** | Lista de períodos con stock = 0 (inicio, fin, duración) |

**Ejemplo búsqueda:**
```
Buscar: "DIOR"
↓
Resultado: DIOR DIORPACIFIC S3I 10A0
Clasificación: A (REPONER)
Stock: 5 unidades
Capital: €12,543
Períodos de agotamiento: 2 períodos (102 días total)
```

---

### **3. 📈 REPORTES ABCD**

**Filtros disponibles:**
- **Categoría ABCD**: Selecciona A, B, C, D (multi-select)
- **Capital mínimo**: Solo productos con capital ≥ X€
- **Mostrar máximo**: Limita resultados (10, 20, 50...)

**3 Vistas:**

#### **Vista Tabla** 📊
- Todos los productos filtrados ordenados por capital
- Columnas: Marca, ABCD, Stock, PVO, Capital, Ventas 180d, Última Venta, Acción

#### **Vista Gráficos** 📈
- **Stock por Categoría**: Barras con total de unidades
- **Top 10 Capital**: Productos que más dinero tienen bloqueado

#### **Vista Descargar** 💾
- Botón para descargar CSV con los resultados
- Nombre: `abcd_report_YYYYMMDD_HHMMSS.csv`

**Ejemplo filtro:**
```
Mostrar: Categoría D
Capital mínimo: €500
Máximo: 30 productos
↓
Resultado: 30 productos para liquidar, ordenados por capital
```

---

### **4. 📉 ANÁLISIS DETALLADO**

**Uso:**
1. Selecciona un producto del dropdown (búsqueda automática mientras escribes)
2. Ve el análisis completo

**Contenido:**

| Elemento | Descripción |
|----------|------------|
| **Métricas** | ABCD, Stock, PVO, Capital, Ventas 180d |
| **Histórico** | Primera compra, última compra, última venta, última reposición |
| **Clasificación** | Motivo de la clasificación, alertas, acción recomendada |
| **Agotamientos** | Tarjetas mostrando cada período sin stock |

**Ejemplo:**
```
Producto: DIOR DIORPACIFIC S3I 10A0
ABCD: A
Motivo: Demanda probada en últimos 180 días
Acción: REPONER - Producto con demanda constante
Períodos de agotamiento:
  Período 1: 01/11/2024 → 15/12/2024 (45 días)
  Período 2: 20/01/2025 → 28/02/2025 (39 días)
```

---

## 📊 Clasificación ABCD Explicada

### **Reglas de Clasificación**

| Categoría | Criterio | Acción | Ejemplo |
|-----------|----------|--------|---------|
| **A** | Demanda en últimos 180 días + Valor alto | ✅ REPONER | Gafas con múltiples ventas |
| **B** | Demanda en últimos 180 días + Valor medio | 📌 MANTENER | Gafas con ventas moderadas |
| **C** | **60+ días sin ventas** (excluyendo agotamientos) | ⚠️ REVISAR | Baja demanda, posible liquidar |
| **D** | **180+ días sin ventas** AND sin demanda 180d | 🔴 LIQUIDAR | Producto obsoleto |

### **Ventanas de Agotamiento**

El sistema es inteligente: **No cuenta días sin stock como "sin demanda"**

```
Ejemplo:
- 01/10: Última venta
- 05/10: Stock agotado (última compra 15/09)
- 20/10: Se repone inventario
- 30/10: Nueva venta

Cálculo:
  - Días sin venta aparente: 60 días (01/10 → 30/10)
  - Días REALES sin demanda: 26 días (04/10 → 30/10)
  - Motivo: 05/10 a 20/10 el producto estaba AGOTADO
  
Resultado: NO es categoría C (necesita 60+ días)
```

---

## 🔧 Características Técnicas

### **Cache de Datos**
- Datos actualizados cada **5 minutos**
- Reduce carga a la BD
- Búsquedas rápidas

### **Períodos de Agotamiento**
- Consultados en tiempo real desde la BD
- Calculados a partir de `stock_move` y `stock_quant`
- Mostrados en formato legible (fechas, duración en días)

### **Exportación**
- Formato CSV estándar
- Timestamp en nombre de archivo
- Abre en Excel/Sheets sin problemas

### **Responsive**
- Funciona en desktop y tablet
- Layout dinámico (ancho adaptable)
- Gráficos interactivos (Plotly)

---

## 💡 Casos de Uso

### **Caso 1: Revisar un producto específico**
```
Gerente: "¿Por qué el DIOR está como D?"
Acción: 
  1. Ir a "Buscar Producto"
  2. Escribir "DIOR"
  3. Ver: Clasificación A, períodos de agotamiento, ventas 180d
Resultado: Explicar por qué fue mal clasificado antes
```

### **Caso 2: Identificar productos para liquidar**
```
Gerente: "Muéstrame los D con más capital bloqueado"
Acción:
  1. Ir a "Reportes ABCD"
  2. Filtrar: Categoría D
  3. Ordenar por Capital
  4. Descargar CSV
Resultado: Lista de 4,565 productos D, ordenables por capital
```

### **Caso 3: Auditoría de categoría C**
```
Gerente: "¿Cuántos productos C hay? ¿Son realmente de baja demanda?"
Acción:
  1. Ir a "Reportes ABCD"
  2. Filtrar: Categoría C
  3. Ver gráficos de stock
  4. Analizar los Top 10 en capital
Resultado: Decisión de qué revisar, qué liquidar
```

### **Caso 4: Análisis de un producto problemático**
```
Comprador: "El SKU 12345 lleva 3 meses sin venderse"
Acción:
  1. Ir a "Análisis Detallado"
  2. Buscar producto
  3. Ver histórico completo
  4. Ver períodos de agotamiento
Resultado: Entender si es falta de demanda o de stock
```

---

## 🐛 Solución de Problemas

### **"No se encuentra el producto"**
- Verifica que sea el nombre exacto en Odoo
- Intenta con el código de barras
- Intenta con parte del nombre (ej: "DIOR" en lugar de "DIOR PACIFIC")

### **"Los datos no se actualizan"**
- Espera 5 minutos (tiempo de cache)
- O recarga la página (F5)
- O reinicia Streamlit: `Ctrl+C` y ejecuta de nuevo

### **"Error de conexión a BD"**
- Verifica que `db_config.py` tenga credenciales correctas
- Revisa que PostgreSQL esté corriendo
- Checkea el acceso a red desde tu IP

### **"Los gráficos no se muestran"**
- Recarga la página
- Verifica que Plotly esté instalado: `pip install plotly`

---

## 📞 Contacto

Para reportar bugs o sugerencias:
1. Prueba primero en otra pestaña del navegador
2. Si persiste, guarda un screenshot
3. Verifica los logs en terminal de Streamlit

---

## 📅 Changelog

**v1.0 - 23/06/2025**
- ✅ Página de inicio con KPIs
- ✅ Búsqueda de productos
- ✅ Reportes ABCD con filtros
- ✅ Análisis detallado
- ✅ Períodos de agotamiento
- ✅ Exportación CSV
- ✅ Cache de datos

**Pendiente para v1.1**
- ⏳ Dashboard con tendencias
- ⏳ Alertas de reposición
- ⏳ Predicción de demanda
- ⏳ Multi-usuario con permisos
