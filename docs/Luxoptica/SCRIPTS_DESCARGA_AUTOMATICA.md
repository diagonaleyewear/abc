# 🤖 Descarga Automática de Imágenes - Guía de Scripts

## 📋 Tres Scripts Principales

### 1️⃣ `monitor_luxoptica_downloads.py`
**Propósito:** Monitorear estado y logs

```powershell
python monitor_luxoptica_downloads.py
```

**Qué revisa:**
- ✅ Variables de configuración M365 en .env
- ✅ Conectividad con Graph API (autenticación)
- ✅ Archivo de estado de descargas
- ✅ Imágenes ya descargadas

**Output:**
```
✅ Configuración: OK
✅ Conectividad Graph: OK
✅ Estado de descargas: 5 correos procesados
✅ Carpeta de descargas: 50 imágenes en lote-001
```

**Cuándo usarlo:** 
- Verificar que todo está funcionando
- Revisar qué imágenes se descargaron
- Diagnosticar problemas

---

### 2️⃣ `run_luxoptica_download.py`
**Propósito:** Ejecutar descarga de imágenes (manual/test)

```powershell
python run_luxoptica_download.py
```

**Qué hace:**
1. Valida configuración M365
2. Se conecta a Microsoft Graph API
3. Lee inbox del mailbox compartido
4. Descarga adjuntos (imágenes)
5. Organiza por modelo y EAN
6. Crea copias para Farfetch y Miinto cuando existen sus IDs en Odoo
7. Registra pendientes en `repo/images/.market_pending.json`
8. Registra correos procesados en `.mail_download_state.json`

**Output:**
```
✅ Correos escaneados: 15
✅ Con adjuntos: 1
✅ Adjuntos descargados: 50
   Guardados en: docs/Luxoptica/descargas/2026-08-20/lote-001/
```

**Cuándo usarlo:**
- Test manual para verificar que funciona
- Ejecutar manualmente cuando sea necesario
- Debugging de problemas

---

### 3️⃣ `scheduler_luxoptica.py`
**Propósito:** Configurar ejecución automática (cada hora)

```powershell
# Ver instrucciones para crear scheduler
python scheduler_luxoptica.py setup

# Ejecutar descarga manual (test)
python scheduler_luxoptica.py test

# Ver estado actual
python scheduler_luxoptica.py status
```

**Qué hace:**
- Proporciona comando PowerShell para crear tarea en Windows Task Scheduler
- Permite ejecutar pruebas
- Muestra estado actual

### Monitor automático en Docker

En producción, `docker-compose.yml` incluye el servicio
`abcd-luxoptica-monitor`. Revisa el buzón cada 10 minutos, descarga los ZIP y
organiza las imágenes sin intervención manual.

```bash
docker compose up -d --build --force-recreate
docker compose logs -f abcd-luxoptica-monitor
```

Las carpetas persistentes son:

- `repo/images/`: imágenes originales y copias por mercado.
- `luxoptica_data/`: solicitudes TXT, manifiestos y descargas auxiliares.
- `masterdata_data/`: diccionario personalizado de producción.

**Instalación de Scheduler (Windows):**
```powershell
# Copiar el comando que genera:
python scheduler_luxoptica.py setup

# Ejecutar en PowerShell (como Administrador)
[Copiar y pegar el comando generado]

# Verificar que se creó
Get-ScheduledTask -TaskName "LuxopticaImageDownloader"
```

---

## 🚀 Flujo Recomendado

### Día 1: Envío a Luxoptica (hoy)
```powershell
# 1. Verificar que todo está ok
python monitor_luxoptica_downloads.py

# Debe mostrar:
# ✅ Configuración: OK
# ✅ Conectividad Graph: OK
# ✅ Carpeta de descargas: vacía (esperando respuesta)
```

### Día 2-3: Esperar Respuesta Luxoptica
- Luxoptica procesa (24-48h)
- Envía correo a `images@diagonaleyewear.com`

### Día 3+: Descargar Automáticamente

**Opción A - Manual (Test Primero):**
```powershell
# Test 1: Ejecutar descarga manual
python run_luxoptica_download.py

# Output esperado:
# ✅ Correos escaneados: 5
# ✅ Adjuntos descargados: 50
# 📁 Guardados en: docs/Luxoptica/descargas/2026-08-20/lote-001/

# Test 2: Monitorear estado
python monitor_luxoptica_downloads.py

# Output esperado:
# ✅ Estado de descargas: 50 imágenes descargadas
# 📁 docs/Luxoptica/descargas/2026-08-20/lote-001/
#    ├─ image-001.jpg
#    ├─ image-002.jpg
#    └─ ...
```

**Opción B - Automático (después de verificar):**
```powershell
# Crear tarea de Windows que ejecute cada hora
python scheduler_luxoptica.py setup

# Ver el comando PowerShell generado
# Copiar y ejecutar en PowerShell (Admin)

# Verificar que se creó
Get-ScheduledTask -TaskName "LuxopticaImageDownloader"

# Verificar que funciona
python scheduler_luxoptica.py status
```

---

## 📊 Estructura de Archivos Descargados

```
repo/images/
├─ .market_pending.json                (imágenes sin ID de mercado)
└─ RB2140/
   └─ 8056262672563/
      ├─ originales.png
      ├─ Farfetch/
      │  ├─ <id>_V1.png
      │  ├─ <id>_V2.png
      │  └─ <id>_V3.png
      └─ Miinto/
         ├─ <id>_V1.jpeg
         ├─ <id>_V2.jpeg
         └─ <id>_V3.jpeg
```

Si una imagen ya se ha descargado pero Odoo todavía no tiene el ID de
Farfetch o Miinto, el original se conserva en su carpeta EAN y se registra en
`.market_pending.json`. El monitor vuelve a consultar Odoo en cada intervalo
(`--interval-minutes`, 10 minutos por defecto) y crea las copias pendientes
cuando los IDs aparecen.

---

## NAS Synology: sincronización de imágenes

Las imágenes organizadas en `repo/images/` se sincronizan con el Synology
mediante el servicio rsync. El módulo remoto validado es `Fotos` y el destino
corresponde a la carpeta compartida `/volume1/Fotos`.

Esta replica remota se conserva, pero **no equivale al backup completo de
migracion**: excluye ZIP y no verifica por si misma planes/recibos/recuperacion.
En la comprobacion del 10/10/2026 desde Cloud 192.168.1.55, el acceso TCP 873 y
el listado rsync agotaron su timeout. No se escribio en el NAS remoto.
Preparacion NFS y backups independientes:
[plan de almacenamiento](../../deployment/nfs/README.md).
Con el layout separado, los adjuntos voluminosos van a
`IMAGE_REPOSITORY_WORK_ROOT`; JSON de estado y SQLite permanecen locales en
`IMAGE_REPOSITORY_STATE_ROOT`. No sincronizar estas raices ni locks/metadatos
privados al modulo publico Fotos. Antes del corte, revisar el origen de la
tarea rsync para la nueva raiz por EAN manteniendo la replica, sin `--delete`.

### Comprobar conectividad y módulo

Desde el equipo que realiza la sincronización:

```powershell
Test-NetConnection 188.227.143.110 -Port 873
rsync rsync://188.227.143.110/
```

Debe aparecer el módulo `Fotos`. La conexión rsync usa el usuario configurado
en Synology, por ejemplo `sync_images`.

### Prueba sin modificar el NAS

Antes de copiar datos, ejecutar un `dry-run`:

```powershell
rsync -avn --itemize-changes --stats `
   --exclude='staging/' `
   --exclude='20??-??-??/***' `
   --exclude='*.zip' `
   repo/images/ `
   sync_images@188.227.143.110::Fotos/
```

Las exclusiones evitan copiar las carpetas temporales por fecha y los ZIP
descargados. En la prueba validada se detectaron 1.202 archivos y unos 3,77 GB
para sincronizar, sin borrar archivos remotos.

### Sincronización real

Cuando el `dry-run` sea correcto:

```powershell
rsync -av --partial `
   --exclude='staging/' `
   --exclude='20??-??-??/***' `
   --exclude='*.zip' `
   repo/images/ `
   sync_images@188.227.143.110::Fotos/
```

`--partial` permite reanudar transferencias interrumpidas. No se usa `--delete`,
por lo que la sincronización no elimina contenido existente en el NAS.

`staging/` se excluye en todos los niveles: nunca replicar fuente, backups,
recuperacion ni temporales del ensayo. Estos comandos son una receta, no
prueba de una tarea automatica. La investigacion encontro dos comandos
manuales historicos con origen relativo `repo/images/` y sin exclusion literal
staging; no se localizo script/programacion de replica en el alcance accesible.
No se presupone DSM ni se cambio o ejecuto la replica. La nueva replica Fotos
se preparara tras validar migracion, con simulacion/conectividad previas,
solo imagenes definitivas por EAN, sin staging/backups/temporales/SQLite,
sin borrados, con lock e historial visible Cloud; no activar ahora.
Evidencia y limites: [captura consistente](../../deployment/nfs/CAPTURE.md).

### Requisitos del Synology

- Servicio rsync habilitado.
- Módulo compartido `Fotos` publicado.
- Usuario rsync con permiso de lectura/escritura en `Fotos`.
- Puerto TCP `873` accesible desde el servidor Docker.
- Si el acceso se realiza por Internet, limitar la regla WAN a la IP pública
   del servidor y preferir una VPN frente a exponer rsync directamente.

---

## 🔍 Logs y Diagnostico

### Ver qué correos se procesaron
```powershell
Get-Content docs/Luxoptica/descargas/.mail_download_state.json | ConvertFrom-Json | Select -ExpandProperty processed_message_ids
```

### Ver último resultado de ejecución
```powershell
# Si está en Windows Task Scheduler, revisar Event Viewer
Get-EventLog -LogName Application -Source TaskScheduler -Newest 5
```

### Limpiar logs (si es necesario)
```powershell
# Resetear el estado (descargará todo de nuevo)
Remove-Item docs/Luxoptica/descargas/.mail_download_state.json -Force
```

---

## ✅ Checklist de Verificación

- [ ] `.env` tiene todas las 5 variables M365
- [ ] `python test_graph_api_config.py` muestra ✅ TODAS LAS PRUEBAS PASARON
- [ ] `python monitor_luxoptica_downloads.py` muestra 4x ✅
- [ ] `python run_luxoptica_download.py` funciona sin errores (aún sin imágenes)
- [ ] Windows Task Scheduler configurado (opcional pero recomendado)
- [ ] Solicitud enviada a Luxoptica el 2026-08-19
- [ ] Esperando respuesta (24-48h)

---

## 🎯 Resumen

| Script | Propósito | Ejecutar | Frecuencia |
|--------|-----------|----------|-----------|
| `test_graph_api_config.py` | Validar credenciales | 1x (setup) | Manual |
| `monitor_luxoptica_downloads.py` | Ver estado | Bajo demanda | Manual |
| `run_luxoptica_download.py` | Descargar imágenes | Test y manual | Manual |
| `poll_luxoptica_mail.py` | Descargar y revisar pendientes | `--interval-minutes 10` | Automático |
| `scheduler_luxoptica.py` | Automatizar descarga | Setup 1x | Automático (cada hora) |

---

## 📞 Troubleshooting

### Error: "Faltan variables de entorno"
```powershell
# Verificar .env
cat .env | Select-String "M365_"

# Debe mostrar 5 líneas con M365_
```

### Error: "403 Forbidden"
```powershell
# Permisos no concedidos en Azure AD
# Ejecutar en PowerShell (como admin):
python test_graph_api_config.py

# Revisar: docs/Luxoptica/CONFIGURACION_GRAPH_API.md Paso 3-4
```

### No descarga las imágenes
```powershell
# Verificar que hay correos en mailbox
# (Luxoptica aún no ha respondido - esperar 24-48h)

# Ver estado actual
python monitor_luxoptica_downloads.py

# Si muestra "Carpeta: vacía" - es normal, esperar a Luxoptica
```

---

## 🎉 Próximos Pasos

1. ✅ Ejecuta hoy: `python monitor_luxoptica_downloads.py`
2. ⏳ Espera 24-48h a que Luxoptica responda
3. 🔄 Ejecuta: `python run_luxoptica_download.py` (cuando llegue el correo)
4. ✅ Verifica: `python monitor_luxoptica_downloads.py` (debe mostrar imágenes)
5. 🤖 Configura: Scheduler automático si todo funciona bien

---

**Commit:** Todos estos scripts están en repositorio `main` y sincronizados.
