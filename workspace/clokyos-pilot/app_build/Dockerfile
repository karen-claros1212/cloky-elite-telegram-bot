FROM php:8.3-fpm

# Versiones
ARG LARAVEL_VERSION=11
ARG PHP_VERSION=8.3

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    git \
    curl \
    libpng-dev \
    libonig-dev \
    libxml2-dev \
    libzip-dev \
    zip \
    unzip \
    libpq-dev \
    libssl-dev \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Instalar extensiones PHP
RUN docker-php-ext-install pdo_pgsql pgsql mbstring exif pcntl bcmath gd zip

# Instalar Composer
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

# Configurar directorio de trabajo
WORKDIR /var/www/html

# Configurar permisos
RUN useradd -m -s /bin/bash clokyos
RUN chown -R clokyos:clokyos /var/www/html

# Exponer puerto para PHP-FPM
EXPOSE 9000

# CMD por defecto (se sobrescribe en docker-compose)
CMD ["php-fpm"]
