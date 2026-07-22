# Cloky Elite Telegram Bot v1.5 — Sandbox Configurable

Bot Telegram autónomo + Claude Code CLI + Backend Anthropic-compatible local.

Fecha: 2026-05-10. Versión: 1.5.0.

## Cambios v1.4 → v1.5

**Sandbox systemd ahora es OPCIONAL** vía variable `BOT_SANDBOX_MODE`:

| Modo | Filesystem | Casos de uso |
|------|------------|--------------|
| `strict` (default) | `ProtectHome=tmpfs` + BindPaths solo workspace y `~/.claude` | Producción autónoma, máxima seguridad |
| `open` | `ProtectHome=false` — acceso completo a `$HOME` | Auditar/refactorizar proyectos del usuario |

**Lo que SIEMPRE permanece activo (cualquier modo):**

- `NoNewPrivileges=true` (no escalada a root vía sudo/setuid)
- `CapabilityBoundingSet=` vacío (cero capabilities)
- `SystemCallFilter` restrictivo (sin `@privileged @resources @mount @swap @reboot`)
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`
- `RestrictNamespaces=true`, `LockPersonality=true`, `RestrictSUIDSGID=true`
- Bloqueo pre-spawn de 11 comandos destructivos (`rm -rf /`, `mkfs`, `dd /dev/sd*`, etc.)
- Sanitización anti-prompt-injection
- Allowlist Telegram por user_id
- Redacción de tokens en logs y mensajes

## Cambiar entre modos

```bash
# Activar acceso completo a $HOME
BOT_SANDBOX_MODE=open bash install_service.sh
systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service

# Volver a modo estricto
BOT_SANDBOX_MODE=strict bash install_service.sh
systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service
```

El modo elegido se persiste en `~/cloky-elite-telegram-bot/.sandbox_mode` y se reaplica en reinstalaciones futuras a menos que se especifique uno nuevo.

## Visibilidad del modo activo

El bot muestra el modo en:

- `/version` → `Sandbox: open` o `Sandbox: strict`
- `/config` → línea `sandbox_mode: open|strict|unknown`
- `/help` → encabezado destaca modo activo
- `journalctl` al arrancar → `SANDBOX_MODE open` con warning si es OPEN

## Implicaciones del modo OPEN

**Lo que el bot PUEDE hacer ahora:**
- Leer/escribir en cualquier archivo de `/home/$USER/*`
- Auditar VIPER, Taurus, BoviSense, CobraVivo
- Refactorizar código en cualquier proyecto
- Leer `~/.ssh/`, `~/.aws/`, archivos `.env` de otros servicios
- Modificar configuraciones de Agent Zero, Morgan/OpenClaw

**Lo que el bot SIGUE SIN poder hacer:**
- Escalar a root (NoNewPrivileges + CapabilityBoundingSet=)
- Reiniciar servicios systemd (RestrictNamespaces)
- Cargar módulos del kernel
- Modificar `/etc`, `/usr`, `/bin` (no por sandbox, sino por permisos UNIX normales del usuario `jesus`)
- Cambiar hostname o clock
- Apagar la máquina
- Ejecutar los 11 comandos destructivos bloqueados pre-spawn

## Lo que NUNCA modifica (en ningún modo)

- llama.cpp / TurboQuant / Qwen (config, binarios, modelos cargados)
- Otros bots Telegram (los tokens propios de cada uno)

El bot es **cliente HTTP read-only** del backend `127.0.0.1:8080`.

## Tabla completa de comandos Telegram

```
/start /help   - Comandos + modo sandbox activo
/status        - Estado de la tarea activa
/cancel        - SIGINT (cancelar operación actual, preservar sesión)
/stop          - SIGTERM → escalada SIGKILL en 3s
/clear         - Limpiar historial de contexto
/cwd           - Mostrar workspace
/health        - Probar backend end-to-end
/config        - Tokens, timeouts, modo sandbox
/version       - Versión bot + Python + PID + sandbox mode
/uptime        - Tiempo desde último reinicio
/tasks         - Tareas activas en curso
/stats         - Métricas acumuladas
<texto libre>  - Tarea para Claude Code
```

## Instalación / migración desde v1.4

```bash
systemctl --user stop cloky-elite-telegram-bot.service

cd ~/cloky-elite-telegram-bot
cp bot.py bot.py.bak.v1.4
cp install_service.sh install_service.sh.bak.v1.4

unzip -o /ruta/cloky-elite-telegram-bot-v1.5.zip -d /tmp/v15
cp /tmp/v15/cloky-elite-telegram-bot/bot.py .
cp /tmp/v15/cloky-elite-telegram-bot/install_service.sh .
cp /tmp/v15/cloky-elite-telegram-bot/README.md .

# Elegir modo según tu necesidad
BOT_SANDBOX_MODE=open bash install_service.sh
# o:
# BOT_SANDBOX_MODE=strict bash install_service.sh

systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service
journalctl --user -u cloky-elite-telegram-bot.service -n 30 --no-pager
```

## Validación post-instalación

Desde Telegram a `@Cloky77bot`:

1. `/version` → debe decir `Sandbox: open` (o strict)
2. `/config` → línea `sandbox_mode: open`
3. **Test acceso modo OPEN**: pedile `lista archivos en /home/jesus/projects/taurus`
   - En modo `strict` → "permission denied" o "no existe"
   - En modo `open` → lista los archivos
4. **Test bloqueo destructivo (cualquier modo)**: `rm -rf /` → bloqueado
5. **Test no-escalada (cualquier modo)**: pedile `sudo systemctl restart sshd`
   - Debe fallar — NoNewPrivileges sigue activo

## Score de hardening

```bash
systemd-analyze --user security cloky-elite-telegram-bot.service
```

| Modo | Score esperado | Nivel |
|------|----------------|-------|
| strict | 3-5 | OK / GOOD |
| open | 5-7 | MEDIUM |

El score en `open` baja porque pierde `ProtectHome`, pero sigue por debajo del default de servicios systemd típicos (8-9).

## Estructura

```
cloky-elite-telegram-bot/
├── bot.py                  v1.5 — 1630 líneas, lee BOT_SANDBOX_MODE
├── install_service.sh      v1.5 — genera unit según modo
├── .env.example            Template con todas las variables documentadas
├── claude-settings.json    Para ~/.claude/settings.json
├── CLAUDE.md               Política operativa para Claude Code
├── README.md               Este archivo
├── .sandbox_mode           ← Auto-creado, persiste el modo elegido
├── workspace/              ← Cwd de Claude Code
├── state/                  ← sessions, stats, running PIDs
└── logs/                   ← bot.log rotado automáticamente
```

## Garantías técnicas

- Sintaxis Python validada (1630 líneas)
- Sintaxis Bash validada
- Tests funcionales en ambos modos
- Backward compat con v1.4 (todos los patches y comandos siguen)
- Zero comandos contra llama.cpp / TurboQuant / Qwen
- El modo elegido persiste entre reinicios y reinstalaciones
- Logs reflejan el modo activo desde el arranque

## Auditoría rápida del bot en modo OPEN

```bash
# Verificar que el unit tiene ProtectHome=false
systemctl --user cat cloky-elite-telegram-bot.service | grep -E "ProtectHome|BindPaths|NoNewPrivileges|CapabilityBoundingSet"

# Verificar que el bot ve /home completo
PID=$(systemctl --user show -p MainPID --value cloky-elite-telegram-bot.service)
sudo ls /proc/$PID/root/home/jesus 2>&1 | head -20
# Esperado en open: lista normal de directorios
# Esperado en strict: solo cloky-elite-telegram-bot

# Verificar que NO puede escalar a root (incluso en open)
sudo cat /proc/$PID/status | grep -E "^Cap|^NoNewPrivs"
# Esperado: CapBnd: 0000000000000000, NoNewPrivs: 1
```
