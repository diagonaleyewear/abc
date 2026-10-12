#!/usr/bin/env python3
"""
Script: Generar solicitud de 50 imágenes para Luxoptica
Extrae 50 UPC del MASTERDATA y genera archivo .txt para enviar
"""

from pathlib import Path
from datetime import datetime
import pandas as pd
import sys
from image_repository import ImageRepository

def generate_luxoptica_request_50():
    """Genera archivo de solicitud con 50 EAN."""
    
    # Buscar archivo MASTERDATA transformado
    docs_dir = Path(__file__).parent / "docs" / "MasterData"
    
    # Intentar con estos archivos en orden de preferencia
    candidates = [
        docs_dir / "MASTERDATA_prueba_grande_transformado.xlsx",
        docs_dir / "result.xlsx",
        docs_dir / "MASTERDATA_transformado.xlsx",
    ]
    
    masterdata_file = None
    for candidate in candidates:
        if candidate.exists():
            masterdata_file = candidate
            break
    
    if not masterdata_file:
        print(f"❌ No se encontró archivo MASTERDATA en {docs_dir}")
        print(f"   Buscados: {[c.name for c in candidates]}")
        return 1
    
    print(f"✅ Usando: {masterdata_file.name}")
    
    # Leer Excel
    try:
        df = pd.read_excel(masterdata_file)
    except Exception as e:
        print(f"❌ Error al leer Excel: {e}")
        return 1
    
    # Buscar columna de UPC/Barcode
    upc_col = None
    for col_name in ["Barcode", "UPC", "barcode", "upc"]:
        if col_name in df.columns:
            upc_col = col_name
            break
    
    if not upc_col:
        print(f"❌ No se encontró columna UPC/Barcode")
        print(f"   Columnas disponibles: {list(df.columns)}")
        return 1
    
    # Extraer UPC válidos
    upc_list = df[upc_col].dropna().astype(str).str.strip()
    upc_list = [upc for upc in upc_list if upc and upc != ""]
    repository = ImageRepository()
    upc_list = [ean for ean in dict.fromkeys(upc_list)
                if repository.pending_views(ean, ("frontal", "lateral", "perspectiva"))]
    
    if len(upc_list) < 50:
        print(f"⚠️  Solo hay {len(upc_list)} UPC disponibles (se esperaban 50)")
        print(f"   Se generará lote con {len(upc_list)} UPC")
        request_list = upc_list
    else:
        request_list = upc_list[:50]
    
    print(f"✅ EAN extraídos: {len(request_list)}")
    
    # Generar archivo .txt
    luxoptica_dir = Path(__file__).parent / "docs" / "Luxoptica"
    luxoptica_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"upc-products-images-request-{timestamp}-50-eans.txt"
    file_path = luxoptica_dir / file_name
    
    # Escribir archivo (sin prefijo "es.", uno por línea)
    content = "\n".join(request_list) + "\n"
    file_path.write_text(content, encoding="utf-8")
    
    print(f"\n✅ Archivo generado:")
    print(f"   Ubicación: {file_path}")
    print(f"   EAN: {len(request_list)}")
    print(f"   Tamaño: {len(content)} bytes")
    
    # Mostrar primeros y últimos 5 EAN
    print(f"\n📋 Primeros 5 EAN:")
    for i, upc in enumerate(request_list[:5], 1):
        print(f"   {i}. {upc}")
    
    if len(request_list) > 5:
        print(f"   ...")
        print(f"📋 Últimos 5 EAN:")
        for i, upc in enumerate(request_list[-5:], len(request_list)-4):
            print(f"   {i}. {upc}")
    
    print(f"\n📧 Pasos siguientes:")
    print(f"   1. Ir a https://luxottica.com/")
    print(f"   2. Login → Servicios → Recursos Digitales → Imágenes de Producto")
    print(f"   3. Cargar archivo: {file_name}")
    print(f"   4. Seleccionar 'Todas las vistas'")
    print(f"   5. Especificar email: images@diagonaleyewear.com")
    print(f"   6. Enviar solicitud")
    print(f"\n   El sistema descargará automáticamente las imágenes cuando Luxoptica responda.")
    
    return 0


if __name__ == "__main__":
    sys.exit(generate_luxoptica_request_50())
