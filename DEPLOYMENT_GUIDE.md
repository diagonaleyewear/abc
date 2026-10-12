# 🚀 Deployment a Cloud - Instrucciones

> Antes de reconstruir una imagen desde fuentes, configure la
> [identidad real del build Cloud](docs/CLOUD_HOME.md#identidad-del-artefacto).
> Los ejemplos históricos no aportan los nuevos argumentos obligatorios.
> El cambio de la PR #39 no autoriza despliegue, automatización ni migración.

## ⭐ OPCIÓN RECOMENDADA: Docker + Apache2 + ISPConfig

**Si tienes ISPConfig, esto es lo más simple y seguro.** Ver [DEPLOYMENT_ISPCONFIG.md](DEPLOYMENT_ISPCONFIG.md)

---

## Opción 1: Docker en Servidor Propio (Apache2 + ISPConfig)

### Paso 1: Preparar el Servidor

```bash
# Instalar Docker y Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verificar instalación
docker --version
docker-compose --version
```

### Paso 2: Descargar/Clonar el Repositorio

```bash
# Si tienes Git
git clone <repo-url> /opt/abcd-control
cd /opt/abcd-control

# O copiar archivos manualmente
scp -r ./* usuario@cloud.diagonaleyewear.net:/opt/abcd-control/
```

### Paso 3: Configurar Variables de Entorno

```bash
cd /opt/abcd-control

# Crear archivo .env desde el ejemplo
cp .env.example .env

# Editar con tus credenciales
nano .env
```

**Contenido del .env:**
```
DB_HOST=10.3.0.13
DB_PORT=5432
DB_NAME=DiagonalDBProd
DB_USER=user_sg_informatica
DB_PASSWORD=xy8fPpxPerETSQ
```

### Paso 4: Construir e Iniciar el Contenedor

```bash
# Construir la imagen Docker
docker-compose build

# Iniciar el servicio en background
docker-compose up -d

# Ver logs
docker-compose logs -f abcd-app
```

### Paso 5: Configurar Apache2 como Reverse Proxy (Producción)

**⚠️ Si tienes ISPConfig, NO hagas esto manualmente. Usa la GUI de ISPConfig.**
**Ver:** [DEPLOYMENT_ISPCONFIG.md](DEPLOYMENT_ISPCONFIG.md)

**Para servidores sin ISPConfig:**

```bash
# Habilitar módulos necesarios
sudo a2enmod proxy
sudo a2enmod proxy_http
sudo a2enmod proxy_wstunnel
sudo a2enmod rewrite
sudo a2enmod headers
sudo a2enmod ssl

# Crear archivo de configuración
sudo nano /etc/apache2/sites-available/abcd.cloud.diagonaleyewear.net.conf

# Pegar contenido de apache2.conf (de este proyecto)
# Salvar: Ctrl+X → Y → Enter

# Habilitar sitio
sudo a2ensite abcd.cloud.diagonaleyewear.net.conf

# Verificar sintaxis
sudo apache2ctl configtest
# Debe retornar: "Syntax OK"

# Instalar Let's Encrypt
sudo apt-get install certbot python3-certbot-apache -y
sudo certbot certonly --apache -d abcd.cloud.diagonaleyewear.net

# Reiniciar Apache
sudo systemctl restart apache2
```

---

## Opción 2: Usar Nginx (Alternativa a Apache2)

**Si prefieres Nginx en lugar de Apache2:**

```bash
# Instalar Nginx
sudo apt-get install nginx certbot python3-certbot-nginx -y

# Crear archivo de configuración de Nginx
sudo nano /etc/nginx/sites-available/abcd.diagonaleyewear.net
```

Pega el contenido del archivo `nginx.conf` de este proyecto (o cópialo):

```bash
# Habilitar el sitio
sudo ln -s /etc/nginx/sites-available/abcd.diagonaleyewear.net \
           /etc/nginx/sites-enabled/

# Verificar configuración
sudo nginx -t

# Reiniciar Nginx
sudo systemctl restart nginx

# Obtener certificado SSL (Let's Encrypt)
sudo certbot certonly --nginx -d abcd.diagonaleyewear.net
```

---

## Opción 3: Usar Heroku (Rápido, pero limitado)

### Paso 1: Instalar Heroku CLI

```bash
curl https://cli.heroku.com/install.sh | sh
heroku login
```

### Paso 2: Crear App en Heroku

```bash
heroku create abcd-control
heroku config:set DB_HOST=10.3.0.13 DB_PORT=5432 ...
```

### Paso 3: Deploy

```bash
git push heroku main
```

---

## Opción 4: AWS / Google Cloud (Para Escalabilidad)

### AWS Elastic Container Service (ECS)

1. Subir imagen a ECR (Elastic Container Registry)
2. Crear tarea ECS con la imagen
3. Crear servicio ECS
4. Configurar ALB (Application Load Balancer)
5. Mapear dominio con Route 53

**Script de ejemplo:**
```bash
aws ecr create-repository --repository-name abcd-control
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

docker build -t abcd-control:latest .
docker tag abcd-control:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/abcd-control:latest
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/abcd-control:latest
```

---

## Mantenimiento y Monitoreo

### Comandos Útiles

```bash
# Ver estado del contenedor
docker-compose ps

# Ver logs en tiempo real
docker-compose logs -f abcd-app --tail=100

# Reiniciar el servicio
docker-compose restart abcd-app

# Detener el servicio
docker-compose stop

# Eliminar contenedor (datos persistentes en BD se mantienen)
docker-compose down

# Actualizar a nueva versión
git pull origin main
docker-compose build --no-cache
docker-compose up -d
```

### Monitoreo

**Verificar que la app está respondiendo:**
```bash
curl -I http://localhost:8501
# Debe retornar: HTTP/1.1 200 OK

# Desde el exterior
curl -I https://abcd.diagonaleyewear.net
```

**Ver consumo de recursos:**
```bash
docker stats abcd-app
```

**Configurar alertas (opcional):**
- Prometheus para métricas
- Alertmanager para notificaciones

---

## Troubleshooting

### Error: "Connection refused to database"
```bash
# Verificar conexión a BD
docker-compose exec abcd-app psql -h 10.3.0.13 -U user_sg_informatica -d DiagonalDBProd -c "SELECT 1"

# Ver logs
docker-compose logs abcd-app | grep -i database
```

### Error: "Port 8501 already in use"
```bash
# Usar otro puerto
docker-compose down
docker-compose up -d  # O editar docker-compose.yml y cambiar port

# O liberar el puerto
sudo lsof -i :8501
sudo kill -9 <PID>
```

### Imagen muy grande
```bash
# Limpiar imágenes no usadas
docker image prune -a

# Verificar tamaño
docker image ls | grep abcd-control
```

---

## Configuración de Producción

### 1. Variables de Entorno Seguras

**NO usar credenciales en código, usar secretos:**

```bash
# En servidor
echo "xy8fPpxPerETSQ" | docker secret create db_password -
echo "user_sg_informatica" | docker secret create db_user -

# En docker-compose.yml (para Swarm)
secrets:
  db_password:
    external: true
  db_user:
    external: true
```

### 2. Backups Automáticos

```bash
# Crear script de backup
cat > /opt/abcd-control/backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/abcd-control"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Backup de BD (si es necesario)
pg_dump -h 10.3.0.13 -U user_sg_informatica -d DiagonalDBProd \
  > $BACKUP_DIR/backup_$TIMESTAMP.sql

# Guardar versión actual
docker-compose config > $BACKUP_DIR/docker-compose_$TIMESTAMP.yml

# Limpiar backups antiguos (mantener 7 días)
find $BACKUP_DIR -name "backup_*" -mtime +7 -delete
find $BACKUP_DIR -name "docker-compose_*" -mtime +7 -delete

echo "Backup completado: $TIMESTAMP"
EOF

chmod +x /opt/abcd-control/backup.sh

# Cron job diario
echo "0 2 * * * /opt/abcd-control/backup.sh" | sudo crontab -
```

### 3. Actualización Automática

```bash
# Script de actualización
cat > /opt/abcd-control/update.sh << 'EOF'
#!/bin/bash
cd /opt/abcd-control
git pull origin main
docker-compose build --no-cache
docker-compose up -d
EOF

# Cron job semanal (lunes a las 3:00 AM)
echo "0 3 * * 1 /opt/abcd-control/update.sh" | sudo crontab -
```

---

## Checklist de Deployment

- [ ] Servidor Linux con Docker instalado
- [ ] Certificado SSL válido
- [ ] Archivo `.env` con credenciales seguras
- [ ] BD PostgreSQL accesible desde el servidor
- [ ] Nginx configurado como reverse proxy
- [ ] HTTPS habilitado
- [ ] Logs configurados
- [ ] Backups automatizados
- [ ] Monitoreo en place
- [ ] Documentación actualizada

---

## Soporte

Para errores o problemas:
1. Revisar logs: `docker-compose logs abcd-app`
2. Verificar conectividad a BD
3. Checkear certificado SSL: `ssl-test.diagonaleyewear.net`
4. Consultar documentación oficial Streamlit/Docker
