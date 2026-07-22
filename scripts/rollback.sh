#!/usr/bin/env bash
set -euo pipefail
BACKUP="${1:?Uso: rollback.sh /home/USER/cloky-backups/cloky-TIMESTAMP}"
TARGET="${2:-$HOME/cloky-elite-telegram-bot}"
SERVICE="$HOME/.config/systemd/user/cloky-elite-telegram-bot.service"
[[ -f "$BACKUP/app.tar.gz" ]] || { echo "Backup inválido"; exit 2; }
systemctl --user stop cloky-elite-telegram-bot.service 2>/dev/null || true
mkdir -p "$TARGET"
tar -C "$TARGET" -xzf "$BACKUP/app.tar.gz"
[[ -f "$BACKUP/service.unit" ]] && cp "$BACKUP/service.unit" "$SERVICE"
systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service
systemctl --user status cloky-elite-telegram-bot.service --no-pager
