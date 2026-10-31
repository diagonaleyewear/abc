from __future__ import annotations

from io import BytesIO
from pathlib import Path
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text

from db_loader import load_odoo_dataframe
from engine import load_input_file, run_abcd_engine
from db_config import load_db_config
from cloud_design import inject_styles, palette

# Configurar página
st.set_page_config(
    page_title="Control ABCD de Productos",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)
inject_styles()
colours = palette("auto")

# Estilos personalizados
st.markdown(f"""
    <style>
    .metric-card {{
        background-color: {colours['neutral']};
        padding: 20px;
        border-radius: 10px;
        margin: 10px 0;
    }}
    .status-a {{ color: {colours['success']}; font-weight: bold; }}
    .status-b {{ color: {colours['warning']}; font-weight: bold; }}
    .status-c {{ color: {colours['accent']}; font-weight: bold; }}
    .status-d {{ color: {colours['danger']}; font-weight: bold; }}
    </style>
""", unsafe_allow_html=True)

# Título principal
col1, col2 = st.columns([3, 1])
with col1:
    st.title("📦 Control ABCD de Productos")
with col2:
    st.write(f"📅 {datetime.now().strftime('%d/%m/%Y')}")

# Sidebar
st.sidebar.header("⚙️ Configuración")

# Cargar datos
@st.cache_data
def load_data_cached():
    return load_odoo_dataframe()

# Fuente de datos
data_source = st.sidebar.radio(
    "Origen de datos",
    ["Base de Datos", "Archivo Excel"],
    help="Selecciona la fuente de datos"
)

if data_source == "Base de Datos":
    df_raw = load_data_cached()
else:
    uploaded_file = st.sidebar.file_uploader("Subir archivo Excel", type=['xlsx'])
    if uploaded_file:
        df_raw = load_input_file(BytesIO(uploaded_file.read()))
    else:
        df_raw = None

if df_raw is not None:
    # Parámetros del motor ABCD
    st.sidebar.subheader("Parámetros ABCD")
    ref_date = st.sidebar.date_input(
        "Fecha de referencia",
        datetime.now(),
        help="Fecha para calcular la clasificación"
    )
    
    # Ejecutar clasificación
    df_abcd = run_abcd_engine(df_raw.copy(), reference_date=pd.Timestamp(ref_date))
    
    # Tabs principales
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Dashboard",
        "🔍 Búsqueda Producto",
        "📈 Análisis Detallado",
        "📋 Reportes",
        "⚙️ Configuración"
    ])
    
    # TAB 1: Dashboard
    with tab1:
        st.header("Dashboard ABCD")
        
        # Estadísticas principales
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric(
                "Categoría A",
                len(df_abcd[df_abcd['ABCD'] == 'A']),
                "Reponer/Priorizar",
                help="Productos probados con ventas activas"
            )
        
        with col2:
            st.metric(
                "Categoría B",
                len(df_abcd[df_abcd['ABCD'] == 'B']),
                "Mantener",
                help="Productos con ventas controladas"
            )
        
        with col3:
            st.metric(
                "Categoría C",
                len(df_abcd[df_abcd['ABCD'] == 'C']),
                "Bajo impacto",
                help="Productos con baja rotación"
            )
        
        with col4:
            st.metric(
                "Categoría D",
                len(df_abcd[df_abcd['ABCD'] == 'D']),
                "Liquidar",
                help="Productos sin demanda reciente"
            )
        
        st.divider()
        
        # Gráficos
        col1, col2 = st.columns(2)
        
        with col1:
            abcd_counts = df_abcd['ABCD'].value_counts().sort_index()
            st.bar_chart(abcd_counts, title="Distribución por Categoría")
        
        with col2:
            capital_by_abcd = df_abcd.groupby('ABCD')['Capital_Bloqueado (€)'].sum()
            st.bar_chart(capital_by_abcd, title="Capital Bloqueado por Categoría")
        
        st.divider()
        
        # Top productos por capital bloqueado
        st.subheader("Top 10: Mayor Capital Bloqueado")
        top_capital = df_abcd.nlargest(10, 'Capital_Bloqueado (€)')[
            ['Marca', 'ABCD', 'Stock', 'PVO', 'Capital_Bloqueado (€)']
        ]
        st.dataframe(top_capital, use_container_width=True)
    
    # TAB 2: Búsqueda de Producto
    with tab2:
        st.header("🔍 Búsqueda de Producto")
        
        col1, col2 = st.columns([3, 1])
        
        with col1:
            search_term = st.text_input(
                "Buscar por nombre, código o barcode",
                placeholder="Ejemplo: DIOR, 192337232893..."
            )
        
        with col2:
            search_button = st.button("🔍 Buscar", use_container_width=True)
        
        if search_term:
            # Buscar en diferentes campos
            mask = (
                df_abcd['Marca'].str.contains(search_term, case=False, na=False) |
                df_abcd['Cód Barras'].astype(str).str.contains(search_term, case=False, na=False) |
                df_abcd['product_id'].astype(str).str.contains(search_term, case=False, na=False)
            )
            
            resultados = df_abcd[mask]
            
            if len(resultados) > 0:
                st.success(f"Se encontraron {len(resultados)} producto(s)")
                
                # Mostrar cada resultado
                for idx, row in resultados.iterrows():
                    with st.container(border=True):
                        col1, col2, col3, col4 = st.columns(4)
                        
                        with col1:
                            st.subheader(row['Marca'][:30])
                        
                        with col2:
                            status_class = f"status-{row['ABCD'].lower()}"
                            st.markdown(f"<p class='{status_class}'>Categoría: {row['ABCD']}</p>", 
                                      unsafe_allow_html=True)
                        
                        with col3:
                            st.write(f"Stock: {row['Stock']:.0f} ud")
                        
                        with col4:
                            st.write(f"Capital: €{row['Capital_Bloqueado (€)']:,.2f}")
                        
                        st.caption(f"Barcode: {row['Cód Barras']}")
                        
                        # Detalles expandibles
                        with st.expander("Ver detalles completos"):
                            col1, col2, col3 = st.columns(3)
                            
                            with col1:
                                st.write(f"**PVO:** €{row['PVO']:.2f}")
                                st.write(f"**Stock:** {row['Stock']:.0f} unidades")
                                st.write(f"**Última Venta:** {row['Última Venta']}")
                            
                            with col2:
                                st.write(f"**Primera Compra:** {row['Primera Compra']}")
                                st.write(f"**Última Compra:** {row['Última Compra']}")
                                st.write(f"**Ventas 7 días:** {row['Ventas_7_Dias']:.0f}")
                            
                            with col3:
                                st.write(f"**Motivo:** {row['Motivo']}")
                                st.write(f"**Acción:** {row['Accion_Recomendada']}")
                                st.write(f"**Alerta:** {row['Alerta']}")
            else:
                st.warning("No se encontraron productos que coincidan")
    
    # TAB 3: Análisis Detallado
    with tab3:
        st.header("📈 Análisis Detallado por Categoría")
        
        selected_category = st.selectbox(
            "Seleccionar categoría",
            ["A", "B", "C", "D"]
        )
        
        category_data = df_abcd[df_abcd['ABCD'] == selected_category]
        
        # Descripción
        descriptions = {
            'A': 'Productos probados con ventas activas y alto valor - REPONER / PRIORIZAR',
            'B': 'Productos con ventas pero valor medio - MANTENER CONTROLADO',
            'C': 'Productos sin ventas reciente o bajo impacto económico - MANTENER MÍNIMO',
            'D': 'Productos sin demanda - LIQUIDAR / NO REPONER'
        }
        
        st.info(descriptions[selected_category])
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Productos", len(category_data))
        with col2:
            st.metric("Capital Total", f"€{category_data['Capital_Bloqueado (€)'].sum():,.0f}")
        with col3:
            st.metric("Stock Total", f"{category_data['Stock'].sum():.0f} ud")
        with col4:
            st.metric("PVO Promedio", f"€{category_data['PVO'].mean():.2f}")
        
        st.divider()
        
        # Tabla de productos en categoría
        st.subheader(f"Productos en Categoría {selected_category}")
        display_cols = ['Marca', 'Stock', 'PVO', 'Capital_Bloqueado (€)', 'Última Venta', 'Motivo']
        available_cols = [c for c in display_cols if c in category_data.columns]
        
        st.dataframe(
            category_data[available_cols].sort_values('Capital_Bloqueado (€)', ascending=False),
            use_container_width=True,
            height=400
        )
    
    # TAB 4: Reportes
    with tab4:
        st.header("📋 Reportes")
        
        report_type = st.selectbox(
            "Tipo de reporte",
            [
                "Resumen Ejecutivo",
                "Productos para Liquidar (D)",
                "Productos Críticos (Stock Bajo)",
                "Capital Bloqueado",
                "Rotación de Inventario"
            ]
        )
        
        if report_type == "Resumen Ejecutivo":
            st.subheader("Resumen Ejecutivo ABCD")
            
            summary_data = {
                'Categoría': ['A', 'B', 'C', 'D'],
                'Productos': [
                    len(df_abcd[df_abcd['ABCD'] == cat])
                    for cat in ['A', 'B', 'C', 'D']
                ],
                'Capital (€)': [
                    df_abcd[df_abcd['ABCD'] == cat]['Capital_Bloqueado (€)'].sum()
                    for cat in ['A', 'B', 'C', 'D']
                ],
                'Stock': [
                    df_abcd[df_abcd['ABCD'] == cat]['Stock'].sum()
                    for cat in ['A', 'B', 'C', 'D']
                ]
            }
            
            summary_df = pd.DataFrame(summary_data)
            st.dataframe(summary_df, use_container_width=True)
            
            # Descargar reporte
            csv = summary_df.to_csv(index=False)
            st.download_button(
                label="Descargar CSV",
                data=csv,
                file_name=f"resumen_abcd_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )
        
        elif report_type == "Productos para Liquidar (D)":
            st.subheader("Productos en Categoría D (Liquidar)")
            category_d = df_abcd[df_abcd['ABCD'] == 'D'].sort_values('Capital_Bloqueado (€)', ascending=False)
            
            st.metric("Capital Total a Liquidar", f"€{category_d['Capital_Bloqueado (€)'].sum():,.2f}")
            
            st.dataframe(
                category_d[['Marca', 'Stock', 'PVO', 'Capital_Bloqueado (€)', 'Última Venta']],
                use_container_width=True
            )
            
            # Descargar
            csv = category_d.to_csv(index=False)
            st.download_button(
                label="Descargar Lista de Liquidación",
                data=csv,
                file_name=f"liquidacion_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )
    
    # TAB 5: Configuración
    with tab5:
        st.header("⚙️ Configuración")
        
        st.subheader("Información del Sistema")
        
        info_cols = st.columns(2)
        
        with info_cols[0]:
            st.write(f"**Productos cargados:** {len(df_abcd)}")
            st.write(f"**Capital total:** €{df_abcd['Capital_Bloqueado (€)'].sum():,.2f}")
            st.write(f"**Stock total:** {df_abcd['Stock'].sum():.0f} unidades")
        
        with info_cols[1]:
            st.write(f"**Fecha análisis:** {ref_date}")
            st.write(f"**Stock promedio:** {df_abcd['Stock'].mean():.1f} unidades")
            st.write(f"**PVO promedio:** €{df_abcd['PVO'].mean():.2f}")
        
        st.divider()
        
        st.subheader("Parámetros ABCD Activos")
        st.info("""
        - **Umbral D:** Sin ventas 120+ días (sin demanda reciente)
        - **Umbral C:** Sin ventas 60+ días
        - **Período de margen:** 90 días
        - **Ventanas de agotamiento:** Consideradas automáticamente
        """)
        
        st.divider()
        
        st.subheader("Exportar Datos")
        
        if st.button("Descargar Análisis Completo (Excel)"):
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_abcd.to_excel(writer, sheet_name='Productos', index=False)
                
                # Sheet de resumen
                summary = pd.DataFrame({
                    'Categoría': ['A', 'B', 'C', 'D'],
                    'Cantidad': [len(df_abcd[df_abcd['ABCD'] == c]) for c in ['A', 'B', 'C', 'D']],
                    'Capital': [df_abcd[df_abcd['ABCD'] == c]['Capital_Bloqueado (€)'].sum() for c in ['A', 'B', 'C', 'D']]
                })
                summary.to_excel(writer, sheet_name='Resumen', index=False)
            
            output.seek(0)
            st.download_button(
                label="📥 Descargar Excel",
                data=output.getvalue(),
                file_name=f"analisis_abcd_{datetime.now().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

else:
    st.warning("No se pudieron cargar los datos. Verifica la conexión a la base de datos.")
    st.info("Selecciona una fuente de datos en la barra lateral para comenzar.")
