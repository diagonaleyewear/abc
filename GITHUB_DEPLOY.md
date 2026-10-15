# Procedimiento vigente para Cloud

La publicacion GHCR no despliega produccion. Usar el workflow manual protegido
**Deploy Cloud Production** y revisar [instalacion administrativa, identidad
dedicada y rollback](deployment/cloud/README.md).
Las instrucciones SSH/auto-deploy legacy de este documento no deben utilizarse
para `cloud-nas` ni para el hotfix Luxottica.

# GitHub Actions + Auto-Deploy (archivo historico)

## ⚡ En 3 Pasos

### Paso 1: Crear Secrets en GitHub (5 min)

1. **Ir a tu repo:** GitHub → Settings → Secrets and variables → Actions
2. **Crear 4 secrets:**

| Secret | Valor | Ejemplo |
|--------|-------|---------|
| `SERVER_HOST` | IP del servidor | `cloud.diagonaleyewear.net` |
| `SERVER_USER` | Usuario SSH | `rubensg` |
| `SERVER_PORT` | Puerto SSH | `2224` |
| `SERVER_SSH_KEY` | Clave privada SSH | (ver abajo) |

**Para generar clave SSH:**

```bash
# En tu máquina (Windows/Mac/Linux)
ssh-keygen -t ed25519 -f abcd_deploy_key -N ""

# Esto crea dos archivos:
# - abcd_deploy_key (PRIVADA - para GitHub)
# - abcd_deploy_key.pub (PÚBLICA - para servidor)

# Ver la clave privada
cat abcd_deploy_key
```

**En GitHub (SECRET):**
- Copiar contenido completo de `abcd_deploy_key`
- Pegar en `SERVER_SSH_KEY`

**En el servidor:**

```bash
# SSH al servidor
ssh root@tu-servidor

# Crear directorio .ssh si no existe
mkdir -p ~/.ssh
chmod 700 ~/.ssh

# Agregar clave pública
cat >> ~/.ssh/authorized_keys << 'EOF'
# Pegar aquí contenido de abcd_deploy_key.pub
ssh-ed25519 AAAA... (tu-email@example.com)
EOF

# Asegurar permisos
chmod 600 ~/.ssh/authorized_keys
```

---

### Paso 2: Subir Código a GitHub (5 min)

```bash
# En tu directorio ABC
cd "c:\Users\Ruben\OneDrive - Diagonal Eyewear\Proyectos\ABC"

# Inicializar Git (si no está)
git init
git add .
git commit -m "ABCD Control - Inicial"

# Agregar remote (reemplaza USERNAME y REPO)
git remote add origin https://github.com/USERNAME/REPO.git

# Subir a GitHub
git branch -M main
git push -u origin main
```

---

### Paso 3: Primera Actualización (Automática)

```bash
# Cualquier cambio que hagas:
git add .
git commit -m "Descripción del cambio"
git push origin main

# 🎉 ¡Se depliegue automáticamente!
```

**Ver status:**
- GitHub → Actions → Verás el workflow ejecutándose
- O accede a la app: https://abcd.cloud.diagonaleyewear.net

---

## 🔄 Flujo Automático

```
Tu Máquina
    ↓
git push origin main
    ↓
GitHub Actions (tests + build Docker)
    ↓
SSH al Servidor
    ↓
git pull en /opt/abcd-control
    ↓
docker-compose build
    ↓
docker-compose up -d
    ↓
✅ Nuevo código en producción
```

---

## 📋 El Workflow Hace Esto

### En cada PUSH a main:

1. **Build Docker**
   - Construye imagen con código nuevo
   - Sube a GitHub Container Registry (gratis)

2. **Deploy SSH**
   - Conecta al servidor automáticamente
   - Ejecuta `scripts/deploy.sh`
   - Descarga código + reinicia contenedor

3. **Health Check**
   - Verifica que la app esté respondiendo
   - Ve últimos logs

### En cada PULL REQUEST:

1. **Tests**
   - Chequea sintaxis Python
   - Corre linter (flake8)
   - NO despliega (solo en main)

---

## 🎯 Casos de Uso

### **Caso 1: Bug Fix Rápido**

```bash
# En tu máquina
nano app_enhanced.py          # Arreglar bug
git add app_enhanced.py
git commit -m "Fix: corregir bug en búsqueda"
git push

# ✅ GitHub Actions automáticamente:
#    - Tests
#    - Build Docker
#    - SSH al servidor
#    - Deploy
#    - App actualizada en 2-3 minutos
```

### **Caso 2: Feature Nueva**

```bash
# Crear rama feature
git checkout -b feature/nueva-busqueda

# Hacer cambios
nano app_enhanced.py
git add .
git commit -m "Feature: nueva búsqueda avanzada"
git push origin feature/nueva-busqueda

# En GitHub: Abrir Pull Request
# - Se corren tests automáticamente
# - Si OK, merge a main
# - Se depliegue automáticamente
```

### **Caso 3: Actualizar Dependencias**

```bash
# Actualizar requirements
nano requirements.txt
git add requirements.txt
git commit -m "Update: Streamlit a v1.30"
git push

# ✅ Automático:
#    - Build nueva imagen
#    - Deploy
```

---

## 🔒 Seguridad

✅ **Clave SSH privada:** Solo en GitHub Secrets (encriptada)
✅ **Clave SSH pública:** Solo en servidor
✅ **No guardar .env:** Usar secrets/variables de entorno
✅ **Tests antes de deploy:** Pull requests requieren tests

---

## 📊 Ver Status del Deploy

### **En GitHub:**

```
GitHub → Tu Repo → Actions
  └── Deploy to Production
      ├── Iniciado: fecha/hora
      ├── Estado: In Progress / ✅ Success / ❌ Failed
      ├── Logs completos
      └── Duración: ~3-5 min
```

### **En el Servidor:**

```bash
# Ver logs del último deploy
tail -f /var/log/abcd-deploy.log

# Ver contenedor corriendo
docker-compose ps

# Ver logs de la app
docker-compose logs -f abcd-app
```

---

## 🆘 Troubleshooting

### **Error: "SSH key not found"**

```bash
# Verificar que SERVER_SSH_KEY está en GitHub Secrets
# Comprobar que contiene -----BEGIN... y -----END...
```

### **Error: "Permission denied (publickey)"**

```bash
# En el servidor, verificar:
cat ~/.ssh/authorized_keys
# Debe contener la clave pública (ssh-ed25519 AAA...)

# Revisar permisos:
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### **Error: "docker-compose: command not found"**

```bash
# En el servidor:
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### **Deploy tarda mucho**

- Es normal en primer build (3-5 min)
- Próximos son más rápidos (cache)

---

## 📝 Variables de Entorno

El GitHub Actions **NO** toca `.env` (seguridad).

**Ubicación de secretos:**
```
GitHub Secrets → SERVER_* (para deploy)
.env en servidor → DB_*, etc (credenciales reales)
```

Si necesitas cambiar BD o credenciales:
```bash
# En el servidor
ssh root@tu-servidor
nano /opt/abcd-control/.env
docker-compose down && docker-compose up -d
```

---

## 🔔 Notificaciones (Opcional)

### Agregar Slack Notifications

1. **Crear webhook en Slack:**
   - Slack workspace → Apps → Incoming Webhooks
   - Create New Webhook
   - Copiar URL

2. **Agregar secret en GitHub:**
   - Settings → Secrets → `SLACK_WEBHOOK`

3. **El script `deploy.sh` enviará notificaciones automáticamente**

---

## ✅ Checklist Final

- [ ] Clave SSH generada y agregada
- [ ] Secrets en GitHub configurados
- [ ] Código pusheado a main
- [ ] GitHub Actions corrió exitosamente
- [ ] App actualizada en servidor
- [ ] URL funciona: https://abcd.cloud.diagonaleyewear.net

---

## 📚 Archivos Importantes

| Archivo | Propósito |
|---------|-----------|
| `.github/workflows/deploy.yml` | Workflow de GitHub Actions |
| `scripts/deploy.sh` | Script que ejecuta en servidor |
| `.dockerignore` | Excluye archivos del build |
| `docker-compose.yml` | Orquestación en servidor |

---

## 🎉 Resultado Final

```
Cambio código local
    ↓ git push
GitHub Actions (2-3 min)
    ↓
✨ App actualizada en producción
    ↓
Acceso en: https://abcd.cloud.diagonaleyewear.net
```

**¡Zero downtime!**

---

## 🚀 Comandos Frecuentes

```bash
# Ver logs del deploy
git log --oneline -10

# Si quieres deshacer el último push
git revert HEAD
git push

# Ver status del GitHub Actions
# → GitHub → Actions (interfaz visual)

# Forzar redeploy sin cambios
git commit --allow-empty -m "Deploy"
git push
```

---

## 📞 Soporte

**GitHub Actions no funciona:**
- Revisar logs en GitHub Actions UI
- Verificar Secrets están configurados
- Comprobar clave SSH válida

**Deploy no llega al servidor:**
- `ssh root@servidor` y verificar `/var/log/abcd-deploy.log`
- Chequear conectividad desde GitHub Actions
- Probar SSH manual: `ssh -i abcd_deploy_key root@servidor`
