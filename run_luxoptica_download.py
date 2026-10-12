#!/usr/bin/env python3
"""
Script: Ejecutar Descarga Manual de Imágenes (para testing)
Simula lo que hará el scheduler automáticamente cada hora
"""

from datetime import datetime
import sys

def main():
    print("\n" + "=" * 80)
    print(f"DESCARGA MANUAL: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    from graph_mail_downloader import (
        download_luxoptica_mail_attachments,
        load_m365_config,
        validate_m365_config,
    )
    
    # Validar configuración
    config = load_m365_config()
    missing = validate_m365_config(config)
    
    if missing:
        print(f"\n❌ Faltan variables: {', '.join(missing)}")
        print("\n   Configura en .env:")
        for var in missing:
            print(f"   - {var}=<valor>")
        return 1
    
    print(f"\n✅ Configuración validada")
    print(f"   Mailbox: {config.mailbox}")
    print(f"   Root: {config.download_root}")
    
    # Ejecutar descarga
    print(f"\n📥 Iniciando búsqueda de correos...")
    
    try:
        summary = download_luxoptica_mail_attachments(
            sender_hint="luxottica",
            subject_hint="image",
            lookback_days=7,
            top_messages=100,
        )
        
        print(f"\n✅ DESCARGA COMPLETADA")
        print(f"   Correos escaneados: {summary.messages_scanned}")
        print(f"   Con adjuntos: {summary.messages_with_attachments}")
        print(f"   Adjuntos descargados: {summary.attachments_downloaded}")
        print(f"   Correos procesados nuevamente: {len(summary.processed_message_ids)}")
        
        if summary.attachments_downloaded > 0:
            print(f"\n📁 Archivos descargados:")
            for path in summary.saved_paths[:5]:
                print(f"   - {path}")
            if len(summary.saved_paths) > 5:
                print(f"   ... y {len(summary.saved_paths) - 5} más")
        else:
            print(f"\n⏳ No hay imágenes aún (esperando respuesta de Luxoptica)")
        
        print(f"\n📊 Próxima ejecución: Programada automáticamente cada hora")
        from repository_storage import state_root
        print(f"   Log de estado: {state_root(config.download_root) / '.mail_download_state.json'}")
        
        return 0
        
    except Exception as e:
        print(f"\n❌ Error durante descarga: {e}")
        print(f"\n   Verifica:")
        print(f"   1. Credenciales en .env")
        print(f"   2. Permisos en Azure AD (Mail.Read)")
        print(f"   3. Full Access al mailbox compartido")
        return 1


if __name__ == "__main__":
    sys.exit(main())
