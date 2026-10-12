# 📦 Estructura de Archivos Docker

> Cloud requiere identidad real del artefacto al construir: BUILD_COMMIT, BUILD_ID
> y BUILD_PUBLISHED. Consulte [la configuración actual](docs/CLOUD_HOME.md#identidad-del-artefacto)
> antes de usar los ejemplos históricos de build de esta guía. Esta PR sólo
> construye/verifica, sin arrancar automatizaciones ni desplegar.

## Archivos Creados/Modificados

### 1. **Dockerfile**
**Propósito:** Define cómo construir la imagen Docker

**Características:**
- ✅ Multi-stage build (builder + final)
- ✅ Optimizado para tamaño mínimo (slim Python 3.11)
- ✅ Depurador de salud (healthcheck)
- ✅ Variables de entorno configuradas
- ✅ Configuración de Streamlit inline

**Versión:** Production-ready

**Tamaño esperado:** ~500MB

```bash
# Construir manualmente
docker build -t abcd-control:latest .

# Ver layers
docker history abcd-control:latest
```

---

### 2. **docker-compose.yml**
**Propósito:** Orquestar el contenedor (desarrollo y producción)

**Lo que incluye:**
- Configuración de servicio `abcd-app`
- Servicio `abcd-luxoptica-monitor` (descarga de correo Luxoptica)
- Servicio `abcd-order-alerts` (alertas automáticas de pedidos de clientes vigilados, ver
  [docs/AlertaPedidos/SPRINTS_ALERTA_PEDIDOS_CLIENTES.md](docs/AlertaPedidos/SPRINTS_ALERTA_PEDIDOS_CLIENTES.md#operación-del-servicio-abcd-order-alerts))
- Variables de entorno para BD
- Puerto 8501 expuesto
- Health check
- Límites de recursos (2GB CPU, 2GB RAM)
- Volúmenes (comentados para producción)
- Logs rotados
- Network bridge

**Ambiente:**
```bash
# Desarrollo (con volúmenes para hot reload)
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up

# Producción
docker-compose up -d
```

---

### 3. **.dockerignore**
**Propósito:** Excluir archivos innecesarios de la imagen

**Excluye:**
- Git history
- Cache de Python (__pycache__)
- Directorios de desarrollo (.vscode, .idea)
- Archivos temporales
- Bases de datos locales
- Logs

**Reducción:** ~50MB menos en la imagen

---

### 4. **.env.example**
**Propósito:** Template para variables de entorno

**Variables:**
- `DB_HOST`, `DB_PORT`, `DB_NAME` - PostgreSQL
- `DB_USER`, `DB_PASSWORD` - Credenciales
- Configuraciones de Streamlit

**Uso:**
```bash
cp .env.example .env
# Editar con credenciales reales
nano .env
```

⚠️ **IMPORTANTE:** Nunca commitear `.env` con credenciales reales

---

### 5. **start.sh**
**Propósito:** Script bash para iniciar fácilmente (Linux/Mac)

**Qué hace:**
1. Verifica Docker instalado
2. Crea `.env` desde template
3. Construye imagen
4. Inicia contenedor
5. Espera a que esté listo
6. Muestra URL de acceso

**Uso:**
```bash
chmod +x start.sh
./start.sh
```

---

### 6. **DEPLOYMENT_ISPCONFIG.md** ⭐
**Propósito:** Guía específica para ISPConfig + Apache2

**Para:** Si tienes un panel ISPConfig (lo más común en hosting compartido)

**Ventajas:**
- ✅ Gestión visual desde ISPConfig GUI
- ✅ SSL automático con Let's Encrypt
- ✅ Fácil de actualizar
- ✅ Integrado con otros sitios web

**Usa esto si:** Tu servidor usa ISPConfig

---

### 7. **apache2.conf**
**Propósito:** Configuración de Apache2 como reverse proxy

**Soporta:**
- ✅ Proxy inverso a Docker (puerto 8501)
- ✅ WebSocket (/_stcore/stream)
- ✅ SSL/TLS con Let's Encrypt
- ✅ Compresión Gzip
- ✅ Headers de seguridad
- ✅ Rate limiting
- ✅ Logs rotados

**Uso:**
- En ISPConfig: Copiar a "Directivas personalizadas de Apache2"
- Manual: Copiar a `/etc/apache2/sites-available/`

---

### 8. **nginx.conf** (Alternativa)
**Propósito:** Configuración de Nginx como reverse proxy

**Solo si usas Nginx en lugar de Apache2**

**Características:**
- ✅ Proxy inverso a Docker
- ✅ WebSocket support
- ✅ SSL/TLS
- ✅ Compresión
- ✅ Seguridad

---

### 9. **docker-compose.prod.yml**
**Propósito:** Override para producción (más recursos, security)

**Aplica:**
```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

### 10. **DEPLOYMENT_GUIDE.md**
**Propósito:** Guía general de deployment

**Contiene:**
- ✅ Opción 1: Docker + Apache2 + ISPConfig (⭐ Recomendado)
- ✅ Opción 2: Docker + Nginx
- ✅ Opción 3: Heroku
- ✅ Opción 4: AWS/Google Cloud
- ✅ Monitoreo y mantenimiento
- ✅ Troubleshooting

---

### 11. **requirements.txt** (actualizado)
**Cambios:**
- ➕ Añadido `plotly>=5.0` (gráficos)
- ➕ Añadido `numpy>=1.21` (dependencia)

---

## 🗂️ Estructura Final del Proyecto

```
.
├── Dockerfile              ← Definición de imagen
├── docker-compose.yml      ← Orquestación (versión actual)
├── .dockerignore          ← Archivos a excluir
├── .env.example           ← Template de variables
├── start.sh               ← Script para iniciar
├── DEPLOYMENT_GUIDE.md    ← Guía de deployment
├── requirements.txt       ← Dependencias Python (actualizado)
│
├── app_enhanced.py        ← Aplicación Streamlit
├── db_loader.py          ← Carga de datos
├── engine.py             ← Lógica ABCD
├── db_config.py          ← Configuración BD
│
├── GUIA_APP_ENHANCED.md  ← Documentación de uso
└── docs/
    └── copilot_odoo.md   ← Documentación técnica
```

---

## 🚀 Flujo de Deployment

### **Desarrollo Local**

```
1. git clone / descargar archivos
   ↓
2. docker-compose build
   ↓
3. docker-compose up -d
   ↓
4. http://localhost:8501
```

### **Servidor Producción (cloud.diagonaleyewear.net)**

```
1. SSH al servidor
   ↓
2. git clone / scp archivos
   ↓
3. Editar .env con credenciales
   ↓
4. docker-compose up -d
   ↓
5. Configurar Nginx proxy + SSL
   ↓
6. https://abcd.cloud.diagonaleyewear.net
```

---

## 📊 Comparación: Docker vs Sin Docker

| Aspecto | Sin Docker | Con Docker |
|---------|-----------|-----------|
| **Instalación** | Python + dependencias | Docker + 1 comando |
| **Compatibilidad** | Windows/Mac/Linux | Idéntico en todas partes |
| **Actualización** | `pip install -U` | `docker build` |
| **Aislamiento** | No | Sí (seguro) |
| **Escalabilidad** | Manual | Automática |
| **Producción** | ⚠️ Complejo | ✅ Recomendado |

---

## 🔍 Verificación Pre-Deployment

### **Checklist Local**

```bash
# ✅ Verificar Docker
docker --version
docker-compose --version

# ✅ Verificar archivos
ls Dockerfile docker-compose.yml .env.example requirements.txt
ls app_enhanced.py db_loader.py engine.py db_config.py

# ✅ Build exitoso
docker-compose build

# ✅ Contenedor corre
docker-compose up -d
sleep 3
docker-compose ps

# ✅ Health check
curl -I http://localhost:8501

# ✅ Logs limpios
docker-compose logs abcd-app | grep -i error
```

### **Checklist Servidor**

```bash
# ✅ Docker instalado
docker --version

# ✅ Puerto 8501 disponible
sudo lsof -i :8501 || echo "Puerto disponible"

# ✅ BD accesible
psql -h 10.3.0.13 -U user_sg_informatica -d DiagonalDBProd -c "SELECT 1"

# ✅ Espacios de disco
df -h /opt

# ✅ Permisos
sudo chown -R $USER:$USER /opt/abcd-control
```

---

## 📈 Monitoreo Post-Deployment

### **Comandos Diarios**

```bash
# Estado del contenedor
docker-compose ps

# Uso de recursos
docker stats --no-stream abcd-app

# Últimas líneas de logs
docker-compose logs --tail=20 abcd-app

# Health check
curl http://localhost:8501/_stcore/health
```

### **Alertas Automáticas**

```bash
# Crear script de monitoreo
cat > monitor.sh << 'EOF'
#!/bin/bash
if ! curl -s http://localhost:8501/_stcore/health > /dev/null; then
    echo "ALERTA: ABCD Control no responde!"
    docker-compose restart abcd-app
    # Enviar email/Slack si es necesario
fi
EOF

# Ejecutar cada 5 minutos
*/5 * * * * /opt/abcd-control/monitor.sh
```

---

## 🛠️ Troubleshooting Rápido

**Problema:** "Image build failed"
```bash
docker-compose build --no-cache
```

**Problema:** "Connection refused"
```bash
docker-compose logs abcd-app | grep -i error
```

**Problema:** "Port already in use"
```bash
docker-compose down
docker-compose up -d
```

**Problema:** "Out of memory"
```bash
# Aumentar en docker-compose.yml
# memory: 4G  (en lugar de 2G)
docker-compose down
docker-compose up -d
```

---

## 📚 Referencias

- [Docker Docs](https://docs.docker.com/)
- [Docker Compose Reference](https://docs.docker.com/compose/compose-file/compose-file-v3/)
- [Streamlit Docker](https://docs.streamlit.io/knowledge-base/tutorials/deploy/docker)
- [Nginx Reverse Proxy](https://nginx.org/en/docs/)
- [Let's Encrypt SSL](https://letsencrypt.org/getting-started/)
