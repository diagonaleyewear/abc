# Multi-stage build para optimizar el tamaño de imagen
FROM python:3.11-slim as builder

WORKDIR /app

# Instalar dependencias del sistema necesarias
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage final
FROM python:3.11-slim

WORKDIR /app

# Instalar solo lo necesario en runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Madrid

# Copiar dependencias instaladas desde el builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Copiar archivos de la aplicación
COPY app_enhanced.py .
COPY app_exports.py .
COPY app_navigation.py .
COPY app_pages ./app_pages
COPY adyen_reconciliation.py .
COPY adyen_reconciliation_ui.py .
COPY kering_images.py .
COPY kering_images_ui.py .
COPY kering_portal.py .
COPY kering_jobs.py .
COPY image_repository.py image_naming.py image_exports.py kering_media.py .
COPY market_publication.py .
COPY repository_storage.py image_work_storage.py .
COPY image_thumbnails.py .
COPY image_library.py cloud_design.py .
COPY provider_images.py provider_images_ui.py provider_odoo_orders.py repository_catalog_readonly.py .
COPY marcolin_images.py safilo_images.py nuorder_images.py .
COPY scripts/provider_requests_migrate.py ./scripts/provider_requests_migrate.py
COPY migrate_image_repository.py .
COPY scripts/nfs_repository_guard.py ./scripts/nfs_repository_guard.py
COPY scripts/verify_staging_exports.py scripts/test_staging_storage.py ./scripts/
COPY scripts/report_migration_review.py scripts/snapshot_sqlite_copy.py ./scripts/
COPY scripts/smoke_order_alerts.py ./scripts/
COPY scripts/smoke_luxoptica_stop.py ./scripts/
COPY scripts/monitor_stop_fixture ./scripts/monitor_stop_fixture
COPY deployment/nfs/staging-layout.tsv ./deployment/nfs/staging-layout.tsv
COPY scripts/smoke_image_storage.py ./scripts/smoke_image_storage.py
COPY scripts/smoke_image_thumbnails.py ./scripts/smoke_image_thumbnails.py
COPY scripts/regenerate_image_thumbnails.py ./scripts/regenerate_image_thumbnails.py
COPY scripts/cloud_image_entrypoint.sh ./scripts/cloud_image_entrypoint.sh
COPY cloud_dashboard.py process_activity.py build_info.py .
COPY price_comparator.py vytria_price_sync.py price_connectors.py price_scheduler.py price_analytics.py price_web.py price_web_extract.py price_web_fetch.py .
COPY price_references.py price_observations.py competitor_requirements.py .
COPY assets ./assets
COPY logo ./logo
COPY scripts/write_build_info.py ./scripts/write_build_info.py
ARG BUILD_COMMIT
ARG BUILD_VERSION
ARG BUILD_PUBLISHED
ARG BUILD_ID
RUN BUILD_COMMIT="$BUILD_COMMIT" BUILD_VERSION="$BUILD_VERSION" BUILD_PUBLISHED="$BUILD_PUBLISHED" BUILD_ID="$BUILD_ID" \
    python scripts/write_build_info.py
COPY scripts/validate_kering_portal.py ./scripts/validate_kering_portal.py
COPY db_loader.py .
COPY engine.py .
COPY abcd_snapshots.py .
COPY auth_session.py .
COPY db_config.py .
COPY transform_luxottica_masterdata.py .
COPY validate_masterdata_odoo_dryrun.py .
COPY graph_mail_downloader.py .
COPY save_abcd_weekly_snapshot.py .
COPY watchlist_config.py .
COPY check_pedidos_vigilados.py .
COPY run_alerta_pedidos.py .
COPY order_alerts.py .
COPY luxoptica_auto_upload.py .
COPY poll_luxoptica_mail.py .
COPY service_stop.py .

# Crear directorio para configuración de Streamlit
RUN mkdir -p ~/.streamlit

# Configurar PATH
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV PYTHONUNBUFFERED=1

# Configurar Streamlit
RUN echo "\
[theme]\n\
primaryColor = '#417B7B'\n\
base = 'light'\n\
\n\
[server]\n\
port = 8501\n\
headless = true\n\
maxUploadSize = 200\n\
enableXsrfProtection = true\n\
\n\
[logger]\n\
level = info\n\
" > ~/.streamlit/config.toml

# Navegador requerido por la subida automatica a Luxottica.
RUN python -m playwright install --with-deps chromium
RUN (getent group 100 >/dev/null || groupadd --gid 100 users) \
    && useradd --uid 1037 --gid 100 --create-home cloud-images \
    && cp -r /root/.streamlit /home/cloud-images/.streamlit \
    && chown -R 1037:100 /home/cloud-images/.streamlit \
    && chmod 0700 /home/cloud-images /home/cloud-images/.streamlit

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=5)" || exit 1

# Comando para ejecutar la aplicación
ENTRYPOINT ["sh", "/app/scripts/cloud_image_entrypoint.sh"]
CMD ["streamlit", "run", "app_enhanced.py", "--server.address", "0.0.0.0"]
