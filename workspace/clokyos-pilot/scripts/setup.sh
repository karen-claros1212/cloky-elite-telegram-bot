#!/bin/bash
# ClokyOS Pilot - Setup Script for Mac Mini M4
# Uso: bash scripts/setup.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================
# Pre-flight checks
# ============================================
log_info "=== ClokyOS Pilot Setup ==="

# Check Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker no esta instalado. Instala Docker Desktop para Mac ARM primero."
    exit 1
fi

# Check Docker Compose
if ! docker compose version &> /dev/null 2>&1 && ! docker-compose version &> /dev/null 2>&1; then
    log_error "Docker Compose no esta disponible."
    exit 1
fi

# Check ports
for port in 80 443 5432 6379; do
    if lsof -i :$port &> /dev/null; then
        log_warn "El puerto $port ya esta en uso."
    fi
done

# ============================================
# Generate secrets
# ============================================
log_info "Generando secretos..."

if [ ! -f secrets/db_password.txt ]; then
    openssl rand -base64 32 > secrets/db_password.txt
    log_info "DB password generado"
fi

if [ ! -f secrets/jwt_secret.txt ]; then
    openssl rand -base64 32 > secrets/jwt_secret.txt
    log_info "JWT secret generado"
fi

# ============================================
# Generate SSL certificates (self-signed)
# ============================================
log_info "Generando certificados SSL..."

if [ ! -f ssl/clokyos.crt ] || [ ! -f ssl/clokyos.key ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout ssl/clokyos.key \
        -out ssl/clokyos.crt \
        -subj "/C=CO/ST=Colombia/L=Bogota/O=MasControl/CN=localhost" 2>/dev/null
    log_info "Certificados SSL generados"
else
    log_info "Certificados SSL existentes encontrados"
fi

# ============================================
# Copy and configure environment
# ============================================
log_info "Configurando entorno..."

if [ ! -f .env ]; then
    cp .env.example .env
    log_info ".env creado desde .env.example"
else
    log_info ".env existente encontrado"
fi

# ============================================
# Copy POS source code
# ============================================
log_info "Copiando codigo fuente del POS..."

POS_SOURCE="/home/jesus/pentest/mascontrol/private_repos/pos"

if [ ! -d "$POS_SOURCE" ]; then
    log_error "POS source no encontrado en $POS_SOURCE"
    log_error "Asegurate de que el repositorio pos/ este en /home/jesus/pentest/mascontrol/private_repos/pos/"
    exit 1
fi

# Copy app code (excluding node_modules, vendor, storage)
rsync -av --exclude='node_modules' --exclude='vendor' --exclude='.git' \
    --exclude='storage/logs/*' \
    --exclude='*.log' \
    "$POS_SOURCE/" app_data/

log_info "Codigo fuente copiado a app_data/"

# ============================================
# Build and start
# ============================================
log_info "Iniciando servicios..."

# Build images
docker compose build --no-cache

# Start services
docker compose up -d

log_info "Esperando que los servicios inicien..."
sleep 10

# Check health
log_info "Verificando salud de servicios..."
docker compose ps

# ============================================
# Database setup
# ============================================
log_info "Configurando base de datos..."

# Run migrations
docker compose exec -T app php artisan migrate --force || {
    log_warn "Migraciones fallaron. Revisar logs."
}

# Generate app key
docker compose exec -T app php artisan key:generate --force || {
    log_warn "App key generation fallida. Correr manualmente: docker compose exec app php artisan key:generate"
}

# ============================================
# Final status
# ============================================
echo ""
log_info "=========================================="
log_info "  ClokyOS Pilot esta listo!"
log_info "=========================================="
echo ""
echo "  URLs:"
echo "    - HTTPS:  https://localhost"
echo "    - HTTP:   http://localhost"
echo "    - API:    https://localhost/api/"
echo "    - Health: https://localhost/health"
echo ""
echo "  Servicios:"
docker compose ps --format table | head -20
echo ""
echo "  Logs: docker compose logs -f"
echo "  Stop: docker compose down"
echo "  Restart: docker compose restart"
echo ""
log_warn "Nota: Certificado SSL autofirmado. Aceptar en navegador."
