# Importar las fichas conocidas de los 14 competidores

Las URLs localizadas manualmente se probaron sobre bases temporales, por lo que producción (100 productos, 14 competidores)
no las tiene. `price_known_urls.py` las carga como **fichas candidatas pendientes de verificar**.

## Qué hace y qué no

- Hace: registra en `competitor_product_urls` una fila por ficha (`status=pending_verification`, `match_status=manual_review`,
  `source=known_urls`) y, si falta, la fila vacía de `competitor_web_config`.
- No hace: peticiones HTTP, ofertas, precios, conectores activados ni planificador. Una candidata no cuenta como oferta comparable.
- Idempotente: `INSERT OR IGNORE`; no sobrescribe filas existentes. Si un competidor ya tiene otra ficha vigente para ese EAN,
  la operación se omite.
- Se omite (y se informa) cualquier ficha cuyo competidor no esté registrado, cuyo dominio o URL no sea válido, cuyo EAN no esté
  en el catálogo o cuyo modelo/color no coincida. La ficha PR 26ZS de Mia Burton solo se asocia a su EAN `8056597875226`;
  si ese EAN no está en el catálogo de producción se omitirá y se verá en el informe.

## Auditoría previa (solo lectura)

```
docker exec <contenedor> python price_known_urls.py --audit
```

Muestra recuentos de tablas, planificadores activos, estados de `competitor_product_urls` y `integrity_check`.
Si indica «VACÍA», las URLs están ausentes (no es un problema de visualización).

## Aplicar en producción

Con autorización expresa, tras desplegar la rama:

1. Simulación (no escribe): `docker exec <contenedor> python price_known_urls.py --dry-run`
2. Aplicar con copia previa:
   `docker exec <contenedor> python price_known_urls.py --apply --backup-dir /app/data/backups`
   Comprueba integridad, crea la copia con la API de backup de SQLite (muestra ruta y sha256), escribe y vuelve a comprobar.
3. Verificar: repetir `--audit` (esperado: `status=pending_verification source=known_urls` y `schedule_enabled=0`);
   en Cloud, pestaña Competidores (fichas por competidor) y Comparativa → Almacenados con EAN `8056597875349`
   (debe mostrar el producto, las fichas y «falta comprobar el precio»).
4. Repetir `--apply` no crea duplicados.

## Reversión

- Lógica: `docker exec <contenedor> python price_known_urls.py --revert --backup-dir /app/data/backups`
  elimina solo filas `source=known_urls`, `pending_verification` y sin precio; no toca fichas verificadas, manuales ni histórico.
- Física: parar el contenedor, copiar el fichero de backup sobre la base de datos, borrar `-wal` y `-shm`, arrancar y ejecutar `--audit`.

## Pestaña Competidores

Cada competidor tiene dos secciones: **Configuración web** (dominio, estrategia, URLs conocidas y su estado, último precio y
disponibilidad, IVA y mercado, problemas de acceso, añadir/sustituir/verificar URL), utilizable con el planificador desactivado
y sin feed; y **Actualización automática** (conector, frecuencia, límites, pausas, autorización, activación).
