# ClokyOS Pilot - Despliegue Mac Mini M4

## Vista rapida

Plataforma SaaS de gestion empresarial con modulo POS + Facturacion Electronica DIAN.
Desplegada en Mac Mini M4 (24GB RAM) con Docker Compose.

## Stack

| Componente | Version | Note |
|---|---|---|
| PHP | 8.3 | Upgrade de PHP 8.1 |
| Laravel | 11 | Upgrade de Laravel 9 |
| PostgreSQL | 16 | Reemplazo de MySQL/MariaDB |
| Redis | 7 | Cache + Sessions |
| Nginx | 1.25 | Reverse proxy + SSL |

## Quick Start

```bash
# 1. Clonar repositorio (si no existe)
cd /home/jesus/cloky-elite-telegram-bot/workspace/clokyos-pilot

# 2. Ejecutar setup
bash scripts/setup.sh

# 3. Acceder
# HTTPS: https://localhost
# API:   https://localhost/api/
```

## Comandos utiles

```bash
# Ver estado
docker compose ps

# Ver logs
docker compose logs -f app
docker compose logs -f db
docker compose logs -f nginx

# Reiniciar servicio
docker compose restart app

# Migrar base de datos
docker compose exec app php artisan migrate

# Clear cache
docker compose exec app php artisan cache:clear
docker compose exec app php artisan config:clear
docker compose exec app php artisan route:clear

# Detener todo
docker compose down

# Detener y eliminar datos
docker compose down -v
```

## Estructura de archivos

```
clokyos-pilot/
├── docker-compose.yml      # Orquestacion principal
├── .env.example            # Variables de entorno
├── app_build/
│   └── Dockerfile          # PHP 8.3-FPM image
├── app_data/               # Codigo fuente Laravel (POS)
├── nginx/
│   ├── nginx.conf          # Config principal
│   └── conf.d/
│       └── clokyos.conf    # Server block Laravel
├── php-custom.ini          # PHP config (OPcache, Redis sessions)
├── secrets/
│   ├── db_password.txt     # DB password (secret)
│   └── jwt_secret.txt      # JWT secret (secret)
├── ssl/                    # SSL certs (autogenerados)
├── scripts/
│   └── setup.sh            # Script de despliegue
└── docs/
    └── arquitectura.md     # Documentacion tecnica
```

## Migracion de POS (Laravel 9) a Laravel 11

### Cambios principales

1. **PHP 8.1 -> 8.3**: Compatible directo
2. **MySQL -> PostgreSQL**: Migracion de drivers y queries
3. **Laravel 9 -> 11**:
   - `app/Http/Kernel.php`: Middleware groups simplificados
   - `routes/`: web.php y api.php separados (ya existen)
   - `config/`: nuevas opciones de session
4. **Multi-tenancy**: `pos_user_{userId}` -> schema PostgreSQL

### Pasos de migracion

```bash
# 1. Instalar Laravel 11 en app_data
cd app_data
composer create-project laravel/laravel:^11.0 .

# 2. Copiar codigo existente (controllers, models, etc)
# (preservando estructura app/ de Laravel 9)

# 3. Migrar DB de MySQL a PostgreSQL
# Instalar extension pgsql en Dockerfile
# Actualizar .env: DB_CONNECTION=pgsql

# 4. Actualizar helpers.php (StartDBConnect)
# PostgreSQL no necesita DB::purge() para cambiar schema

# 5. Ejecutar migraciones
php artisan migrate
```

## Multi-tenancy con PostgreSQL

### Actual (MySQL)
- 42+ bases de datos: `pos_user_1`, `pos_user_2`, etc.
- Cambio dinamico con `DB::purge()` + `Config::set()`
- Race condition en requests concurrentes

### Nuevo (PostgreSQL)
- 1 base de datos, multiples schemas
- Schema principal: `pos_main` (usuarios, licencias)
- Schemas por tenant: `pos_tenant_1`, `pos_tenant_2`, etc.
- Cambio con `SET search_path TO pos_tenant_1` (sin purge)
- Sin race condition (search_path es por session)

## Seguridad

- JWT tokens: 60s expiration (igual que actual)
- Redis sessions: secure + httponly + samesite=Strict
- Nginx: rate limiting en API (10r/s) y login (3r/m)
- SSL: TLSv1.2+ solo
- .env en secrets volume (no expuesto)
- LOAD_INFILE desactivado (PostgreSQL nativo)

## Proximo modulo: Facturacion DIAN

Fase 2: Agregar modulo de facturacion electronica.

```bash
# Copiar facturaElectronicaApi2020 como servicio adicional
cp -r ../private_repos/facturaElectronicaApi2020/app_data/factura/
# Actualizar docker-compose con nuevo servicio
# Migrar de MySQL a PostgreSQL
# Actualizar conexiones SOAP a DIAN
```

## Monitoreo

```bash
# Health check manual
curl -k https://localhost/health

# Ver logs de errores
docker compose logs -f nginx | grep error

# Monitor de conexiones DB
docker compose exec db psql -U clokyos -c "SELECT count(*) FROM pg_stat_activity;"

# Redis memory
docker compose exec redis redis-cli info memory
```

## Troubleshooting

### Puertos en uso
```bash
# Ver que usa el puerto
lsof -i :80
# Matar proceso
kill -9 <PID>
```

### DB no conecta
```bash
# Verificar DB health
docker compose exec db pg_isready
# Ver logs DB
docker compose logs db
```

### PHP-FPM no responde
```bash
# Ver logs PHP
docker compose logs app
# Reiniciar
docker compose restart app
```

### SSL error en navegador
```bash
# Certificado autofirmado - aceptar manualmente
# O usar IP local en lugar de localhost
# O generar cert con mkcert
```
