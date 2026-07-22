#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-$HOME/cloky-elite-telegram-bot}"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/cloky-elite-telegram-bot.service"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/cloky-backups/cloky-$TIMESTAMP"
STAGE="$(mktemp -d /tmp/cloky-v3.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$TARGET" "$SERVICE_DIR" "$HOME/cloky-backups"

if [[ ! -f "$TARGET/.env" ]]; then
  echo "ERROR: falta $TARGET/.env. No se desplegará sin conservar la configuración local."
  exit 2
fi

# Validate source before touching production.
cp -a "$SOURCE_DIR/." "$STAGE/app"
python3 -m venv "$STAGE/venv"
"$STAGE/venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$STAGE/venv/bin/pip" install -r "$STAGE/app/requirements.txt"
"$STAGE/venv/bin/pip" install -e "$STAGE/app"
"$STAGE/venv/bin/python" -m pytest -q "$STAGE/app/tests"
"$STAGE/venv/bin/python" -m py_compile "$STAGE/app"/cloky/*.py

# Backup current code and service. Secrets stay local and are never printed.
mkdir -p "$BACKUP"
if [[ -d "$TARGET" ]]; then
  tar --exclude='.venv' --exclude='logs' --exclude='workspace/uploads' -C "$TARGET" -czf "$BACKUP/app.tar.gz" . || true
fi
if [[ -f "$SERVICE_FILE" ]]; then
  cp "$SERVICE_FILE" "$BACKUP/service.unit"
fi

systemctl --user stop cloky-elite-telegram-bot.service 2>/dev/null || true

# Preserve mutable/local data.
cp "$TARGET/.env" "$STAGE/local.env"
[[ -d "$TARGET/state" ]] && cp -a "$TARGET/state" "$STAGE/old-state" || true
[[ -d "$TARGET/logs" ]] && cp -a "$TARGET/logs" "$STAGE/old-logs" || true
[[ -d "$TARGET/workspace" ]] && cp -a "$TARGET/workspace" "$STAGE/old-workspace" || true

# Replace code, not local state.
find "$TARGET" -mindepth 1 -maxdepth 1 \
  ! -name '.env' ! -name 'state' ! -name 'logs' ! -name 'workspace' ! -name '.git' \
  -exec rm -rf {} +
cp -a "$SOURCE_DIR/cloky" "$SOURCE_DIR/tests" "$SOURCE_DIR/scripts" "$TARGET/"
cp "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/README.md" \
   "$SOURCE_DIR/CHANGELOG.md" "$SOURCE_DIR/SECURITY.md" "$SOURCE_DIR/.gitignore" \
   "$SOURCE_DIR/.env.example" "$TARGET/"
cp "$STAGE/local.env" "$TARGET/.env"
chmod 600 "$TARGET/.env"
mkdir -p "$TARGET/state" "$TARGET/logs" "$TARGET/workspace/uploads"

# Build final venv at its permanent path so entry-point shebangs are correct.
rm -rf "$TARGET/.venv"
python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$TARGET/.venv/bin/pip" install -r "$TARGET/requirements.txt"
"$TARGET/.venv/bin/pip" install -e "$TARGET"
"$TARGET/.venv/bin/python" "$TARGET/scripts/migrate_v2_state.py" "$TARGET"
"$TARGET/.venv/bin/python" -m pytest -q "$TARGET/tests"

CLAUDE_PATH="$(command -v claude || true)"
if [[ -z "$CLAUDE_PATH" ]]; then
  echo "ERROR: Claude Code no está en PATH. Restaurá desde $BACKUP"
  exit 3
fi
NODE_BIN_DIR="$(dirname "$(command -v node || echo /usr/bin/node)")"
SERVICE_PATH="$NODE_BIN_DIR:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=Cloky Enterprise Telegram Bot v3.0.0-rc1
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$TARGET
EnvironmentFile=-$TARGET/.env
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$SERVICE_PATH
ExecStart=$TARGET/.venv/bin/python -m cloky.app
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now cloky-elite-telegram-bot.service
sleep 3
systemctl --user is-active --quiet cloky-elite-telegram-bot.service

cat <<OUT
DEPLOY_OK
Target: $TARGET
Backup: $BACKUP
Service: active
Next:
  $TARGET/.venv/bin/python -m cloky.doctor --full
  journalctl --user -u cloky-elite-telegram-bot.service -n 100 --no-pager
OUT
