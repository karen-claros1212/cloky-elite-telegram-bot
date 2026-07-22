#!/usr/bin/env bash
#
# install_service.sh — Cloky Elite Telegram Bot v2.2.1
#
# Arquitectura: Telegram → bot.py → Claude Code CLI → llama-server :8080
# Sin proxy intermedio. Sin adapter_proxy.py.
#
set -euo pipefail

APP_DIR="$HOME/cloky-elite-telegram-bot"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/cloky-elite-telegram-bot.service"
SANDBOX_MODE_FILE="$APP_DIR/.sandbox_mode"

# Determinar modo de sandbox.
# Prioridad: 1) env var BOT_SANDBOX_MODE, 2) archivo $APP_DIR/.sandbox_mode, 3) default "strict"
if [ -n "${BOT_SANDBOX_MODE:-}" ]; then
  SANDBOX_MODE="$BOT_SANDBOX_MODE"
  echo "$SANDBOX_MODE" > "$SANDBOX_MODE_FILE" 2>/dev/null || true
elif [ -f "$SANDBOX_MODE_FILE" ]; then
  SANDBOX_MODE="$(cat "$SANDBOX_MODE_FILE" | tr -d '[:space:]')"
else
  SANDBOX_MODE="strict"
fi

case "$SANDBOX_MODE" in
  strict|open) ;;
  *)
    echo "ERROR: BOT_SANDBOX_MODE inválido: '$SANDBOX_MODE'. Valores: strict | open"
    exit 1
    ;;
esac

echo "============================================================"
echo "  Cloky Bot v2.2.1"
echo "============================================================"

mkdir -p "$SERVICE_DIR"

# --- Verificar .env ---
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$APP_DIR/.env.example" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "Created $APP_DIR/.env (chmod 600)"
    echo "Edit TELEGRAM_BOT_TOKEN before starting:"
    echo "  nano $APP_DIR/.env"
    exit 0
  else
    echo "ERROR: ni .env ni .env.example existen en $APP_DIR"
    exit 1
  fi
fi

chmod 600 "$APP_DIR/.env" 2>/dev/null || true
chmod +x "$APP_DIR/bot.py"

# --- Detectar python3 (PATH puede ser distinto bajo systemd --user) ---
PYTHON_BIN="$(command -v python3)"
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 no encontrado en PATH"
  exit 1
fi
echo "PYTHON_BIN=$PYTHON_BIN"

# --- Detectar claude CLI ---
CLAUDE_PATH="$(command -v claude || true)"
if [ -z "$CLAUDE_PATH" ]; then
  echo "WARN: 'claude' no encontrado en PATH actual."
  echo "      Verificá que esté accesible para el servicio user:"
  echo "      systemctl --user show-environment | grep PATH"
fi

# --- Detectar Node (Claude Code CLI lo necesita) ---
NODE_BIN_DIR=""
if [ -d "$HOME/.nvm/versions/node" ]; then
  NODE_BIN_DIR="$(find "$HOME/.nvm/versions/node" -maxdepth 2 -name bin -type d 2>/dev/null | head -1)"
fi
if [ -z "$NODE_BIN_DIR" ] && command -v node >/dev/null 2>&1; then
  NODE_BIN_DIR="$(dirname "$(command -v node)")"
fi

# --- Construir PATH para la unit ---
SERVICE_PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.npm-global/bin"
if [ -n "$NODE_BIN_DIR" ]; then
  SERVICE_PATH="$NODE_BIN_DIR:$SERVICE_PATH"
fi

# --- Asegurar subdirectorios ---
mkdir -p "$APP_DIR/workspace" "$APP_DIR/state" "$APP_DIR/logs"

# v1.2: Asegurar que ~/.claude existe ANTES de instalar el servicio.
# BindPaths fallaría si el path origen no existe en el host.
mkdir -p "$HOME/.claude"

# v1.4: Asegurar también otros paths que Claude Code 2.x puede necesitar.
mkdir -p "$HOME/.config/claude-code" 2>/dev/null || true
mkdir -p "$HOME/.cache/claude-code" 2>/dev/null || true

# v1.4: Permisos correctos en state/ (rotación de logs, stats, sessions)
chmod 700 "$APP_DIR/state" "$APP_DIR/logs" 2>/dev/null || true
chmod 700 "$APP_DIR/.env" 2>/dev/null || true

# --- Construir bloque de filesystem según SANDBOX_MODE ---

if [ "$SANDBOX_MODE" = "strict" ]; then
  FILESYSTEM_BLOCK="$(cat <<'STRICT_EOF'
# === Sandbox: STRICT (default) ===
# Oculta /home/* y reexpone solo lo necesario.
ProtectHome=tmpfs
BindReadOnlyPaths=APP_DIR_PLACEHOLDER
BindPaths=APP_DIR_PLACEHOLDER/workspace
BindPaths=APP_DIR_PLACEHOLDER/state
BindPaths=APP_DIR_PLACEHOLDER/logs
BindPaths=-%h/.claude
BindPaths=-%h/.config/claude-code
BindPaths=-%h/.cache/claude-code
BindReadOnlyPaths=-%h/.npm-global
BindReadOnlyPaths=-%h/.nvm
BindReadOnlyPaths=-%h/.local
STRICT_EOF
)"
  FILESYSTEM_BLOCK="${FILESYSTEM_BLOCK//APP_DIR_PLACEHOLDER/$APP_DIR}"

  # v1.6 S2.3: en modo strict, leer ALLOWED_PROJECTS de .env y exponerlos
  # como BindPaths para que /cd <proyecto> funcione dentro del sandbox.
  if [ -f "$APP_DIR/.env" ]; then
    PROJECTS_LINE="$(grep -E '^ALLOWED_PROJECTS=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    if [ -n "$PROJECTS_LINE" ]; then
      echo "Exponiendo proyectos de ALLOWED_PROJECTS via BindPaths:"
      IFS=',' read -ra ENTRIES <<< "$PROJECTS_LINE"
      for entry in "${ENTRIES[@]}"; do
        if [[ "$entry" == *:* ]]; then
          proj_path="${entry#*:}"
          proj_path="$(echo "$proj_path" | xargs)"  # trim
          if [ -d "$proj_path" ]; then
            FILESYSTEM_BLOCK="${FILESYSTEM_BLOCK}"$'\n'"BindPaths=${proj_path}"
            echo "  + $proj_path"
          else
            echo "  - $proj_path (no existe, skip)"
          fi
        fi
      done
    fi
  fi
else
  # MODO OPEN: home completo accesible
  FILESYSTEM_BLOCK="$(cat <<'OPEN_EOF'
# === Sandbox: OPEN ===
# El bot tiene acceso de lectura/escritura a TODO $HOME.
# Permite auditoría/refactorización de cualquier proyecto del usuario.
# El usuario asume responsabilidad del scope ampliado.
# Para volver a strict:
#   BOT_SANDBOX_MODE=strict bash install_service.sh
#   systemctl --user daemon-reload && systemctl --user restart cloky-elite-telegram-bot.service
ProtectHome=false
# Sin BindPaths: /home/USER queda accesible normalmente.
# El workspace original sigue siendo el cwd por defecto, pero Claude Code
# puede navegar a cualquier path con cd.
OPEN_EOF
)"
fi

# --- Generar unit file ---
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Cloky Elite Telegram Bot v2.2.1 — Claude Code local bridge
After=network.target
Documentation=https://github.com/anthropics/claude-code

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$SERVICE_PATH
Environment=BOT_SANDBOX_MODE=$SANDBOX_MODE
ExecStart=$PYTHON_BIN $APP_DIR/bot.py
Restart=always
RestartSec=5

# v1.4: graceful shutdown — bot.py escucha SIGTERM y termina limpio.
KillSignal=SIGTERM
TimeoutStopSec=30
KillMode=mixed

$FILESYSTEM_BLOCK

# === Privilegios y namespaces (activos en AMBOS modos) ===
# Estos no dependen del sandbox de filesystem. Son siempre seguros.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true

# Red: solo TCP/UDP locales y a Telegram API.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

# Filtro de syscalls.
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @debug @mount @swap @reboot @raw-io
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native

# Capabilities: ninguna heredada. NO se puede escalar a root.
CapabilityBoundingSet=
AmbientCapabilities=

# Recursos.
MemoryMax=4G
TasksMax=256
LimitNOFILE=4096

[Install]
WantedBy=default.target
EOF

chmod 644 "$SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user enable cloky-elite-telegram-bot.service

echo
echo "=================================================================="
echo "  Service installed: $SERVICE_FILE"
echo "  Sandbox mode: $SANDBOX_MODE"
echo "=================================================================="
echo
echo "Verificación del modo aplicado:"
systemctl --user show cloky-elite-telegram-bot.service \
  -p ProtectHome,NoNewPrivileges,RestrictNamespaces,RestrictAddressFamilies,CapabilityBoundingSet \
  2>/dev/null | sed 's/^/  /'

if [ "$SANDBOX_MODE" = "open" ]; then
  echo
  echo "  ⚠  MODO OPEN ACTIVO:"
  echo "  ⚠  - El bot puede leer/escribir en TODO \$HOME (/home/$(whoami)/)"
  echo "  ⚠  - Incluye: VIPER, Taurus, BoviSense, CobraVivo, .ssh, .env de otros"
  echo "  ⚠  - Sigue ACTIVO: bloqueo destructivo (rm -rf /), sanitize, allowlist"
  echo "  ⚠  - NO puede escalar a root (NoNewPrivileges, CapabilityBoundingSet=)"
fi

echo
echo "Comandos útiles:"
echo "  Iniciar:        systemctl --user restart cloky-elite-telegram-bot.service"
echo "  Estado:         systemctl --user status cloky-elite-telegram-bot.service"
echo "  Logs:           journalctl --user -u cloky-elite-telegram-bot.service -f"
echo "  Score sandbox:  systemd-analyze --user security cloky-elite-telegram-bot.service"
echo "  Detener:        systemctl --user stop cloky-elite-telegram-bot.service"
echo
echo "Cambiar modo de sandbox:"
echo "  BOT_SANDBOX_MODE=strict bash install_service.sh   # restaurar modo seguro"
echo "  BOT_SANDBOX_MODE=open   bash install_service.sh   # acceso a todo HOME"
echo
echo "Tras cambiar modo, recordar:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user restart cloky-elite-telegram-bot.service"
echo
echo "El servicio NO modifica llama.cpp / TurboQuant / Qwen."
echo "Solo consume http://127.0.0.1:8080 como cliente HTTP."
echo "=================================================================="
