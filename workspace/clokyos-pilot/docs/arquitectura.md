# ClokyOS - Arquitectura del Sistema

## Visao General

ClokyOS es una plataforma SaaS de gestion empresarial multi-tenant que combina:
1. **Punto de Venta (POS)** - Gestion de inventario, ventas, clientes
2. **Facturacion Electronica DIAN** - UBL 2.1, firma XAdES, envio a DIAN
3. **Multi-tenancy** - Schemas compartidos en PostgreSQL

## Diagrama de Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│                      Mac Mini M4 (24GB RAM)                    │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Nginx (Reverse Proxy + SSL)                 │  │
│  │  Port 80 -> 443 | Rate Limiting | Gzip | Static Assets  │  │
│  └────────────────────────┬────────────────────────────────┘  │
│                           │                                    │
│  ┌────────────────────────▼────────────────────────────────┐  │
│  │              PHP 8.3-FPM (Laravel 11)                    │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │  │
│  │  │   POS       │  │   INVOICING  │  │   AUTH        │  │  │
│  │  │  Service    │  │   Service    │  │   Service     │  │  │
│  │  └─────────────┘  └──────────────┘  └───────────────┘  │  │
│  │                                                         │  │
│  │  ┌─────────────┐  ┌──────────────┐                     │  │
│  │  │   QUEUE     │  │   MULTI-     │                     │  │
│  │  │   Worker    │  │   TENANCY    │                     │  │
│  │  └─────────────┘  └──────────────┘                     │  │
│  └────────────────────────┬────────────────────────────────┘  │
│                           │                                    │
│  ┌────────────────────────▼────────────────────────────────┐  │
│  │              PostgreSQL 16                               │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │  │
│  │  │ pos_main    │  │ pos_tenant_1 │  │ pos_tenant_2  │  │  │
│  │  │ (users,     │  │ (data)       │  │ (data)        │  │  │
│  │  │  licenses)  │  └──────────────┘  └───────────────┘  │  │
│  │  └─────────────┘                                        │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Redis 7                                     │  │
│  │  Cache | Sessions | Queue | Rate Limiting               │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Multi-tenancy: Schema vs Database

### Antes (MySQL - Actual)
```
pos_user_1  -> DB 1
pos_user_2  -> DB 2
pos_user_3  -> DB 3
...
pos_user_42 -> DB 42
```
**Problemas:**
- 42+ conexiones DB simultaneas max
- Backup individual por tenant
- Cambio de schema requiere `DB::purge()` (race condition)
- Migraciones en lote complejas

### Nuevo (PostgreSQL)
```
clokyos_pos (1 DB)
├── pos_main          -> usuarios, licencias, suscripciones
├── pos_tenant_1      -> datos tenant 1
├── pos_tenant_2      -> datos tenant 2
├── pos_tenant_3      -> datos tenant 3
...
└── pos_tenant_42     -> datos tenant 42
```
**Ventajas:**
- 1 sola conexion DB
- Backup centralizado
- Cambio de schema: `SET search_path` (sin race condition)
- Migraciones atomicas por tenant
- PostgreSQL handles 1000+ schemas eficientemente

## Flujo de Request

```
Cliente (Browser/App)
    |
    v
Nginx (SSL + Rate Limit)
    |
    v
PHP-FPM (Laravel 11)
    |
    +---> Auth Middleware (JWT)
    |         |
    |         v
    |    Validar token + obtener tenant_id
    |
    +---> Multi-tenancy Middleware
    |         |
    |         v
    |    SET search_path = pos_tenant_{tenant_id}
    |
    +---> Controller
    |         |
    |         v
    |    Business Logic
    |         |
    |         v
    +---> Query (auto-scoped a tenant)
              |
              v
         PostgreSQL (search_path scoped)
              |
              v
         Response (JSON/HTML)
```

## Migracion de Datos

### Paso 1: Crear estructura PostgreSQL
```sql
-- DB principal
CREATE DATABASE clokyos_pos;

-- Schema principal
CREATE SCHEMA pos_main;
GRANT ALL ON SCHEMA pos_main TO clokyos;

-- Schema para tenant 1 (ejemplo)
CREATE SCHEMA pos_tenant_1;
GRANT ALL ON SCHEMA pos_tenant_1 TO clokyos;
```

### Paso 2: Migrar datos de MySQL
```bash
# Para cada tenant en MySQL:
mysqldump -u root -p pos_user_1 \
  --skip-lock-tables \
  --single-transaction | \
  psql -U clokyos clokyos_pos -d pos_tenant_1
```

### Paso 3: Actualizar codigo Laravel
```php
// Antes (MySQL)
function StartDBConnect($database) {
    DB::purge('mysql');
    Config::set('database.connections.mysql.database', $database);
    DB::reconnect('mysql');
}

// Nuevo (PostgreSQL)
function setTenantSchema($tenantId) {
    DB::statement("SET search_path TO pos_tenant_{$tenantId}");
}
```

## Seguridad

### JWT
- Algorithm: HS256
- Expiration: 60s (igual que actual)
- Refresh: 1440s (24h)
- Storage: Redis (blacklist en logout)

### Rate Limiting
- API: 10 req/s por IP
- Login: 3 req/min por IP
- Burst: 20x para API, 5x para login

### SSL/TLS
- TLSv1.2 minimo
- TLSv1.3 preferido
- Self-signed para piloto
- Wildcard para produccion

### Sessions
- Driver: Redis
- Secure cookie: true
- HttpOnly: true
- SameSite: Strict

## Escalabilidad

### Horizontal (futuro)
```
                    ┌─────────────┐
                    │ Load Balancer│
                    │ (Nginx/HAProxy)│
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              v            v            v
         ┌─────────┐ ┌─────────┐ ┌─────────┐
         │ App 1   │ │ App 2   │ │ App 3   │
         └─────────┘ └─────────┘ └─────────┘
              │            │            │
              └────────────┼────────────┘
                           v
                  ┌────────────────┐
                  │ PostgreSQL     │
                  │ (Primary/Replica)│
                  └────────────────┘
```

### Vertical (Mac Mini M4)
- 24GB RAM: suficiente para piloto
- Limitacion: no hyperthreading en M4 base
- Recomendacion: max 4 containers de app simultaneos

## Monitoreo

### Metrics
- Response time (Nginx logs)
- Queue length (Redis)
- DB connections (pg_stat_activity)
- Memory usage (docker stats)

### Alerts
- Container down (docker compose ps)
- DB connection limit > 80%
- Queue depth > 100
- Response time > 5s
