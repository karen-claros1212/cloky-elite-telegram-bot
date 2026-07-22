# Cloky Elite Telegram Bot v2.2.1

Bot Telegram autónomo + Claude Code CLI + Backend Anthropic-compatible local.

Arquitectura: **Telegram → bot.py → Claude Code CLI → llama-server :8080**

Sin proxy intermedio. Sin adapter_proxy.py. Sin FORCE_PERMISSION_MODE.

## Cambios v2.2.0 → v2.2.1

**Streaming:** Los deltas parciales, texto de asistente y resultado final se separan en buckets distintos. El resultado final tiene prioridad sobre el streaming parcial. No se mezclan más.

**Fallback seguro:** Se eliminó el walker recursivo que caminaba campos arbitrarios del JSON. Ahora solo lee rutas tipadas: `result`, `summary`, `message`, `content`, `text`.

**Sentinel "No response requested.":** Cuando aparece, el bot termina el turno sin reintentar, sin enviar "Please continue", y recomienda `/newsession`.

**Health retry:** Si `/health` devuelve 503 al arrancar, reintentá 5 veces antes de warning.

**Modos:** `FORCE_PERMISSION_MODE` eliminado de `.env` (v2.2.1 no lo implementa). `/bypass` persiste `bypassPermissions`.

**Sesiones:** `/newsession` registra `NATIVE_SESSION_ROTATE`. El primer mensaje usa `FIRST_TURN`, los siguientes `RESUME`. Sin `--fork-session`.

## Comandos Telegram

```
/start /help   - Comandos + modo activo
/status        - Estado de la tarea activa
/cancel        - Cancelar operación actual
/stop          - Terminar proceso
/clear         - Rotar sesión nativa de Claude Code
/newsession    - Crear nueva sesión nativa (sin tocar historial)
/sessions      - Listar sesiones nativas
/cd <nombre>   - Cambiar proyecto activo
/cwd /pwd      - Mostrar workspace actual
/projects      - Listar proyectos disponibles
/plan          - Modo plan (aprobación antes de ejecutar)
/edit          - Modo edit (aprobación de cambios)
/auto          - Modo auto (sin prompts)
/bypass        - Modo bypass (autonomía total)
/health        - Probar backend end-to-end
/config        - Tokens, timeouts, modo
/version       - Versión bot + Python + PID
/uptime        - Tiempo desde último reinicio
/tasks         - Tareas activas
/stats         - Métricas acumuladas
/compact       - Reducir historial de sesión
<texto libre>  - Tarea para Claude Code
```

## Instalación

```bash
cd ~/cloky-elite-telegram-bot

# Copiar config
cp .env.example .env
# Editar .env con tu TOKEN y API_KEY

# Instalar servicio systemd
bash install_service.sh
systemctl --user daemon-reload
systemctl --user start cloky-elite-telegram-bot.service

# Verificar
journalctl --user -u cloky-elite-telegram-bot.service -f
```

## Configuración mínima (.env)

```bash
TELEGRAM_BOT_TOKEN="tu_token_de_botfather"
ALLOWED_TELEGRAM_USER_ID="8166253211"
ANTHROPIC_BASE_URL="http://127.0.0.1:8080"
ANTHROPIC_API_KEY="sk-tu_key"
CLAUDE_DEFAULT_PERMISSION_MODE="bypassPermissions"
```

## Instalación / migración desde v2.2.0

```bash
systemctl --user stop cloky-elite-telegram-bot.service

cd ~/cloky-elite-telegram-bot
cp bot.py bot.py.bak.v2.2.0
cp install_service.sh install_service.sh.bak.v2.2.0

# Copiar archivos nuevos
# (bot.py, install_service.sh actualizados a v2.2.1)

# Reiniciar
systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service

# Verificar
journalctl --user -u cloky-elite-telegram-bot.service -n 50 --no-pager
```

## Pruebas

```bash
cd ~/cloky-elite-telegram-bot
python3 -m py_compile bot.py
python3 -m unittest discover -s tests -v
```

## Notas técnicas

- **Backend directo:** bot.py se conecta directamente a `:8080` (llama-server). No usa proxy intermedio.
- **Sesiones nativas:** Claude Code persiste transcript en `~/.claude/projects/<encoded>/<UUID>.jsonl`. El bot usa `--session-id` (primer turno) y `--resume` (continuación).
- **Streaming token-a-token:** Usa `--include-partial-messages` y procesa eventos `stream_event` con `text_delta`.
- **Sentinel:** "No response requested." se detecta y descarta automáticamente.
- **Fallback tipado:** Solo lee campos documentados del protocolo stream-json.

## Licencia

MIT
