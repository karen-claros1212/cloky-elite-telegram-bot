#!/usr/bin/env python3
"""
Cloky Elite Telegram Bot v2.2.1 — fix del comando de sesión
============================================================

HOTFIX v2.2.0 → v2.2.1
----------------------
v2.2.0 quitó --fork-session (correcto) pero dejó --session-id junto a
--resume. Claude Code rechaza esa combinación y sale con código 1:

    Error: --session-id can only be used with --continue or --resume
           if --fork-session is also specified.

Los flags son MUTUAMENTE EXCLUYENTES:
    Turno 1  (no hay .jsonl):  --session-id <UUID>   → crea la sesión
    Turno 2+ (hay .jsonl):     --resume <UUID>       → la continúa

Agregados 4 tests que construyen el comando real y verifican que la
combinación ilegal no pueda producirse en ningún caso.

Bridge: Telegram → Claude Code CLI → llama.cpp local → Qwen.

Una auditoría externa revisó v2.1.0 y encontró 8 defectos reales. Todos
verificados en el código y corregidos acá. El más grave era de gestión de
sesiones y explica las conversaciones que "no avanzaban".

1. FORK EN CADA MENSAJE  (crítico)
   Cada continuación usaba --resume + --fork-session. El fork crea un
   session_id NUEVO al reanudar, pero el bot no lo capturaba: seguía
   guardando el ID padre y reanudándolo en el turno siguiente. Resultado:
   turnos ignorados, ramas repetidas, transcript inconsistente.
   → Ahora: primer turno --session-id, continuación solo --resume.

2. AFIRMACIÓN NO PROBADA sobre stdin
   v2.1.0 declaraba el stdin como "causa raíz". El fallo por stdin ilegible
   era de Windows anterior a 2.1.211; en Linux con 2.1.215 Claude Code
   advierte y continúa. Enviar por stdin es correcto, pero no está probado
   que fuera la causa. Comentario corregido.

3. CONTADOR DE TOKENS ROTO
   Los frames de usage son SNAPSHOTS acumulativos, no incrementos. El código
   los sumaba: 100/10 → 100/20 → 100/30 daba 300/60 en vez de 100/30.
   Verificado numéricamente. El test previo era un FALSO POSITIVO: validaba
   una reimplementación en el test, no el código de producción.
   → Ahora: max() sobre los snapshots, y el test ejercita la lógica real.

4. SENTINEL "No response requested."
   No se detectaba y entraba al historial como si fuera respuesta.
   → Ahora: is_sentinel_output() lo reconoce y lo descarta.

5. PARSER RECURSIVO INSEGURO
   Caminaba campos arbitrarios del JSON, así que cualquier campo desconocido
   podía terminar en el chat.
   → Ahora: extracción TIPADA de los campos documentados únicamente
     (message.content[].text, stream_event.event.delta.text, result.result,
     error.message). Los bloques tool_use/tool_result se ignoran.

6. /compact CORROMPÍA SESIONES  (peligroso)
   Truncaba strings dentro del .jsonl interno de Claude Code. Ese formato es
   interno y cambia entre versiones: truncarlo puede romper tool_results,
   bloques de thinking, firmas y metadata de reanudación — dejando la sesión
   imposible de reanudar. Causa plausible de los fallos que perseguíamos.
   → Ahora: /compact DESACTIVADO. compact_transcript() eliminada del código.
     El historial solo se LEE (/context). Para aliviar: /newsession.

7. SIN STREAMING TOKEN A TOKEN
   Faltaba --include-partial-messages, así que llegaban bloques completos.
   → Ahora: flag agregado y eventos stream_event/text_delta procesados.

8. TESTS QUE NO PROBABAN NADA
   Varios verificaban que una línea existiera en el código fuente.
   → Ahora 50 tests, y los de usage/parser ejercitan la lógica real.

TESTS
    python3 -m unittest discover -s tests -v   → Ran 50 / OK

NO MODIFICA
    llama.cpp / TurboQuant / Qwen / chat template / Claude Code CLI.
    Otros bots, Agent Zero, Morgan/OpenClaw, VIPER, Taurus.

v2.2.2 (2026-07-22) — mejoras del bot existente
-------------------------------------------------
1. Streaming: partial_text solo como último recurso en composición final.
   result y assistant se priorizan para no mezclar deltas parciales.
2. send_message() verifica resultado real de sendMessage; loguea fallos.
3. /newsession: rotate_native_session_id persiste correctamente (verificado).
   El siguiente mensaje usa FIRST_TURN automáticamente.
4. Modos: get_user_mode → set_user_mode persiste ANTES de lanzar Claude.
5. fallback_from_outputs: un solo pase con campos documentados (result,
   summary, message, content, text). Sin walker recursivo.
6. Sin dead-letter ni bloqueo largo ante 409. Retry x3 con backoff exponencial.
7. Version actualizada.

NO SE CAMBIA:
   Arquitectura Telegram → Cloky → Claude Code → llama-server :8080.
   No se despliega v3 ni ClaudeAgentSDK.
   Se conservan todas las funciones existentes.
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ============================================================================
# Versión y constantes
# ============================================================================

VERSION = "2.2.2"
VERSION_DATE = "2026-07-22"
APP_NAME = "cloky-elite-telegram-bot"

# v1.5: Modo de sandbox. Inyectado por systemd unit como Environment=BOT_SANDBOX_MODE=...
# Si no está, asume "unknown" — el bot funciona igual, solo afecta la UI informativa.
SANDBOX_MODE = os.environ.get("BOT_SANDBOX_MODE", "unknown").lower()
BASE_DIR = Path(os.environ.get("BOT_HOME", str(Path.home() / "cloky-elite-telegram-bot"))).resolve()
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
WORKSPACE_DIR = Path(os.environ.get("CLAUDE_WORKSPACE", str(BASE_DIR / "workspace"))).resolve()

# v1.6.1 IMP-3: mkdir con error claro si fallan permisos.
# (Antes: crash silencioso al import sin contexto.)
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError) as _e:
    print(f"FATAL: no se pudo crear directorios base: {_e}", file=sys.stderr)
    print(f"  STATE_DIR={STATE_DIR}", file=sys.stderr)
    print(f"  LOG_DIR={LOG_DIR}", file=sys.stderr)
    print(f"  WORKSPACE_DIR={WORKSPACE_DIR}", file=sys.stderr)
    print(f"  Verificá permisos del usuario y que BOT_HOME exista.", file=sys.stderr)
    sys.exit(2)

LOG_FILE = LOG_DIR / "bot.log"
SESSION_FILE = STATE_DIR / "sessions.json"
RUNNING_FILE = STATE_DIR / "running.json"        # v1.4: PIDs activos para recuperación
STATS_FILE = STATE_DIR / "stats.json"            # v1.4: métricas acumuladas
NATIVE_SESSIONS_FILE = STATE_DIR / "native_sessions.json"  # v1.7: mapping user+cwd → claude session_id
USER_MODES_FILE = STATE_DIR / "user_modes.json"            # v1.8: modo activo por usuario
LAST_PROMPT_FILE = STATE_DIR / "last_prompts.json"         # v1.8: último prompt por usuario para Plan→Approve
START_TIME = time.time()                         # v1.4: para /uptime

# v1.4: Rotación de logs
LOG_MAX_BYTES = 10 * 1024 * 1024                 # 10 MB por archivo
LOG_BACKUP_COUNT = 3                             # bot.log + .1 + .2 + .3


# ============================================================================
# .env loader (sin python-dotenv para mantener stdlib pura)
# ============================================================================

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")


# ============================================================================
# Variables runtime
# ============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_TELEGRAM_USER_ID = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()

QWEN_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
QWEN_API_KEY = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_ID = os.environ.get(
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf",
)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")

# v1.3: Defaults alineados a documentación oficial Claude Code (2026-05-10).
# CLAUDE_TIMEOUT_SECONDS: 1800s (30 min) ≥ BASH_MAX_TIMEOUT_MS para no matar
# subprocess antes de que bash complete una operación larga.
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "1800"))

# Default oficial 32000, máximo 64000. Issue #24159 documenta bug en Opus 4.6
# con 64K. 32000 es el sweet spot verificado.
CLAUDE_MAX_OUTPUT_TOKENS = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000")

# 8 es seguro para Ryzen 9 9950X3D (16C/32T). El default no documentado es 4.
CLAUDE_TOOL_CONCURRENCY = os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "8")

# v1.3: Variables adicionales para Bash y MCP (propagadas al subprocess).
BASH_DEFAULT_TIMEOUT_MS = os.environ.get("BASH_DEFAULT_TIMEOUT_MS", "600000")     # 10 min
BASH_MAX_TIMEOUT_MS = os.environ.get("BASH_MAX_TIMEOUT_MS", "1800000")            # 30 min
BASH_MAX_OUTPUT_LENGTH = os.environ.get("BASH_MAX_OUTPUT_LENGTH", "50000")
MAX_THINKING_TOKENS = os.environ.get("MAX_THINKING_TOKENS", "31999")
MCP_TIMEOUT = os.environ.get("MCP_TIMEOUT", "60000")
MCP_TOOL_TIMEOUT = os.environ.get("MCP_TOOL_TIMEOUT", "120000")
MAX_MCP_OUTPUT_TOKENS = os.environ.get("MAX_MCP_OUTPUT_TOKENS", "32000")

POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "45"))
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_INTERVAL_SECONDS", "20"))
# v1.7: MAX_CONTEXT_MESSAGES ya NO se usa para construir el prompt (Claude Code lo hace
# nativamente con --resume). Se mantiene como límite del log informativo en sessions.json
# del bot (para /status, /stats, no para inyectar contexto).
MAX_CONTEXT_MESSAGES = int(os.environ.get("MAX_CONTEXT_MESSAGES", "12"))
MAX_TELEGRAM_CHUNK = 3900

# === v1.7 Persistencia nativa Claude Code ===
# Tope de turnos por ejecución de claude. Sin esto, una tarea puede consumir
# context window indefinidamente. 50 es generoso pero acotado.
CLAUDE_MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "50"))

# v1.8.0: Botones inline en Telegram
INLINE_KEYBOARD_ENABLED = os.environ.get("INLINE_KEYBOARD_ENABLED", "true").lower() in {"1", "true", "yes"}

# Permission mode por defecto. v1.8.0 lo hará configurable por usuario en runtime.
# Por ahora todos los usuarios arrancan en bypassPermissions (= comportamiento v1.6.1).
# Valores válidos: "default" | "plan" | "acceptEdits" | "bypassPermissions"
CLAUDE_DEFAULT_PERMISSION_MODE = os.environ.get("CLAUDE_DEFAULT_PERMISSION_MODE", "bypassPermissions").strip()
if CLAUDE_DEFAULT_PERMISSION_MODE not in {"default", "plan", "acceptEdits", "bypassPermissions"}:
    print(
        f"WARN: CLAUDE_DEFAULT_PERMISSION_MODE inválido: {CLAUDE_DEFAULT_PERMISSION_MODE!r}, "
        f"usando 'bypassPermissions'",
        file=sys.stderr,
    )
    CLAUDE_DEFAULT_PERMISSION_MODE = "bypassPermissions"

# === v1.6 Sprint 2: streaming, archivos, multi-proyecto ===

# S2.1 Streaming incremental
# Telegram rate limit: ~30 msg/seg total, pero editMessageText puede dispararse
# anti-spam (429 Too Many Requests) si se hace muy seguido al mismo mensaje.
# 1.5-2 segundos entre edits es el sweet spot reportado.
STREAM_ENABLED = os.environ.get("STREAM_ENABLED", "true").lower() in {"1", "true", "yes"}
STREAM_EDIT_INTERVAL = float(os.environ.get("STREAM_EDIT_INTERVAL", "1.5"))  # segundos
STREAM_MIN_CHUNK_LEN = int(os.environ.get("STREAM_MIN_CHUNK_LEN", "200"))    # chars antes de empezar a editar
STREAM_PREVIEW_LIMIT = 3500  # chars máximo en mensaje en vivo (deja margen sobre 4096)

# S2.2 Manejo de archivos
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
try:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError) as _e:
    print(f"WARN: uploads_dir no creado: {_e}", file=sys.stderr)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))  # 50 MB

# S2.3 Multi-proyecto
# Lista de proyectos accesibles via /cd. Cada uno debe existir en disco.
# Sin esto, /cd queda deshabilitado y todo trabajo va al workspace por defecto.
# Formato env var: "name1:path1,name2:path2,name3:path3"
# Ejemplo: ALLOWED_PROJECTS="cobravivo:/home/jesus/projects/cobravivo,viper:/home/jesus/VIPER_HFT_Bot"
ALLOWED_PROJECTS: dict[str, Path] = {}
_raw_projects = os.environ.get("ALLOWED_PROJECTS", "").strip()
if _raw_projects:
    for _entry in _raw_projects.split(","):
        _entry = _entry.strip()
        if ":" not in _entry:
            continue
        _name, _path = _entry.split(":", 1)
        _name = _name.strip()
        _path = _path.strip()
        if _name and _path:
            ALLOWED_PROJECTS[_name] = Path(_path).resolve()

# Estado actual de cwd por usuario (overrides WORKSPACE_DIR cuando se setea via /cd).
_user_cwd: dict[str, Path] = {}
_user_cwd_lock = threading.Lock()

# v1.2: Autonomía de Claude Code.
# Cuando es True, Claude Code ejecuta tools sin pedir confirmación humana.
# El sandbox systemd (Patch 6) y FORBIDDEN_PATHS_REGEX son las defensas reales.
CLAUDE_AUTO_APPROVE = os.environ.get("CLAUDE_AUTO_APPROVE", "true").lower() in {"1", "true", "yes"}

# v1.2: Defensa en profundidad. Aunque el sandbox systemd ya oculta /home/*
# fuera del workspace, este filtro pre-Popen actúa como segunda capa:
# si el prompt del usuario menciona explícitamente estos paths, se rechaza
# antes de invocar Claude Code. Solo evita comandos destructivos OBVIOS;
# no pretende ser exhaustivo.
FORBIDDEN_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-[rf]{1,2}\s+/(?:\s|\*|;|\&|\||\Z)"),   # rm -rf / (no rm -rf /tmp)
    re.compile(r"\brm\s+-[rf]{1,2}\s+/\*"),                      # rm -rf /*
    re.compile(r"\brm\s+-[rf]{1,2}\s+~(?:\s|/|\Z)"),             # rm -rf ~ o ~/
    re.compile(r"\brm\s+-[rf]{1,2}\s+\$HOME\b"),                 # rm -rf $HOME
    re.compile(r"\bdd\s+.*of=/dev/(sd|nvme|hd|xvd|mmcblk)"),     # dd of=/dev/sda
    re.compile(r"\bmkfs\."),                                      # mkfs.*
    re.compile(r"\b(shutdown|halt|poweroff|reboot)\b"),           # apagar
    re.compile(r":\(\)\s*\{\s*:\|:\&\s*\}\s*;\s*:"),             # fork bomb
    re.compile(r">\s*/dev/(sd|nvme|hd|xvd|mmcblk)[a-z]?[0-9]*\b"),# > /dev/sda
    re.compile(r"\bchmod\s+(?:-R\s+)?777\s+/(?:\s|\Z)"),         # chmod 777 / o chmod -R 777 /
    re.compile(r"\bchown\s+-R\s+\S+\s+/(?:\s|\Z)"),              # chown -R x /
]


if not TELEGRAM_BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is required in .env", file=sys.stderr)
    sys.exit(1)

if not ALLOWED_TELEGRAM_USER_ID:
    print("ERROR: ALLOWED_TELEGRAM_USER_ID is required in .env", file=sys.stderr)
    sys.exit(1)


# ============================================================================
# Redacción de secretos (Patch 1)
# ============================================================================

TOKEN_PATTERNS = [
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),                  # Telegram bot tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                        # OpenAI/Anthropic keys
    re.compile(r"\bfreecc-[A-Za-z0-9_-]+\b"),                        # free-claude-code proxy
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),                          # HuggingFace tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                             # AWS access keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                   # GitHub tokens
    re.compile(r"\bxoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+\b"),              # Slack bot tokens
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),         # Authorization Bearer
    re.compile(r"Authorization:\s*[^\s]+", re.IGNORECASE),
    re.compile(r"ANTHROPIC_(?:API_KEY|AUTH_TOKEN)\s*=\s*[^\s]+"),
    re.compile(r"TELEGRAM_BOT_TOKEN\s*=\s*[^\s]+"),
]


# ============================================================================
# Saneamiento de salida — una sola puerta para todo lo que ve el usuario
# ============================================================================
# Cada patrón acá salió de un incidente REAL en producción, documentado con
# screenshot. No hay heurísticas especulativas.

_ALLOWED_CTRL = {0x09, 0x0A, 0x0D}  # \t \n \r
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_API_ID_RE = re.compile(r"^(chatcmpl|msg|req|resp|call|toolu)[-_][A-Za-z0-9]{8,}$")

# Prompts internos de reintento/continuación. NUNCA deben verse en el chat.
# (Incidente 2026-07-21: se filtró el de "no visible output".)
_INTERNAL_PROMPTS = (
    "your previous response had no visible output",
    "please continue and produce a user-visible response",
    "[continue]",
    "continue and produce",
)


# Sentinels que Claude Code emite como salida sintética, no como respuesta real.
_SENTINELS = ("no response requested.",)


def is_sentinel_output(s: str) -> bool:
    """True si la salida es un sentinel sintético, no una respuesta del modelo."""
    return (s or "").strip().lower().rstrip(".") in tuple(x.rstrip(".") for x in _SENTINELS)


def is_internal_string(s: str) -> bool:
    """True si el string es metadata interna, no texto para el usuario."""
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()
    if any(p in low for p in _INTERNAL_PROMPTS):
        return True
    if is_sentinel_output(s):
        return True
    if _UUID_RE.match(s) or _ISO_TS_RE.match(s) or _API_ID_RE.match(s):
        return True
    if s.startswith("/") and ("/.claude/" in s or "/claude-skills/" in s or s.endswith(".jsonl")):
        return True
    if s.startswith("mcp__") and "__" in s[5:]:
        return True
    if "@claude-code-skills" in s:
        return True
    if s.endswith(".gguf") or s.startswith("Qwen"):
        return True
    if len(s) <= 80 and re.match(r"^[0-9a-f-]+$", s, re.I):
        return True
    if 24 <= len(s) <= 64 and s.isalnum() and not s.isalpha():
        return True
    return False


def is_output_garbage(text: str) -> bool:
    """True si el texto es un volcado binario (incidente 2026-07-19)."""
    if not text:
        return False
    if "\x00" in text or "\ufffd" in text:
        return True
    if len(text) < 24:
        return False
    bad = sum(
        1 for ch in text
        if ord(ch) not in _ALLOWED_CTRL and (ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F)
    )
    if bad / len(text) > 0.15:
        return True
    return len(re.findall(r"\S{1,4}\s{5,}", text)) >= 8


def sanitize_output(text: str) -> str:
    """Quita caracteres de control y colapsa espaciado absurdo."""
    if not text:
        return text
    out = "".join(
        ch for ch in text
        if ord(ch) in _ALLOWED_CTRL or not (ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F)
    )
    out = re.sub(r"[ \t]{12,}", "  ", out)
    return re.sub(r"\n{4,}", "\n\n\n", out)


def redact(text: str) -> str:
    """Redacta tokens y secretos conocidos. Aplicar SIEMPRE antes de log/Telegram."""
    if not text:
        return text
    output = text
    for pattern in TOKEN_PATTERNS:
        output = pattern.sub("[REDACTED]", output)
    return output


# ============================================================================
# Sanitización contra prompt injection (Patch 2)
# ============================================================================

# Tags usados como separadores de rol en build_prompt. Cualquier mensaje que
# los contenga literalmente puede romper la estructura del prompt.
INJECTION_TAG_RE = re.compile(
    r"^\s*\[(system|assistant|user|tool|tool_result|tool_use)\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Patrón típico de override ("ignora las instrucciones previas"). No se elimina,
# solo se marca para auditoría — un usuario legítimo puede estar discutiendo el
# tema sin intención maliciosa.
INSTRUCTION_OVERRIDE_RE = re.compile(
    r"\b(ignora|ignore|disregard|forget|olvida)\s+(las\s+)?(instrucciones|instructions|prompt)\s+(previas|anteriores|previous|above)\b",
    re.IGNORECASE,
)


def sanitize_for_context(text: str) -> str:
    """
    Defensa estructural contra prompt injection.
    No bloquea contenido: neutraliza tags falsificados que rompan los separadores.
    """
    if not text:
        return ""
    sanitized = INJECTION_TAG_RE.sub(
        lambda m: "[user-attempted-tag-" + m.group(1).lower() + "]",
        text,
    )
    if INSTRUCTION_OVERRIDE_RE.search(sanitized):
        sanitized = "[NOTA: posible override de instrucciones detectado]\n" + sanitized
    return sanitized


def check_forbidden_command(text: str) -> str | None:
    """
    v1.2: Detecta comandos destructivos OBVIOS en el texto del usuario.
    Retorna el patrón encontrado o None.
    Esto es defensa en profundidad: el sandbox systemd ya bloquea fuera
    del workspace, pero comandos como 'rm -rf ~' no deberían siquiera
    intentarse.
    """
    if not text:
        return None
    for pattern in FORBIDDEN_COMMAND_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


# ============================================================================
# Logging
# ============================================================================

def _rotate_log_if_needed() -> None:
    """v1.4 S1.5: Rotación de logs cuando bot.log > LOG_MAX_BYTES."""
    try:
        if not LOG_FILE.exists():
            return
        if LOG_FILE.stat().st_size < LOG_MAX_BYTES:
            return
        # Rotar: bot.log.2 → bot.log.3, bot.log.1 → bot.log.2, bot.log → bot.log.1
        for i in range(LOG_BACKUP_COUNT, 0, -1):
            src = LOG_DIR / f"bot.log.{i}"
            dst = LOG_DIR / f"bot.log.{i + 1}"
            if src.exists():
                if i == LOG_BACKUP_COUNT:
                    src.unlink()  # eliminar el más viejo
                else:
                    src.replace(dst)
        LOG_FILE.replace(LOG_DIR / "bot.log.1")
    except Exception:
        pass  # nunca crashear por rotación


def log(message: str) -> None:
    """Log con redacción de tokens y rotación automática."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {redact(message)}"
    print(line, flush=True)
    try:
        _rotate_log_if_needed()
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # nunca crashear por logging


# ============================================================================
# Telegram API
# ============================================================================

def telegram_api(method: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """
    v1.6.1 IMP-1: retry x3 con backoff exponencial para errores transitorios.
    Errores no recuperables (400, 401, 403, 404) NO se reintentan.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"cloky-bot/{VERSION}"}

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx no recuperables — no reintentar
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                try:
                    body = exc.read().decode("utf-8", errors="ignore")[:300]
                except Exception:
                    body = ""
                log(f"TELEGRAM_API_HTTP_ERROR method={method} code={exc.code} body={body}")
                return {"ok": False, "error_code": exc.code, "description": body}
            # 408, 429, 5xx → reintentar
            last_exc = exc
            log(f"TELEGRAM_API_RETRY method={method} attempt={attempt+1}/3 code={exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            log(f"TELEGRAM_API_RETRY method={method} attempt={attempt+1}/3 error={type(exc).__name__}: {exc}")

        # Backoff exponencial: 0.5s, 1.5s, 4.5s
        if attempt < 2:
            time.sleep(0.5 * (3 ** attempt))

    log(f"TELEGRAM_API_GAVE_UP method={method} after 3 attempts: {last_exc!r}")
    return {"ok": False, "error": repr(last_exc)}


def split_text(text: str) -> list[str]:
    """Split respetando límite de Telegram. Corta en \\n cuando es posible."""
    text = (text or "").strip()
    if not text:
        return ["(sin contenido visible)"]
    chunks: list[str] = []
    while len(text) > MAX_TELEGRAM_CHUNK:
        cut = text.rfind("\n", 0, MAX_TELEGRAM_CHUNK)
        if cut < 1000:
            cut = MAX_TELEGRAM_CHUNK
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def send_message(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    notify: bool = True,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    """
    v1.4: añadido parámetro notify para silenciar progreso o resaltar resultados.
    v1.8.0: añadido reply_markup para teclados inline.

    notify=True (default): sonido y vibración normales.
    notify=False: mensaje silencioso (útil para typing/progress).
    reply_markup: si se pasa, se adjunta SOLO al último chunk (los chunks
                  intermedios no llevan teclado para no saturar).
    """
    # BARRERA FINAL: nada llega al usuario sin pasar por acá.
    if text and is_output_garbage(text):
        log(f"OUTPUT_GARBAGE_BLOCKED len={len(text)}")
        text = "⚠️ La salida contenía datos ilegibles y fue descartada."
    else:
        text = sanitize_output(text)

    chunks = split_text(text)
    last_idx = len(chunks) - 1
    for idx, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
            "disable_notification": not notify,
        }
        if idx == 0 and reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        # v1.8.0: el reply_markup va solo en el último chunk
        if idx == last_idx and reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = telegram_api("sendMessage", payload)
        if not result.get("ok"):
            log(f"SEND_MESSAGE_FAIL chunk={idx+1}/{len(chunks)} ok={result.get('ok')} error={result.get('description', '')}")


def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Editar un mensaje existente. Retorna el resultado de la API.
    v1.8.0: soporta reply_markup para refrescar teclado.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:MAX_TELEGRAM_CHUNK],
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return telegram_api("editMessageText", payload, timeout=30)


def answer_callback_query(
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> None:
    """
    v1.8.0: responde un callback_query para cerrar el "loading" en Telegram.
    Siempre se debe llamar tras procesar el callback, sino el botón queda
    girando indefinidamente del lado del usuario.
    """
    telegram_api(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text[:200],  # límite Telegram
            "show_alert": show_alert,
        },
        timeout=15,
    )


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    telegram_api("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=20)



class TypingKeepalive:
    """
    Indicador "escribiendo..." continuo.
    Telegram lo limpia a los 5s; refrescamos cada 4s mientras la tarea corre.
    stop() es idempotente y se llama SIEMPRE en el finally (evita el bug de
    typing infinito).
    """

    REFRESH_SECONDS = 4.0

    def __init__(self, chat_id: int, action: str = "typing") -> None:
        self.chat_id = chat_id
        self.action = action
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while True:
            try:
                send_chat_action(self.chat_id, self.action)
            except Exception:
                pass
            if self._stop.wait(self.REFRESH_SECONDS):
                return

    def start(self) -> "TypingKeepalive":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"typing-{self.chat_id}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(timeout=2.0)
            except Exception:
                pass
        self._thread = None


# ============================================================================
# v1.6 S2.1: Streaming incremental a Telegram
# ============================================================================

class StreamingMessage:
    """
    Maneja una respuesta incremental de Claude a Telegram.
    Envía un mensaje inicial y va editándolo a medida que llegan tokens.

    Rate-limited: solo edita cada STREAM_EDIT_INTERVAL segundos (default 1.5s)
    para evitar 429 Too Many Requests de Telegram.

    Cuando el contenido excede STREAM_PREVIEW_LIMIT, "cierra" el mensaje actual
    y crea uno nuevo (efecto: rolling preview).

    v1.6.1 BUG-6 fix: skip de edits redundantes — Telegram retorna 400
    "message is not modified" si el texto no cambió desde el último edit.
    """

    def __init__(self, chat_id: int, reply_to: int | None = None):
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.message_ids: list[int] = []   # IDs de mensajes "cerrados" + actual
        self.current_text = ""              # buffer del mensaje actual
        self.last_edit_at = 0.0
        self.last_sent_text = ""            # v1.6.1: cache del último texto enviado para skip redundante
        self.enabled = STREAM_ENABLED
        self.lock = threading.Lock()

    def _send_new(self, text: str) -> int | None:
        """Crea un mensaje nuevo. Retorna su message_id."""
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:MAX_TELEGRAM_CHUNK] if text else "…",
            "disable_web_page_preview": True,
            "disable_notification": True,  # streaming es silencioso
        }
        if not self.message_ids and self.reply_to:
            payload["reply_to_message_id"] = self.reply_to
        result = telegram_api("sendMessage", payload)
        if result.get("ok"):
            return result["result"]["message_id"]
        return None

    def append(self, chunk: str, force: bool = False) -> None:
        """
        Agregar chunk al buffer y actualizar Telegram si pasó STREAM_EDIT_INTERVAL.
        force=True: edita ya, ignorando rate limit (usar al final del stream).
        v1.6.1: skip edit si el texto no cambió desde el último envío.
        """
        if not self.enabled or not chunk:
            return
        with self.lock:
            self.current_text += chunk
            now = time.time()
            should_edit = force or (now - self.last_edit_at >= STREAM_EDIT_INTERVAL)
            if not should_edit:
                return

            # Si excedimos el límite del mensaje actual, "cerrarlo" y abrir nuevo
            if len(self.current_text) > STREAM_PREVIEW_LIMIT:
                cut_at = self.current_text.rfind("\n", 0, STREAM_PREVIEW_LIMIT)
                if cut_at < 1000:
                    cut_at = STREAM_PREVIEW_LIMIT
                head = self.current_text[:cut_at].rstrip()
                tail = self.current_text[cut_at:].lstrip()
                if self.message_ids and head != self.last_sent_text:
                    edit_message(self.chat_id, self.message_ids[-1], head)
                    self.last_sent_text = head
                new_id = self._send_new(tail or "…")
                if new_id:
                    self.message_ids.append(new_id)
                self.current_text = tail
                self.last_sent_text = tail
                self.last_edit_at = now
                return

            # v1.6.1 BUG-6 fix: skip si texto idéntico al último enviado
            if self.current_text == self.last_sent_text:
                self.last_edit_at = now  # actualiza ts para no spammear el check
                return

            # Editar el mensaje actual
            if self.message_ids:
                edit_message(self.chat_id, self.message_ids[-1], self.current_text)
            else:
                new_id = self._send_new(self.current_text)
                if new_id:
                    self.message_ids.append(new_id)
            self.last_sent_text = self.current_text
            self.last_edit_at = now

    def finalize(self, full_text: str | None = None) -> None:
        """
        Cierre del stream. Si full_text se pasa, reemplaza el contenido
        del último mensaje con la versión final.
        """
        with self.lock:
            if full_text is not None:
                self.current_text = full_text
            if self.message_ids:
                final = self.current_text[-STREAM_PREVIEW_LIMIT:] if len(self.current_text) > STREAM_PREVIEW_LIMIT else self.current_text
                final = final or "(sin contenido)"
                # v1.6.1: solo editar si cambió
                if final != self.last_sent_text:
                    edit_message(self.chat_id, self.message_ids[-1], final)
                    self.last_sent_text = final


# ============================================================================
# v1.6 S2.2: Descarga de archivos enviados por Telegram
# ============================================================================

def _safe_filename(name: str) -> str:
    """Sanitiza nombres de archivo: solo alfanum, _, -, ., max 200 chars."""
    if not name:
        return "file"
    safe = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
    safe = safe.strip("._")
    return safe[:200] or "file"


def telegram_get_file_path(file_id: str) -> str | None:
    """Resuelve file_id → path relativo en api.telegram.org."""
    result = telegram_api("getFile", {"file_id": file_id}, timeout=20)
    if not result.get("ok"):
        return None
    return result.get("result", {}).get("file_path")


def telegram_download_file(file_id: str, save_to: Path) -> tuple[bool, str]:
    """
    Descarga un archivo desde Telegram a save_to. Devuelve (success, info_string).
    Aplica MAX_UPLOAD_BYTES como cap defensivo.
    v1.6.1 IMP-4: User-Agent agregado.
    """
    file_path = telegram_get_file_path(file_id)
    if not file_path:
        return False, "no se pudo resolver file_id"
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": f"cloky-bot/{VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            if content_length and content_length > MAX_UPLOAD_BYTES:
                return False, f"archivo demasiado grande ({content_length} bytes, máx {MAX_UPLOAD_BYTES})"
            save_to.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with save_to.open("wb") as out:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        return False, f"archivo excede {MAX_UPLOAD_BYTES} bytes durante descarga"
                    out.write(chunk)
            return True, f"{total} bytes"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def extract_file_info(message: dict[str, Any]) -> tuple[str, str, int] | None:
    """
    Extrae file_id, suggested filename, size de un mensaje Telegram con archivo.
    Soporta: document, photo, audio, video, voice.
    """
    if "document" in message:
        doc = message["document"]
        return doc["file_id"], _safe_filename(doc.get("file_name", "document")), int(doc.get("file_size", 0))
    if "photo" in message:
        # photo es array; tomar la mayor resolución
        photos = sorted(message["photo"], key=lambda p: p.get("file_size", 0))
        if photos:
            best = photos[-1]
            ts = int(time.time())
            return best["file_id"], f"photo_{ts}.jpg", int(best.get("file_size", 0))
    if "audio" in message:
        aud = message["audio"]
        name = aud.get("file_name") or f"audio_{int(time.time())}.mp3"
        return aud["file_id"], _safe_filename(name), int(aud.get("file_size", 0))
    if "voice" in message:
        v = message["voice"]
        return v["file_id"], f"voice_{int(time.time())}.ogg", int(v.get("file_size", 0))
    if "video" in message:
        v = message["video"]
        name = v.get("file_name") or f"video_{int(time.time())}.mp4"
        return v["file_id"], _safe_filename(name), int(v.get("file_size", 0))
    return None


# ============================================================================
# v1.6 S2.3: Multi-proyecto con whitelist
# ============================================================================

def get_user_cwd(user_id: str) -> Path:
    """Retorna el cwd actual para un usuario (default WORKSPACE_DIR)."""
    with _user_cwd_lock:
        return _user_cwd.get(user_id, WORKSPACE_DIR)


def set_user_cwd(user_id: str, path: Path) -> None:
    with _user_cwd_lock:
        _user_cwd[user_id] = path


def reset_user_cwd(user_id: str) -> None:
    with _user_cwd_lock:
        _user_cwd.pop(user_id, None)


# ============================================================================
# v1.7 Native Sessions — persistencia nativa Claude Code
# ============================================================================
# Cada (user_id, cwd) tiene un session_id UUID4 estable. Cuando lanzamos claude
# con --session-id <UUID> --resume <UUID>, Claude Code restaura el transcript
# completo de turnos previos desde ~/.claude/projects/<encoded-cwd>/<UUID>.jsonl.
#
# Esto elimina los problemas v1.6.1:
#  - Contexto truncado a 8000 chars por mensaje en sessions.json del bot
#  - Solo 12 turnos visibles para Claude
#  - Tool calls (Read, Bash, Edit, Grep) perdidas entre turnos
#  - Archivos subidos "olvidados" al siguiente mensaje
#
# Estructura del archivo state/native_sessions.json:
# {
#   "8166253211": {                                  # user_id (Telegram)
#     "/home/jesus/cobravivo": "uuid-de-cobravivo",
#     "/home/jesus/viper":     "uuid-de-viper",
#     "/home/jesus/cloky-elite-telegram-bot/workspace": "uuid-default"
#   },
#   ...
# }

import uuid as _uuid  # stdlib

_native_sessions_lock = threading.Lock()


def _load_native_sessions() -> dict[str, dict[str, str]]:
    """Lee el mapping completo user_id -> {cwd: session_id}."""
    if not NATIVE_SESSIONS_FILE.exists():
        return {}
    try:
        with NATIVE_SESSIONS_FILE.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except Exception:
                pass
            try:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {}
                return data
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as exc:
        log(f"NATIVE_SESSIONS_LOAD_ERROR {type(exc).__name__}: {exc}")
        return {}


def _save_native_sessions(data: dict[str, dict[str, str]]) -> None:
    """Escribe el mapping con write-temp-rename atómico + flock."""
    tmp = NATIVE_SESSIONS_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        tmp.replace(NATIVE_SESSIONS_FILE)
    except Exception as exc:
        log(f"NATIVE_SESSIONS_SAVE_ERROR {type(exc).__name__}: {exc}")


def get_native_session_id(user_id: str, cwd: Path) -> str:
    """
    Devuelve el session_id de Claude Code para el par (user_id, cwd).
    Si no existe, lo crea (UUID4 nuevo) y lo persiste antes de retornar.

    El path del cwd se normaliza con resolve() para que /tmp/foo/../foo y
    /tmp/foo sean equivalentes.
    """
    cwd_key = str(cwd.resolve())
    with _native_sessions_lock:
        data = _load_native_sessions()
        user_map = data.setdefault(user_id, {})
        sid = user_map.get(cwd_key)
        if sid:
            return sid
        # No existe: crear nuevo UUID4
        new_sid = str(_uuid.uuid4())
        user_map[cwd_key] = new_sid
        data[user_id] = user_map
        _save_native_sessions(data)
        log(f"NATIVE_SESSION_NEW user={user_id} cwd={cwd_key} sid={new_sid}")
        return new_sid


def rotate_native_session_id(user_id: str, cwd: Path) -> str:
    """
    Fuerza la creación de un session_id NUEVO para (user_id, cwd).
    El viejo session_id queda en disco en ~/.claude/projects/... y puede
    ser recuperado vía /sessions. Esta es la operación detrás de /newsession
    y de /clear (en v1.7 /clear hace rotación, no borrado total).
    """
    cwd_key = str(cwd.resolve())
    with _native_sessions_lock:
        data = _load_native_sessions()
        user_map = data.setdefault(user_id, {})
        old = user_map.get(cwd_key)
        new_sid = str(_uuid.uuid4())
        user_map[cwd_key] = new_sid
        data[user_id] = user_map
        _save_native_sessions(data)
        log(f"NATIVE_SESSION_ROTATE user={user_id} cwd={cwd_key} old={old} new={new_sid}")
        return new_sid


def list_native_sessions(user_id: str) -> dict[str, str]:
    """
    Devuelve el mapping {cwd: session_id} de un usuario.
    Útil para /sessions.
    """
    with _native_sessions_lock:
        data = _load_native_sessions()
        return dict(data.get(user_id, {}))


def _encoded_cwd_dir(cwd: Path) -> Path:
    """
    Devuelve el path del directorio que Claude Code crea en ~/.claude/projects/
    para un cwd dado.

    Claude Code codifica el cwd reemplazando "/" por "-" y prefijando con dash.
    Ej: /home/jesus/cobravivo  →  -home-jesus-cobravivo
    """
    cwd_str = str(cwd.resolve())
    encoded = cwd_str.replace("/", "-")
    if encoded.startswith("-"):
        encoded = encoded[1:]
    encoded = "-" + encoded
    home = Path(os.path.expanduser("~"))
    return home / ".claude" / "projects" / encoded


def native_session_jsonl_path(cwd: Path, session_id: str) -> Path:
    """
    Path canónico del transcript .jsonl para (cwd, session_id).
    Usado por:
      - _run_claude_task_inner para decidir entre primer turno y --resume
      - find_jsonl_transcripts para /sessions
    """
    return _encoded_cwd_dir(cwd) / f"{session_id}.jsonl"


def native_session_has_transcript(cwd: Path, session_id: str) -> bool:
    """
    v1.7.1: True si el transcript .jsonl existe en disco para esta sesión.
    Si es False, --resume no se debe pasar (Claude Code 2.1.150 falla con
    "session not found" si se intenta resumir un UUID sin transcript previo).
    Si es True, agregamos --resume para restaurar el hilo previo.
    contra escrituras concurrentes.
    """
    try:
        return native_session_jsonl_path(cwd, session_id).is_file()
    except Exception:
        return False


def find_jsonl_transcripts(user_id: str) -> list[dict[str, Any]]:
    """
    Busca los archivos .jsonl que Claude Code generó en disco para este
    usuario y proyectos accesibles. Útil para /sessions con info de tamaño
    y última modificación.

    Path típico: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
    """
    results: list[dict[str, Any]] = []
    mapping = list_native_sessions(user_id)
    home = Path(os.path.expanduser("~"))
    projects_root = home / ".claude" / "projects"
    if not projects_root.exists():
        return results
    for cwd_str, sid in mapping.items():
        candidate = native_session_jsonl_path(Path(cwd_str), sid)
        info: dict[str, Any] = {
            "cwd": cwd_str,
            "session_id": sid,
            "jsonl_path": str(candidate),
            "exists": candidate.exists(),
        }
        if candidate.exists():
            try:
                stat = candidate.stat()
                info["size_bytes"] = stat.st_size
                info["mtime"] = stat.st_mtime
            except Exception:
                pass
        results.append(info)
    return results


# ============================================================================
# v1.8.0 — User modes (permission mode por usuario)
# ============================================================================
# Cada usuario tiene un permission_mode activo: default, plan, acceptEdits, bypassPermissions.
# Por defecto: bypassPermissions (= comportamiento v1.7.1, sin cambios para users existentes).
# Persistencia: state/user_modes.json
# {
#   "8166253211": {"mode": "plan", "updated_at": 1716950000},
#   ...
# }

VALID_PERMISSION_MODES = ("default", "plan", "acceptEdits", "bypassPermissions")

# Mapping para UI: modo -> (emoji, etiqueta corta, descripción)
MODE_UI = {
    "plan":              ("📋", "Plan",   "Solo analiza, no ejecuta"),
    "default":           ("✏️", "Edit",   "Pide approval por edit/bash"),
    "acceptEdits":       ("⚡", "Auto",   "Auto-aprueba edits, pide para Bash"),
    "bypassPermissions": ("🚀", "Bypass", "Sin prompts (autonomía total)"),
}

_user_modes_lock = threading.Lock()


def _load_user_modes() -> dict[str, dict[str, Any]]:
    """Lee el mapping user_id → {mode, updated_at}."""
    if not USER_MODES_FILE.exists():
        return {}
    try:
        with USER_MODES_FILE.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except Exception:
                pass
            try:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as exc:
        log(f"USER_MODES_LOAD_ERROR {type(exc).__name__}: {exc}")
        return {}


def _save_user_modes(data: dict[str, dict[str, Any]]) -> None:
    """Escribe atómico con flock."""
    tmp = USER_MODES_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        tmp.replace(USER_MODES_FILE)
    except Exception as exc:
        log(f"USER_MODES_SAVE_ERROR {type(exc).__name__}: {exc}")


def get_user_mode(user_id: str) -> str:
    """
    Retorna el permission_mode activo del usuario.
    Si no tiene uno seteado, usa CLAUDE_DEFAULT_PERMISSION_MODE.
    """
    with _user_modes_lock:
        data = _load_user_modes()
        entry = data.get(user_id, {})
        mode = entry.get("mode")
        if mode in VALID_PERMISSION_MODES:
            return mode
        return CLAUDE_DEFAULT_PERMISSION_MODE


def set_user_mode(user_id: str, mode: str) -> bool:
    """
    Setea el modo activo. Retorna True si el cambio se persistió.
    """
    if mode not in VALID_PERMISSION_MODES:
        log(f"SET_USER_MODE_INVALID user={user_id} mode={mode!r}")
        return False
    with _user_modes_lock:
        data = _load_user_modes()
        data[user_id] = {"mode": mode, "updated_at": int(time.time())}
        _save_user_modes(data)
        log(f"USER_MODE_SET user={user_id} mode={mode}")
        return True


# ============================================================================
# v1.8.0 — Last prompts (para workflow Plan → Approve → Execute)
# ============================================================================
# Cuando el usuario está en plan mode y manda un pedido, guardamos ese prompt
# en memoria por usuario. Si después toca [✅ Aprobar y ejecutar], usamos
# ese prompt para re-lanzar la tarea en bypassPermissions.

_last_prompts_lock = threading.Lock()


def _load_last_prompts() -> dict[str, dict[str, Any]]:
    if not LAST_PROMPT_FILE.exists():
        return {}
    try:
        with LAST_PROMPT_FILE.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except Exception:
                pass
            try:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as exc:
        log(f"LAST_PROMPTS_LOAD_ERROR {type(exc).__name__}: {exc}")
        return {}


def _save_last_prompts(data: dict[str, dict[str, Any]]) -> None:
    tmp = LAST_PROMPT_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        tmp.replace(LAST_PROMPT_FILE)
    except Exception as exc:
        log(f"LAST_PROMPTS_SAVE_ERROR {type(exc).__name__}: {exc}")


def save_last_prompt(user_id: str, prompt: str, chat_id: int) -> None:
    """Guarda el último prompt para que Plan→Approve pueda re-ejecutarlo."""
    with _last_prompts_lock:
        data = _load_last_prompts()
        # Limitar prompt guardado a 8000 chars (suficiente para reenvío)
        data[user_id] = {
            "prompt": prompt[:8000],
            "chat_id": chat_id,
            "saved_at": int(time.time()),
        }
        _save_last_prompts(data)


def get_last_prompt(user_id: str) -> dict[str, Any] | None:
    """Retorna el último prompt guardado, o None si no hay."""
    with _last_prompts_lock:
        data = _load_last_prompts()
        return data.get(user_id)


def clear_last_prompt(user_id: str) -> None:
    with _last_prompts_lock:
        data = _load_last_prompts()
        data.pop(user_id, None)
        _save_last_prompts(data)


# ============================================================================
# v1.8.0 — Inline keyboard builder
# ============================================================================
# Construye el teclado inline que se adjunta a las respuestas del bot.
# Telegram limita callback_data a 64 bytes; usamos formato compacto:
#   "mode:plan", "mode:edit", "mode:auto", "mode:bypass"
#   "op:stop", "op:reset", "op:status", "op:sessions"
#   "plan:approve", "plan:refine", "plan:discard"
#
# El builder usa visibilidad híbrida:
#   - 4 botones de modo: SIEMPRE (fila 1)
#   - 4 botones de operación: SOLO si hay tarea activa o si la sesión tiene
#     transcript (es decir, hay algo que parar/inspeccionar)

# Map corto callback_data → permission_mode largo
_MODE_KEY_TO_MODE = {
    "plan":   "plan",
    "edit":   "default",
    "auto":   "acceptEdits",
    "bypass": "bypassPermissions",
}
_MODE_TO_KEY = {v: k for k, v in _MODE_KEY_TO_MODE.items()}


def build_inline_keyboard(
    user_id: str,
    *,
    include_operations: bool = True,
    include_plan_actions: bool = False,
) -> dict[str, Any] | None:
    """
    Teclado inline SOBRIO. Máximo una fila, en español, sin cortar texto.

    Antes: 12 botones en 4 filas, en inglés, cortados ("Byp", "Sessio"),
    ocupando media pantalla del teléfono en CADA respuesta. Inutilizable.

    Ahora: los comandos viven en el menú nativo de Telegram (botón ☰, se
    registra con setMyCommands). Acá quedan solo los dos controles de uso
    frecuente, y el flujo de Plan cuando corresponde.
    """
    if not INLINE_KEYBOARD_ENABLED:
        return None

    rows: list[list[dict[str, str]]] = []

    # Flujo de Plan: lo único que justifica botones propios, porque es una
    # decisión inmediata sobre la respuesta que acabás de leer.
    if include_plan_actions:
        rows.append([
            {"text": "✅ Ejecutar plan", "callback_data": "plan:approve"},
            {"text": "❌ Descartar",     "callback_data": "plan:discard"},
        ])

    # Controles frecuentes: una sola fila, dos botones, en español.
    if include_operations:
        rows.append([
            {"text": "⏹ Detener", "callback_data": "op:stop"},
            {"text": "⚙️ Modo",    "callback_data": "op:mode"},
        ])

    return {"inline_keyboard": rows} if rows else None


def build_mode_keyboard(user_id: str) -> dict[str, Any]:
    """Submenú de modos, en español. Se abre con el botón ⚙️ Modo."""
    current = get_user_mode(user_id)
    row: list[dict[str, str]] = []
    for key in ("plan", "edit", "auto", "bypass"):
        full = _MODE_KEY_TO_MODE[key]
        emoji, label, _ = MODE_UI[full]
        marca = "✅ " if full == current else ""
        row.append({"text": f"{marca}{emoji} {label}", "callback_data": f"mode:{key}"})
    return {"inline_keyboard": [row[:2], row[2:]]}


def register_bot_commands() -> None:
    """
    Registra los comandos en el menú nativo de Telegram (botón ☰).
    Descripciones en español. Esto reemplaza la botonera que saturaba el chat.
    """
    comandos = [
        {"command": "status",     "description": "Estado de la tarea y la sesión"},
        {"command": "stop",       "description": "Detener la tarea en curso"},
        {"command": "newsession", "description": "Empezar una sesión limpia"},
        {"command": "sessions",   "description": "Ver sesiones por proyecto"},
        {"command": "context",    "description": "Tamaño del historial"},
        {"command": "compact",    "description": "Reducir historial pesado"},
        {"command": "cd",         "description": "Cambiar de proyecto"},
        {"command": "projects",   "description": "Listar proyectos"},
        {"command": "mode",       "description": "Cambiar modo de permisos"},
        {"command": "reset",      "description": "Liberar tarea trabada"},
        {"command": "help",       "description": "Ayuda"},
    ]
    try:
        telegram_api("setMyCommands", {"commands": comandos}, timeout=20)
        log(f"BOT_COMMANDS_REGISTERED n={len(comandos)}")
    except Exception as exc:
        log(f"BOT_COMMANDS_ERR {type(exc).__name__}: {exc}")


def should_include_operations_kb(user_id: str) -> bool:
    """
    Heurística para visibilidad híbrida de la fila operaciones:
    True si hay tarea activa O si la sesión tiene transcript >0.
    """
    with task_lock:
        task = running_tasks.get(user_id)
    if task is not None:
        return True
    # Si tiene transcript con turnos previos, los botones tienen sentido
    cwd = get_user_cwd(user_id)
    sid = get_native_session_id(user_id, cwd)
    return native_session_has_transcript(cwd, sid)


# ============================================================================
# Persistencia de sesiones
# ============================================================================

def load_sessions() -> dict[str, list[dict[str, str]]]:
    """v1.4 S1.6: load con fcntl.flock para consistencia con writes concurrentes."""
    if not SESSION_FILE.exists():
        return {}
    try:
        with SESSION_FILE.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # shared lock para lectura
            except Exception:
                pass
            data = json.load(f)
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            return data
    except Exception:
        return {}


def save_sessions(sessions: dict[str, list[dict[str, str]]]) -> None:
    """v1.4 S1.6: save atómico con flock + write-temp-rename."""
    tmp = SESSION_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # exclusive para escritura
            except Exception:
                pass
            json.dump(sessions, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        tmp.replace(SESSION_FILE)
    except Exception as exc:
        log(f"SAVE_SESSIONS_ERROR {type(exc).__name__}: {exc}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def append_session(user_id: str, role: str, content: str) -> None:
    """
    v1.6.1 BUG-7 fix: nunca propagar excepciones de I/O.
    Si falla, log y continuar — la falta de historial NO debe romper la tarea.
    """
    try:
        sessions = load_sessions()
        history = sessions.setdefault(user_id, [])
        # Sanitiza ANTES de persistir (Patch 2): cualquier tag falsificado queda
        # neutralizado para todas las queries futuras que usen este historial.
        safe_content = sanitize_for_context(content)[-8000:]
        history.append({"role": role, "content": safe_content})
        sessions[user_id] = history[-MAX_CONTEXT_MESSAGES:]
        save_sessions(sessions)
    except Exception as exc:
        log(f"APPEND_SESSION_ERROR user={user_id} {type(exc).__name__}: {exc}")


def clear_session(user_id: str) -> None:
    """v1.6.1 BUG-7 fix: catch I/O errors."""
    try:
        sessions = load_sessions()
        sessions.pop(user_id, None)
        save_sessions(sessions)
    except Exception as exc:
        log(f"CLEAR_SESSION_ERROR user={user_id} {type(exc).__name__}: {exc}")


# ============================================================================
# v1.4 S1.10: Persistencia de estadísticas
# ============================================================================

def _empty_stats() -> dict[str, Any]:
    return {
        "tasks_total": 0,
        "tasks_completed": 0,
        "tasks_cancelled": 0,
        "tasks_failed": 0,
        "tasks_blocked_destructive": 0,
        "total_runtime_seconds": 0.0,
        "total_output_chars": 0,
        "first_seen": time.time(),
    }


def load_stats() -> dict[str, Any]:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with STATS_FILE.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except Exception:
                pass
            data = json.load(f)
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        # Migrar si faltan campos
        defaults = _empty_stats()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _empty_stats()


def save_stats(stats: dict[str, Any]) -> None:
    tmp = STATS_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            json.dump(stats, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        tmp.replace(STATS_FILE)
    except Exception as exc:
        log(f"SAVE_STATS_ERROR {type(exc).__name__}: {exc}")


_stats_lock = threading.Lock()


def stats_increment(key: str, value: float = 1) -> None:
    """Thread-safe stats increment."""
    with _stats_lock:
        stats = load_stats()
        stats[key] = stats.get(key, 0) + value
        save_stats(stats)


# ============================================================================
# v1.4 S1.4: Recuperación de PIDs huérfanos
# ============================================================================

_running_pid_lock = threading.Lock()


def save_running_pid(user_id: str, pid: int, started_at: float) -> None:
    """
    Persistir PID activo en disco para recuperación post-crash.
    v1.6.1 BUG-9 fix: lock thread-safe + fcntl.flock para evitar race.
    """
    with _running_pid_lock:
        try:
            existing: dict[str, Any] = {}
            if RUNNING_FILE.exists():
                with RUNNING_FILE.open("r", encoding="utf-8") as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except Exception:
                        pass
                    try:
                        existing = json.load(f)
                    except Exception:
                        existing = {}
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
            existing[user_id] = {"pid": pid, "started_at": started_at}
            tmp = RUNNING_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                json.dump(existing, f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            tmp.replace(RUNNING_FILE)
        except Exception as exc:
            log(f"SAVE_RUNNING_ERROR {exc}")


def clear_running_pid(user_id: str) -> None:
    """v1.6.1 BUG-9 fix: lock + flock."""
    with _running_pid_lock:
        try:
            if not RUNNING_FILE.exists():
                return
            with RUNNING_FILE.open("r", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                except Exception:
                    pass
                try:
                    data = json.load(f)
                except Exception:
                    data = {}
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            data.pop(user_id, None)
            tmp = RUNNING_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                json.dump(data, f)
                f.flush()
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            tmp.replace(RUNNING_FILE)
        except Exception as exc:
            log(f"CLEAR_RUNNING_ERROR {exc}")


def cleanup_orphan_pids() -> None:
    """
    v1.4 S1.4: Al arrancar, matar procesos huérfanos de runs previas.
    Si el bot crasheó/reinició mientras un claude estaba corriendo, ese proceso
    queda consumiendo VRAM. Esta función lo detecta y lo mata.
    """
    if not RUNNING_FILE.exists():
        return
    try:
        with RUNNING_FILE.open("r", encoding="utf-8") as f:
            orphans = json.load(f)
    except Exception:
        return

    killed = 0
    for user_id, info in orphans.items():
        pid = info.get("pid")
        if not pid:
            continue
        try:
            # /proc/<pid>/comm debe contener "claude" o "node" para que sea seguro matar
            comm_path = Path(f"/proc/{pid}/comm")
            if not comm_path.exists():
                continue
            comm = comm_path.read_text().strip().lower()
            if not any(c in comm for c in ("claude", "node")):
                log(f"ORPHAN_SKIP pid={pid} comm={comm} (no es claude)")
                continue
            # Verificar UID coincide con el nuestro (no matar de otro usuario)
            stat_path = Path(f"/proc/{pid}/status")
            if stat_path.exists():
                uid_line = next(
                    (l for l in stat_path.read_text().splitlines() if l.startswith("Uid:")),
                    None,
                )
                if uid_line:
                    real_uid = int(uid_line.split()[1])
                    if real_uid != os.getuid():
                        log(f"ORPHAN_SKIP pid={pid} uid={real_uid} (no es nuestro UID)")
                        continue
            # Matar el grupo si es posible
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
            killed += 1
            log(f"ORPHAN_KILLED user={user_id} pid={pid} comm={comm}")
        except ProcessLookupError:
            pass  # ya no existe
        except Exception as exc:
            log(f"ORPHAN_KILL_ERROR pid={pid} {exc}")

    # Limpiar el archivo después del barrido
    try:
        RUNNING_FILE.unlink()
    except Exception:
        pass

    if killed:
        log(f"ORPHAN_CLEANUP killed={killed}")


# ============================================================================
# v1.4 S1.1: CLAUDE.md auto-generado
# ============================================================================

CLAUDE_MD_CONTENT = """# Cloky Agent Policy

## Rol
Agente Claude Code conectado a un LLM local vía llama.cpp/TurboQuant.
Operás dentro del workspace asignado.

## Reglas duras
- No tocar el backend LLM (llama.cpp, TurboQuant, vLLM, qwen-server). El servidor es read-only.
- No tocar otros bots, agentes ni proyectos del sistema (Agent Zero, Ductor, Aiolos, Hermes, OpenClaw, VIPER, Taurus, BoviSense, CobraVivo).
- No salir del workspace asignado salvo instrucción explícita del usuario.
- Antes de modificar un archivo, leerlo. Antes de borrar, listar.
- No repetir el mismo comando si ya falló dos veces. Cambiá de estrategia.
- Si una tool devuelve error, reportar el error real. No inventar éxito.

## Modo de trabajo
- Tareas largas: dividir en fases pequeñas, reportar progreso por fase.
- Código: cambios mínimos, reversibles. Backup antes de modificar.
- Auditoría: leer → diagnosticar → proponer fix → aplicar solo si se aprueba.

## Lenguaje
Español técnico. Sin emojis. Sin caveats excesivos.
"""


def ensure_claude_md() -> None:
    """v1.4 S1.1: Crear CLAUDE.md en el workspace si no existe."""
    md_path = WORKSPACE_DIR / "CLAUDE.md"
    if md_path.exists():
        return
    try:
        md_path.write_text(CLAUDE_MD_CONTENT, encoding="utf-8")
        log(f"CLAUDE_MD created at {md_path}")
    except Exception as exc:
        log(f"CLAUDE_MD_ERROR {exc}")


# ============================================================================
# Construcción de prompt con separadores no falsificables (Patch 2)
# ============================================================================

def build_prompt(user_id: str, user_text: str) -> str:
    """
    v1.7.0 RADICAL: Ya NO se reconstruye el contexto manualmente.

    Claude Code restaura el transcript completo nativamente vía --resume con
    el session_id estable. Lo único que enviamos es el mensaje actual del
    usuario, sanitizado contra prompt injection.

    Esto elimina los problemas v1.6.1:
      - Mensajes truncados a 8000 chars
      - Solo 12 turnos visibles
      - Tool calls y archivos leídos perdidos entre turnos
    """
    safe_user_text = sanitize_for_context(user_text)

    # System hint mínimo. Claude Code ya tiene CLAUDE.md en el workspace
    # con la política operacional completa. No saturamos el prompt.
    # Solo incluimos system al primer turno (cuando el transcript está vacío),
    # pero como no sabemos si es primer turno sin tocar disco extra, lo
    # enviamos siempre — Claude Code lo recibe pero al ser --resume con
    # transcript existente, le da peso bajo (es preámbulo del user msg).
    #
    # Importante: NO ponemos un separador random complejo. El user_text
    # sanitizado por sanitize_for_context ya neutralizó cualquier tag
    # [system]/[user]/[assistant] que el atacante pudiera inyectar.

    return safe_user_text


# ============================================================================
# Estado de tareas en ejecución
# ============================================================================

@dataclass
class RunningTask:
    user_id: str
    chat_id: int
    process: subprocess.Popen[str]
    started_at: float = field(default_factory=time.time)
    last_progress_at: float = field(default_factory=time.time)
    last_status: str = "Iniciando Claude Code..."


# Slot puede contener: RunningTask | "PENDING" | None
running_tasks: dict[str, Any] = {}
task_lock = threading.Lock()


# ============================================================================
# Entorno para Claude Code CLI
# ============================================================================

def build_claude_env() -> dict[str, str]:
    """
    v1.3: Construye el environment para el subprocess Claude Code.
    Propaga todas las variables de optimización documentadas oficialmente.
    """
    env = os.environ.copy()
    env.update(
        {
            # --- Backend Anthropic-compatible ---
            "ANTHROPIC_BASE_URL": QWEN_BASE_URL,
            "ANTHROPIC_API_KEY": QWEN_API_KEY,
            "ANTHROPIC_AUTH_TOKEN": QWEN_API_KEY,
            "ANTHROPIC_MODEL": CLAUDE_MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL_ID,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL_ID,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL_ID,

            # --- Tokens (defaults oficiales documentados 2026-05-10) ---
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": CLAUDE_MAX_OUTPUT_TOKENS,
            "MAX_THINKING_TOKENS": MAX_THINKING_TOKENS,
            "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": CLAUDE_TOOL_CONCURRENCY,

            # --- Bash timeouts (críticos para tareas largas) ---
            "BASH_DEFAULT_TIMEOUT_MS": BASH_DEFAULT_TIMEOUT_MS,
            "BASH_MAX_TIMEOUT_MS": BASH_MAX_TIMEOUT_MS,
            "BASH_MAX_OUTPUT_LENGTH": BASH_MAX_OUTPUT_LENGTH,

            # --- MCP timeouts ---
            "MCP_TIMEOUT": MCP_TIMEOUT,
            "MCP_TOOL_TIMEOUT": MCP_TOOL_TIMEOUT,
            "MAX_MCP_OUTPUT_TOKENS": MAX_MCP_OUTPUT_TOKENS,

            # --- Telemetría apagada ---
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "DISABLE_AUTOUPDATER": "1",

            # --- Tool search habilitada ---
            "ENABLE_TOOL_SEARCH": "true",

            # --- No proxy local ---
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
    )
    # Eliminar proxies que rompen comunicación con backend local.
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        env.pop(key, None)
    return env


# ============================================================================
# Parser de stream-json de Claude Code CLI
# ============================================================================

def parse_stream_line(line: str) -> tuple[str | None, str | None]:
    """
    Extrae (texto_visible, nuevo_estado) de una línea stream-json.

    Extracción TIPADA: solo lee los campos documentados del protocolo, en vez
    de caminar recursivamente cualquier campo desconocido. Los formatos
    internos cambian entre versiones de Claude Code; recorrerlos a ciegas
    hacía que apareciera metadata en el chat.

    Campos leídos, y ninguno más:
      • assistant.message.content[].text
      • message.content[].text
      • stream_event.event.delta.text   (streaming parcial)
      • result.result
      • error.message
    """
    line = (line or "").strip()
    if not line or not line.startswith("{"):
        return None, None
    try:
        obj = json.loads(line)
    except Exception:
        return None, None
    if not isinstance(obj, dict):
        return None, None

    etype = str(obj.get("type", ""))

    # Metadata: nunca produce texto visible
    if etype in {"system", "init"}:
        return None, None

    def clean(s: Any) -> str | None:
        if not isinstance(s, str) or not s.strip():
            return None
        v = s.strip()
        if is_sentinel_output(v) or is_internal_string(v) or is_output_garbage(v):
            return None
        return sanitize_output(v)

    def blocks_text(content: Any) -> list[str]:
        """Solo bloques {type:'text', text:'...'}. Ignora tool_use/tool_result."""
        out: list[str] = []
        if isinstance(content, str):
            c = clean(content)
            if c:
                out.append(c)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    c = clean(b.get("text"))
                    if c:
                        out.append(c)
        return out

    # 1) Streaming parcial (--include-partial-messages)
    if etype == "stream_event":
        ev = obj.get("event")
        if isinstance(ev, dict):
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                c = clean(delta.get("text"))
                if c:
                    return c, None
        return None, None

    # 2) Resultado final
    if etype == "result":
        c = clean(obj.get("result"))
        if c:
            return c, None
        if obj.get("subtype") == "error":
            err = obj.get("error") or obj.get("message") or "tarea falló"
            if isinstance(err, (dict, list)):
                err = str(err)[:300]
            return f"ERROR: {sanitize_output(str(err))}", "Error"
        return None, None

    # 3) Error
    if etype == "error":
        err = obj.get("message") or obj.get("error") or "error desconocido"
        if isinstance(err, dict):
            err = err.get("message", str(err)[:200])
        return f"ERROR: {sanitize_output(str(err))}", "Error"

    # 4) Mensajes del asistente
    msg = obj.get("message")
    if isinstance(msg, dict):
        parts = blocks_text(msg.get("content"))
        # Estado según la herramienta en uso (sin exponer su contenido)
        status = None
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = str(b.get("name", ""))
                    if name and not name.startswith("mcp__"):
                        status = f"Usando {name}"
                    break
        if parts:
            return "\n".join(parts), status
        return None, status

    return None, None



def extract_usage(line: str) -> tuple[int, int]:
    """
    Tokens de una línea del stream. Soporta ambos formatos:
      • Anthropic: input_tokens / output_tokens
      • OpenAI:    prompt_tokens / completion_tokens   ← llama-server
    Son SNAPSHOTS acumulativos: el llamador debe quedarse con el mayor, no sumar.
    """
    try:
        obj = json.loads(line)
    except Exception:
        return 0, 0
    if not isinstance(obj, dict):
        return 0, 0

    def read(u: Any) -> tuple[int, int]:
        if not isinstance(u, dict):
            return 0, 0
        try:
            return (
                int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0),
                int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0),
            )
        except (TypeError, ValueError):
            return 0, 0

    i, o = read(obj.get("usage"))
    if i or o:
        return i, o
    msg = obj.get("message")
    if isinstance(msg, dict):
        i, o = read(msg.get("usage"))
        if i or o:
            return i, o
    return 0, 0


BIG_LINE_BYTES = int(os.environ.get("TRANSCRIPT_BIG_LINE_BYTES", str(50 * 1024)))
WARN_BYTES = int(os.environ.get("TRANSCRIPT_WARN_BYTES", str(5 * 1024 * 1024)))


def fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def analyze_transcript(path: Path) -> dict[str, Any]:
    """Solo LEE el historial para informar tamaño. Nunca lo modifica."""
    info = {"exists": False, "total_bytes": 0, "total_lines": 0, "big_lines": 0}
    if not path.is_file():
        return info
    info["exists"] = True
    try:
        info["total_bytes"] = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                info["total_lines"] += 1
                if len(raw.encode("utf-8", "replace")) > BIG_LINE_BYTES:
                    info["big_lines"] += 1
    except Exception as exc:
        log(f"ANALYZE_TRANSCRIPT_ERR {exc}")
    return info


def fallback_from_outputs(stdout_text: str, stderr_text: str, return_code: int | None) -> str:
    """
    Se usa cuando el parser no extrajo texto visible.
    
    Un solo pase: extrae SOLO campos documentados (result, summary, message,
    content, text) de objetos JSON en stdout. Sin walker recursivo.
    Si no hay texto legible, informa en vez de volcar basura.
    """
    METADATA_TYPES = {"system", "init", "tool_use", "tool_result"}
    
    def usable(s: str) -> bool:
        return bool(s and s.strip()) and not is_internal_string(s) and not is_output_garbage(s)
    
    found: list[str] = []
    for raw in stdout_text.splitlines():
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") in METADATA_TYPES:
            continue
        # Campos documentados únicamente
        for key in ("result", "summary", "message", "content", "text"):
            v = obj.get(key)
            if isinstance(v, str) and usable(v):
                found.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_val = item.get("text")
                        if isinstance(text_val, str) and usable(text_val):
                            found.append(text_val.strip())
                    elif isinstance(item, str) and usable(item):
                        found.append(item.strip())
    
    if found:
        joined = "\n\n".join(found)[-12000:]
        if not is_output_garbage(joined):
            return redact(sanitize_output(joined))
    
    # Sin texto legible — informar, NO volcar
    out = ["Claude Code finalizó sin mensaje visible."]
    if return_code is None:
        out.append("Estado: terminado sin código de salida (timeout o interrupción).")
    elif return_code != 0:
        out.append(f"Estado: terminado con código de error {return_code}.")
    err = stderr_text.strip()
    if err and not err.lstrip().startswith(("{{", "[")) and not is_output_garbage(err):
        out.append("stderr:\n" + redact(sanitize_output(err[-2000:])))
    return "\n\n".join(out)


    def usable(s: str) -> bool:
        return bool(s and s.strip()) and not is_internal_string(s) and not is_output_garbage(s)

    # Pase 1: campos de texto conocidos
    found: list[str] = []
    for raw in stdout_text.splitlines():
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") in METADATA_TYPES:
            continue
        for key in ("result", "summary", "message", "content", "text"):
            v = obj.get(key)
            if isinstance(v, str) and usable(v):
                found.append(v.strip())
    if found:
        joined = "\n\n".join(found)[-12000:]
        if not is_output_garbage(joined):
            return redact(sanitize_output(joined))

    # Pase 2: solo campos tipados conocidos (sin walker recursivo)
    # FIX: el walker anterior caminaba campos arbitrarios del JSON, así que
    # cualquier campo desconocido podía terminar en el chat. Ahora solo
    # lee rutas tipadas: result, summary, message, content, text.
    found: list[str] = []
    for raw in stdout_text.splitlines():
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("result", "summary", "message", "content", "text"):
            v = obj.get(key)
            if isinstance(v, str) and usable(v):
                found.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_val = item.get("text")
                        if isinstance(text_val, str) and usable(text_val):
                            found.append(text_val.strip())
                    elif isinstance(item, str) and usable(item):
                        found.append(item.strip())

    # Pase 3: sin texto legible — informar, NO volcar
    out = ["Claude Code finalizó sin mensaje visible."]
    if return_code is None:
        out.append("Estado: terminado sin código de salida (timeout o interrupción).")
    elif return_code != 0:
        out.append(f"Estado: terminado con código de error {return_code}.")
    err = stderr_text.strip()
    if err and not err.lstrip().startswith(("{", "[")) and not is_output_garbage(err):
        out.append("stderr:\n" + redact(sanitize_output(err[-2000:])))
    return "\n\n".join(out)


# ============================================================================
# Allowlist
# ============================================================================

def is_allowed(user_id: int) -> bool:
    allowed = {x.strip() for x in ALLOWED_TELEGRAM_USER_ID.split(",") if x.strip()}
    return str(user_id) in allowed


# ============================================================================
# Comandos Telegram
# ============================================================================


def run_claude_task(user_id: str, chat_id: int, message_id: int, user_text: str) -> None:
    """
    v1.6.1 CRITICAL BUG-1 fix: wrapper externo con try/finally garantizado.

    Antes (v1.6): si build_prompt, append_session o stats_increment crasheaban
    ANTES del try interno (línea ~1280), el slot 'PENDING' reservado por
    handle_update quedaba huérfano para siempre, hasta restart del servicio.
    Era el bug del "Inicializando 854s" que reportabas.

    Ahora: cualquier crash temprano cae en el finally externo que SIEMPRE
    libera el slot. La función interna conserva su propio try/except/finally
    para los crashes durante la ejecución real.
    """
    try:
        _run_claude_task_inner(user_id, chat_id, message_id, user_text)
    except Exception as exc:
        # Capturar cualquier excepción que se haya escapado del inner.
        # No debería pasar, pero defensa en profundidad.
        log(f"RUN_CLAUDE_TASK_OUTER_CRASH user={user_id} {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        try:
            send_message(
                chat_id,
                redact(f"❌ Error temprano del ejecutor:\n{type(exc).__name__}: {exc}"),
                reply_to_message_id=message_id,
            )
        except Exception:
            pass
    finally:
        # SIEMPRE liberar el slot, sin importar qué pasó arriba.
        # Esto incluye el caso donde inner crasheó antes de su propio finally
        # (por ejemplo, si build_prompt levantó una excepción no manejada).
        with task_lock:
            running_tasks.pop(user_id, None)
        try:
            clear_running_pid(user_id)
        except Exception:
            pass


def _run_claude_task_inner(user_id: str, chat_id: int, message_id: int, user_text: str) -> None:
    """v1.6: ejecutor con streaming incremental + cwd dinámico por usuario."""
    prompt = build_prompt(user_id, user_text)
    append_session(user_id, "user", user_text)
    stats_increment("tasks_total")

    # v1.6 S2.3: cwd dinámico — el usuario puede haber hecho /cd <proyecto>
    cwd = get_user_cwd(user_id)
    if not cwd.exists():
        cwd = WORKSPACE_DIR  # fallback si el path se borró

    # v1.7.0: persistencia nativa Claude Code
    # ─────────────────────────────────────────────────────────────────────────
    # Obtener (o crear) el session_id estable para este (user, cwd).
    # Claude Code lo usará para:
    #   1. Persistir el transcript en ~/.claude/projects/<encoded>/<UUID>.jsonl
    #   2. Restaurar turnos previos cuando se invoque con --resume <UUID>
    # Esto resuelve el problema "el bot pierde noción de los archivos que le
    # mandé" — ahora Claude restaura el contexto completo cada vez.
    session_id = get_native_session_id(user_id, cwd)

    # v1.7.1 FIX CRÍTICO: detectar primer turno vs continuación
    # ─────────────────────────────────────────────────────────────────────────
    # Claude Code 2.1.150 falla con "Could not find session" si pasamos
    # --resume <UUID> cuando el transcript .jsonl todavía no existe en disco.
    # El patrón correcto descubierto empíricamente es:
    #
    #   Turno 1 (no hay .jsonl):
    #       claude --session-id <UUID> ...                    [SIN --resume]
    #     → Claude crea ~/.claude/projects/<enc>/<UUID>.jsonl
    #
    #   Turno 2+ (hay .jsonl):
    #       claude --session-id <UUID> --resume <UUID> ...
    #     → Claude restaura el transcript y agrega los turnos nuevos
    #     → sin --fork-session: el fork rota el ID y rompe la continuidad
    #
    # En v1.7.0 mandábamos --resume siempre, lo cual rompía el primer mensaje
    # de cada sesión nueva. Este fix decide flag-by-flag según el estado.
    is_continuation = native_session_has_transcript(cwd, session_id)
    turn_label = "RESUME" if is_continuation else "FIRST_TURN"

    # v1.8.0: leer modo del USUARIO en lugar de constante global.
    # v2.2.1: no usar FORCE_PERMISSION_MODE (v2.2.1 no lo implementa).
    user_permission_mode = get_user_mode(user_id)
    if not user_permission_mode:
        # Default a bypassPermissions si nunca se seteó
        user_permission_mode = CLAUDE_DEFAULT_PERMISSION_MODE
        set_user_mode(user_id, user_permission_mode)

    # Construir command base.
    #
    # --session-id y --resume son MUTUAMENTE EXCLUYENTES. Claude Code rechaza
    # la combinación con:
    #   "--session-id can only be used with --continue or --resume if
    #    --fork-session is also specified."
    # Y --fork-session no sirve acá porque rota el ID en cada turno.
    #
    #   Turno 1  (no hay .jsonl): --session-id <UUID>   → Claude crea la sesión
    #   Turno 2+ (hay .jsonl):    --resume <UUID>       → Claude la continúa
    command = [CLAUDE_BIN, "--permission-mode", user_permission_mode]
    if is_continuation:
        command.extend(["--resume", session_id])
    else:
        command.extend(["--session-id", session_id])
    command.extend([
        "--max-turns", str(CLAUDE_MAX_TURNS),
        "--model", CLAUDE_MODEL,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ])
    # El prompt va por STDIN en vez de argv: es el patrón de los bridges que
    # funcionan. NO está demostrado que argv fuera la causa del silencio.

    log(
        f"START_TASK user={user_id} cwd={cwd} "
        f"session_id={session_id[:8]}... mode={user_permission_mode} "
        f"max_turns={CLAUDE_MAX_TURNS} {turn_label}"
    )

    # v1.8.0: guardar el último prompt del usuario para workflow Plan→Approve
    if user_permission_mode == "plan":
        save_last_prompt(user_id, user_text, chat_id)

    env = build_claude_env()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    # Streaming: separar delta parcial, texto de asistente, resultado final
    partial_text: list[str] = []
    assistant_text: list[str] = []
    result_text: list[str] = []
    sentinel_detected: bool = False
    status = "Procesando con Claude Code..."
    process: subprocess.Popen[str] | None = None
    started = time.time()
    return_code: int | None = None
    task_outcome = "failed"

    # Tokens del turno (formato Anthropic u OpenAI)
    tokens_in = 0
    tokens_out = 0
    last_usage: tuple[int, int] = (0, 0)

    # Indicador "escribiendo..." continuo. Se detiene SIEMPRE en el finally.
    typing = TypingKeepalive(chat_id, "typing").start()

    # Aviso si el transcript está pesado (causa medida de --resume lento)
    try:
        _info = analyze_transcript(native_session_jsonl_path(cwd, session_id))
        if _info["exists"] and _info["total_bytes"] > WARN_BYTES:
            status = f"Procesando con contexto previo ({fmt_bytes(_info['total_bytes'])})..."
            send_message(
                chat_id,
                f"⚠️ Esta sesión tiene {fmt_bytes(_info['total_bytes'])} de historial. "
                f"El --resume va a tardar. Usá /compact para acelerar o /newsession para empezar limpio.",
                notify=False,
            )
    except Exception:
        pass

    # v1.6 S2.1: streaming a Telegram
    streamer = StreamingMessage(chat_id=chat_id, reply_to=message_id) if STREAM_ENABLED else None
    streamed_chars = 0

    def thread_safe_reader(stream: Any, label: str, q: queue.Queue) -> None:
        try:
            for raw in iter(stream.readline, ""):
                q.put((label, raw.rstrip("\n")))
        except Exception as exc:
            log(f"READER_THREAD_ERROR label={label} {type(exc).__name__}: {exc}")
        finally:
            q.put((label, "__EOF__"))

    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdin=subprocess.PIPE,   # FIX: stdin legible + canal del prompt
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            # v1.6.1 IMP-2: start_new_session es thread-safe y preferido sobre
            # preexec_fn (que tiene warnings en Py3.8+). Equivale a os.setsid().
            start_new_session=True,
        )

        # Enviar el prompt por stdin y cerrar: Claude Code lo lee como input
        # y el EOF le indica que no viene nada más.
        try:
            if process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()
        except Exception as exc:
            log(f"STDIN_WRITE_ERR user={user_id} {type(exc).__name__}: {exc}")

        save_running_pid(user_id, process.pid, started)

        with task_lock:
            running_tasks[user_id] = RunningTask(
                user_id=user_id, chat_id=chat_id, process=process, started_at=started
            )

        q: queue.Queue[tuple[str, str]] = queue.Queue()
        threading.Thread(
            target=thread_safe_reader, args=(process.stdout, "stdout", q), daemon=True
        ).start()
        threading.Thread(
            target=thread_safe_reader, args=(process.stderr, "stderr", q), daemon=True
        ).start()

        eof_count = 0
        last_progress = time.time()

        while True:
            if time.time() - started > CLAUDE_TIMEOUT_SECONDS:
                raise TimeoutError(f"Claude Code timeout after {CLAUDE_TIMEOUT_SECONDS}s")

            if process.poll() is not None and eof_count >= 2:
                break

            try:
                label, line = q.get(timeout=1)
            except queue.Empty:
                if time.time() - last_progress >= PROGRESS_INTERVAL:
                    send_chat_action(chat_id)
                    last_progress = time.time()
                continue

            if line == "__EOF__":
                eof_count += 1
                continue

            if label == "stdout":
                 stdout_lines.append(line)
                 try:
                     _i, _o = extract_usage(line)
                     if _i or _o:
                         # Snapshots acumulativos: el mayor gana, NO se suman.
                         tokens_in = max(tokens_in, _i)
                         tokens_out = max(tokens_out, _o)
                 except Exception:
                     pass
                 etype = None
                 visible, new_status = parse_stream_line(line)
                 # Extraer etype para clasificación posterior
                 if line.startswith("{"):
                     try:
                         obj = json.loads(line)
                         etype = str(obj.get("type", ""))
                     except Exception:
                         pass
                 if new_status:
                     status = new_status
                     with task_lock:
                         task = running_tasks.get(user_id)
                         if isinstance(task, RunningTask):
                             task.last_status = new_status
                             task.last_progress_at = time.time()
                 if visible:
                     # Classify into partial, assistant, or result buckets
                     # to avoid mixing streaming deltas with final output
                     if new_status == "Error":
                         assistant_text.append(visible)
                     elif etype == "result":
                         result_text.append(visible)
                     else:
                         partial_text.append(visible)
                     # v1.6 S2.1: streaming incremental a Telegram
                     if streamer is not None:
                         try:
                             # Solo arrancar a editar cuando tenemos contenido mínimo
                             buf_so_far = "\\n\\n".join(partial_text)
                             if len(buf_so_far) >= STREAM_MIN_CHUNK_LEN:
                                 # Append solo el chunk nuevo
                                 streamer.append(visible + "\\n\\n")
                                 streamed_chars = len(buf_so_far)
                         except Exception as exc:
                             log(f"STREAM_ERROR {type(exc).__name__}: {exc}")
            else:
                stderr_lines.append(line)
                if "error" in line.lower() or "timeout" in line.lower():
                    status = "Claude Code reportó evento en stderr"

            if time.time() - last_progress >= PROGRESS_INTERVAL:
                send_chat_action(chat_id)
                last_progress = time.time()

        # v1.4 S1.7: wait con TimeoutExpired manejado
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log(f"WAIT_TIMEOUT user={user_id} pid={process.pid} forzando kill")
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass
            try:
                return_code = process.wait(timeout=2)
            except Exception:
                return_code = -9

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines)

        # === FIX: Streaming dedup separado + composición final limpia ===
        # Dedup parcial por hash sha256 (solo dentro de cada bucket)
        def dedup(parts: list[str]) -> list[str]:
            seen: set[str] = set()
            unique: list[str] = []
            for part in parts:
                normalized = part.strip()
                if not normalized:
                    continue
                h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                unique.append(normalized)
            return unique

        partial_text = dedup(partial_text)
        assistant_text = dedup(assistant_text)
        result_text = dedup(result_text)

        # Composición final: result > assistant (solo campos documentados).
        # partial_text se usa SOLO como último recurso si no hubo streaming
        # ni result/assistant — evita mezclar deltas parciales con mensaje final.
        final_text = result_text[-1] if result_text else None
        if not final_text and assistant_text:
            final_text = "\n\n".join(assistant_text).strip()
        elif not result_text and not assistant_text and partial_text:
            # Solo si no hubo result ni assistant: partial_text como último recurso
            final_text = "\n\n".join(partial_text).strip()

        # === FIX: Sentinel "No response requested." ===
        if is_sentinel_output(final_text or ""):
            sentinel_detected = True
            final_text = "⚠️ La sesión no produjo respuesta visible.\n\nProbá mandando un prompt más específico o usá /newsession para empezar de cero."

        if not final_text:
            final_text = fallback_from_outputs(stdout_text, stderr_text, return_code)

        if return_code != 0 and return_code is not None:
            final_text = f"Claude Code terminó con código {return_code}.\n\n{final_text}"

        # Redacción defensiva final antes de Telegram.
        final_text = redact(final_text)

        append_session(user_id, "assistant", final_text)

        # v1.4 S1.3: notificación visible si la tarea fue larga
        elapsed = time.time() - started
        notify = elapsed > 120.0  # 2 min: vale la pena hacer notificar con sonido

        # Header con métricas operativas para tareas largas
        if elapsed > 30.0:
            hdr = f"✓ Completado en {_format_duration(elapsed)}"
            if tokens_out > 0:
                hdr += f" · ↓ {tokens_out} tokens"
            final_text = hdr + "\n\n" + final_text

        # v1.8.0: construir teclado inline para la respuesta final.
        # - Modos: siempre (fila superior)
        # - Operación: si hay tarea activa o sesión con transcript
        # - Plan workflow: si el modo actual del usuario es "plan"
        try:
            kb = build_inline_keyboard(
                user_id,
                include_operations=should_include_operations_kb(user_id),
                include_plan_actions=(user_permission_mode == "plan"),
            )
        except Exception as exc:
            log(f"BUILD_KEYBOARD_ERROR {type(exc).__name__}: {exc}")
            kb = None

        # v1.6 S2.1: si hubo streaming, finalizar el último mensaje editándolo
        # y enviar el resto como mensajes adicionales si excede.
        if streamer is not None and streamer.message_ids:
            chunks = split_text(final_text)
            # Editar el último mensaje del stream con el primer chunk (que ya tiene header)
            streamer.finalize(chunks[0])
            # Mandar mensajes adicionales con el resto
            for idx, extra in enumerate(chunks[1:]):
                # El reply_markup va solo en el último chunk extra
                is_last_extra = (idx == len(chunks) - 2)
                send_message(chat_id, extra, notify=False, reply_markup=kb if is_last_extra else None)
            # Si no hubo chunks extra, agregamos un mensaje vacío con el teclado al final
            # (no podemos editar el último mensaje del stream para agregarle markup
            # sin reescribir todo, así que mandamos un teclado en mensaje aparte solo
            # si es necesario y no notificamos)
            if len(chunks) == 1 and kb is not None:
                # Editar el último mensaje del stream para agregarle reply_markup
                try:
                    edit_message(
                        chat_id,
                        streamer.message_ids[-1],
                        chunks[0],
                        reply_markup=kb,
                    )
                except Exception:
                    pass
            # Mensaje de notificación final con sonido si la tarea fue larga
            if notify:
                send_message(chat_id, f"🔔 Tarea completada ({_format_duration(elapsed)})", notify=True)
        else:
            send_message(
                chat_id, final_text, reply_to_message_id=message_id,
                notify=notify, reply_markup=kb,
            )

        log(f"END_TASK user={user_id} return_code={return_code} content_len={len(final_text)} elapsed={elapsed:.1f}s")

        # Stats
        task_outcome = "completed" if return_code == 0 else "failed"
        stats_increment(f"tasks_{task_outcome}")
        stats_increment("total_runtime_seconds", elapsed)
        stats_increment("total_output_chars", len(final_text))

    except Exception as exc:
        elapsed = time.time() - started
        is_cancelled = isinstance(exc, _CancelledError)
        if is_cancelled:
            error_text = f"⏹ Tarea cancelada tras {_format_duration(elapsed)}."
            task_outcome = "cancelled"
            stats_increment("tasks_cancelled")
        else:
            error_text = f"❌ Error ejecutando Claude Code:\n{type(exc).__name__}: {exc}"
            stats_increment("tasks_failed")
        log(redact(error_text + "\n" + traceback.format_exc()))
        try:
            send_message(chat_id, redact(error_text), reply_to_message_id=message_id)
        except Exception:
            pass

    finally:
        # Detener "escribiendo..." SIEMPRE y primero (evita typing infinito)
        try:
            typing.stop()
        except Exception:
            pass

        # v1.4 S1.7: asegurar que el proceso esté realmente muerto
        if process is not None and process.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    time.sleep(0.5)
                    if process.poll() is None:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass

        # Patch 3: limpia tanto RunningTask como slot PENDING.
        with task_lock:
            running_tasks.pop(user_id, None)
        clear_running_pid(user_id)


class _CancelledError(Exception):
    """v1.4 S1.2: marker para distinguir cancelación de error."""
    pass


def _format_duration(seconds: float) -> str:
    """Formato amigable: 5s, 1m 30s, 1h 5m."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def handle_command(user_id: str, chat_id: int, message_id: int, text: str) -> bool:
    """
    v1.6.1 BUG-4 fix: wrapper externo con try/except global.
    Si CUALQUIER comando crashea, se loguea y se notifica al usuario,
    pero el main loop NO se rompe.
    """
    try:
        return _handle_command_inner(user_id, chat_id, message_id, text)
    except Exception as exc:
        log(f"HANDLE_COMMAND_CRASH user={user_id} text={text[:80]!r} {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        try:
            send_message(
                chat_id,
                redact(f"❌ Error en comando:\n{type(exc).__name__}: {exc}"),
                reply_to_message_id=message_id,
            )
        except Exception:
            pass
        # Comando "tratado" para que no caiga al fallback de tarea
        return True


def _handle_command_inner(user_id: str, chat_id: int, message_id: int, text: str) -> bool:
    command = text.strip().split()[0].lower()

    if command in {"/start", "/help"}:
        if SANDBOX_MODE == "open":
            sandbox_msg = "⚠ Sandbox: OPEN (acceso a todo $HOME)"
        elif SANDBOX_MODE == "strict":
            sandbox_msg = "Sandbox: STRICT (solo workspace y ~/.claude)"
        else:
            sandbox_msg = f"Sandbox: {SANDBOX_MODE}"
        active_mode = get_user_mode(user_id)
        emoji_now, label_now, _ = MODE_UI[active_mode]
        kb = build_inline_keyboard(
            user_id,
            include_operations=should_include_operations_kb(user_id),
            include_plan_actions=(active_mode == "plan"),
        )
        send_message(
            chat_id,
            (
                f"Bot Claude Code v{VERSION}\n"
                f"Modo: {emoji_now} {label_now}\n"
                f"{sandbox_msg}\n\n"
                "Permission Modes (v1.8 NUEVO):\n"
                "/plan   📋 Solo analiza, no ejecuta (workflow Aprobar/Refinar/Descartar)\n"
                "/edit   ✏️ Pide approval por edit/bash\n"
                "/auto   ⚡ Auto-aprueba edits, pide para Bash\n"
                "/bypass 🚀 Sin prompts (default, autonomía)\n"
                "/mode   Mostrar modo + cambiar con botones\n\n"
                "Operación:\n"
                "/status     - estado tarea + sesión activa\n"
                "/cancel     - cancelar tarea activa (SIGINT)\n"
                "/stop       - matar tarea activa (SIGTERM→SIGKILL)\n"
                "/reset      - liberar slot huérfano + matar proceso\n\n"
                "Sesiones:\n"
                "/sessions   - listar sesiones nativas Claude Code\n"
                "/context    - tamaño del historial de la sesión\n"
                "/compact    - reducir historial pesado (con backup)\n"
                "/newsession - rotar a session_id nuevo\n"
                "/clear      - rotar session_id + limpiar log local\n\n"
                "Workspace:\n"
                "/cwd /pwd   - mostrar cwd actual\n"
                "/cd <p>     - cambiar a proyecto de la whitelist\n"
                "/projects   - listar proyectos disponibles\n\n"
                "Diagnóstico:\n"
                "/health /config /version /uptime /tasks /stats\n\n"
                "📎 Archivos: mandá fotos/docs/audio/video → workspace/uploads/\n"
                "💬 Texto libre → tarea para Claude Code (con streaming).\n\n"
                "Tocá los botones de abajo para cambiar modo o ejecutar acciones."
            ),
            reply_to_message_id=message_id,
            reply_markup=kb,
        )
        return True

    if command == "/version":
        cwd = get_user_cwd(user_id)
        sid = get_native_session_id(user_id, cwd)
        active_mode = get_user_mode(user_id)
        emoji, label, _ = MODE_UI[active_mode]
        send_message(
            chat_id,
            f"Cloky Elite Telegram Bot\nv{VERSION} ({VERSION_DATE})\n"
            f"Python {sys.version.split()[0]}\nPID {os.getpid()}\n"
            f"Sandbox: {SANDBOX_MODE}\n"
            f"Permission mode: {emoji} {label} ({active_mode})\n"
            f"Max turns: {CLAUDE_MAX_TURNS}\n"
            f"Sesión activa: {sid[:8]}...{sid[-4:]}",
            reply_to_message_id=message_id,
        )
        return True

    # v1.8.0: comandos slash para cambiar permission mode
    if command in {"/plan", "/edit", "/auto", "/bypass"}:
        # Mapeo cmd → modo
        cmd_to_mode = {
            "/plan":   "plan",
            "/edit":   "default",
            "/auto":   "acceptEdits",
            "/bypass": "bypassPermissions",
        }
        new_mode = cmd_to_mode[command]
        old_mode = get_user_mode(user_id)
        if old_mode == new_mode:
            emoji, label, desc = MODE_UI[new_mode]
            send_message(
                chat_id,
                f"Ya estás en modo {emoji} {label}.\n_{desc}_",
                reply_to_message_id=message_id,
            )
            return True
        ok = set_user_mode(user_id, new_mode)
        if not ok:
            send_message(chat_id, "No se pudo cambiar el modo.", reply_to_message_id=message_id)
            return True
        emoji, label, desc = MODE_UI[new_mode]
        kb = build_inline_keyboard(
            user_id,
            include_operations=should_include_operations_kb(user_id),
            include_plan_actions=(new_mode == "plan"),
        )
        send_message(
            chat_id,
            f"{emoji} Modo cambiado a **{label}**\n_{desc}_\n\n"
            f"El cambio aplica en tu próximo mensaje.",
            reply_to_message_id=message_id,
            reply_markup=kb,
        )
        return True

    if command == "/mode":
        current_mode = get_user_mode(user_id)
        emoji_now, label_now, desc_now = MODE_UI[current_mode]
        lines = [f"Modo actual: {emoji_now} **{label_now}** ({current_mode})"]
        lines.append(f"_{desc_now}_\n")
        lines.append("Modos disponibles:")
        for m in VALID_PERMISSION_MODES:
            e, l, d = MODE_UI[m]
            mark = " ← activo" if m == current_mode else ""
            lines.append(f"  {e} {l}: {d}{mark}")
        lines.append("\nCambialo tocando los botones o con /plan /edit /auto /bypass")
        kb = build_inline_keyboard(
            user_id,
            include_operations=should_include_operations_kb(user_id),
            include_plan_actions=(current_mode == "plan"),
        )
        send_message(
            chat_id,
            "\n".join(lines),
            reply_to_message_id=message_id,
            reply_markup=kb,
        )
        return True

    if command == "/context":
        cwd = get_user_cwd(user_id)
        sid = get_native_session_id(user_id, cwd)
        info = analyze_transcript(native_session_jsonl_path(cwd, sid))
        if not info["exists"]:
            send_message(chat_id, "Esta sesión todavía no tiene historial en disco.",
                         reply_to_message_id=message_id)
            return True
        lines = [
            "📊 Contexto de la sesión",
            f"Proyecto: {cwd}",
            "",
            f"Tamaño:  {fmt_bytes(info['total_bytes'])}",
            f"Eventos: {info['total_lines']}",
            f"Líneas grandes (>{fmt_bytes(BIG_LINE_BYTES)}): {info['big_lines']}",
        ]
        if info["total_bytes"] > WARN_BYTES:
            lines.append("")
            lines.append("⚠️ Historial pesado — /compact lo reduce sin perder el hilo.")
        send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return True

    if command == "/compact":
        # DESACTIVADO: la versión previa truncaba strings dentro del .jsonl
        # interno de Claude Code. Ese formato es interno y cambia entre
        # versiones; editarlo puede romper tool_results, bloques de thinking,
        # firmas y metadata necesaria para reanudar — dejando la sesión en un
        # estado que reanuda mal. Es una causa plausible de los fallos que
        # veníamos persiguiendo.
        cwd = get_user_cwd(user_id)
        sid = get_native_session_id(user_id, cwd)
        info = analyze_transcript(native_session_jsonl_path(cwd, sid))
        tam = fmt_bytes(info["total_bytes"]) if info["exists"] else "sin datos"
        send_message(
            chat_id,
            "⚠️ /compact está desactivado a propósito.\n\n"
            "Editaba directamente el historial interno de Claude Code, y ese "
            "formato es interno: truncarlo puede dejar la sesión imposible de "
            "reanudar.\n\n"
            f"Historial actual: {tam}\n\n"
            "Para aliviar una sesión pesada usá /newsession — arranca limpia "
            "sin tocar el historial viejo, que queda intacto en disco.",
            reply_to_message_id=message_id,
        )
        return True

    if command == "/uptime":
        up = time.time() - START_TIME
        send_message(chat_id, f"Uptime: {_format_duration(up)}", reply_to_message_id=message_id)
        return True

    if command == "/tasks":
        with task_lock:
            snapshot = list(running_tasks.items())
        if not snapshot:
            send_message(chat_id, "Sin tareas activas.", reply_to_message_id=message_id)
            return True
        lines = ["Tareas activas:"]
        for uid, task in snapshot:
            if isinstance(task, RunningTask):
                elapsed = int(time.time() - task.started_at)
                lines.append(f"  user={uid} pid={task.process.pid} elapsed={elapsed}s status={task.last_status}")
            else:
                lines.append(f"  user={uid} estado={task}")
        send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return True

    if command == "/stats":
        s = load_stats()
        first_seen_dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["first_seen"]))
        avg_runtime = s["total_runtime_seconds"] / max(1, s["tasks_total"])
        send_message(
            chat_id,
            (
                "Estadísticas acumuladas:\n\n"
                f"Primera vez:      {first_seen_dt}\n"
                f"Tareas totales:   {s['tasks_total']}\n"
                f"  completadas:    {s['tasks_completed']}\n"
                f"  canceladas:     {s['tasks_cancelled']}\n"
                f"  fallidas:       {s['tasks_failed']}\n"
                f"  bloqueadas:     {s['tasks_blocked_destructive']}\n"
                f"Tiempo total:     {_format_duration(s['total_runtime_seconds'])}\n"
                f"Promedio/tarea:   {_format_duration(avg_runtime)}\n"
                f"Output total:     {s['total_output_chars']:,} chars"
            ),
            reply_to_message_id=message_id,
        )
        return True

    if command == "/config":
        cwd = get_user_cwd(user_id)
        cwd_label = "workspace" if cwd == WORKSPACE_DIR else "proyecto activo"
        proj_count = len(ALLOWED_PROJECTS)
        sid = get_native_session_id(user_id, cwd)
        user_mode = get_user_mode(user_id)
        u_emoji, u_label, _ = MODE_UI[user_mode]
        send_message(
            chat_id,
            (
                "Configuración activa (Claude Code CLI):\n\n"
                f"max_output_tokens: {CLAUDE_MAX_OUTPUT_TOKENS}\n"
                f"thinking_tokens:   {MAX_THINKING_TOKENS}\n"
                f"tool_concurrency:  {CLAUDE_TOOL_CONCURRENCY}\n"
                f"bash_default_ms:   {BASH_DEFAULT_TIMEOUT_MS} ({int(BASH_DEFAULT_TIMEOUT_MS)//60000}min)\n"
                f"bash_max_ms:       {BASH_MAX_TIMEOUT_MS} ({int(BASH_MAX_TIMEOUT_MS)//60000}min)\n"
                f"bash_output_max:   {BASH_MAX_OUTPUT_LENGTH} chars\n"
                f"subprocess_timeout: {CLAUDE_TIMEOUT_SECONDS}s\n"
                f"sandbox_mode:      {SANDBOX_MODE}\n\n"
                f"=== v1.8 Permission modes ===\n"
                f"modo activo (tuyo): {u_emoji} {u_label} ({user_mode})\n"
                f"modo default global: {CLAUDE_DEFAULT_PERMISSION_MODE}\n"
                f"inline_keyboards:   {INLINE_KEYBOARD_ENABLED}\n\n"
                f"=== v1.7 Persistencia nativa ===\n"
                f"max_turns:         {CLAUDE_MAX_TURNS}\n"
                f"session_id:        {sid[:8]}...{sid[-4:]}\n"
                f"native_sessions_db: {NATIVE_SESSIONS_FILE}\n\n"
                f"=== v1.6 Sprint 2 ===\n"
                f"streaming:         {STREAM_ENABLED} (cada {STREAM_EDIT_INTERVAL}s)\n"
                f"max_upload_bytes:  {MAX_UPLOAD_BYTES:,}\n"
                f"uploads_dir:       {UPLOADS_DIR}\n"
                f"projects:          {proj_count} disponibles\n"
                f"cwd actual:        {cwd} ({cwd_label})\n\n"
                f"Backend: {QWEN_BASE_URL}\n"
                f"Model:   {MODEL_ID[:60]}..."
            ),
            reply_to_message_id=message_id,
        )
        return True

    if command == "/status":
        with task_lock:
            task = running_tasks.get(user_id)
        cwd = get_user_cwd(user_id)
        sid = get_native_session_id(user_id, cwd)
        sid_short = f"{sid[:8]}...{sid[-4:]}"
        if isinstance(task, RunningTask):
            elapsed = int(time.time() - task.started_at)
            send_message(
                chat_id,
                f"Tarea activa desde hace {elapsed}s.\n"
                f"Estado: {task.last_status}\n"
                f"Proyecto: {cwd}\n"
                f"Sesión Claude: {sid_short}",
                reply_to_message_id=message_id,
            )
        elif task == "PENDING":
            send_message(
                chat_id,
                f"Tarea inicializando (PENDING). Aún no hay PID.\n"
                f"Proyecto: {cwd}\n"
                f"Sesión Claude: {sid_short}",
                reply_to_message_id=message_id,
            )
        else:
            send_message(
                chat_id,
                f"Sin tarea activa.\n"
                f"Proyecto: {cwd}\n"
                f"Sesión Claude: {sid_short}",
                reply_to_message_id=message_id,
            )
        return True

    if command == "/cwd":
        cwd = get_user_cwd(user_id)
        is_default = cwd == WORKSPACE_DIR
        msg = f"Cwd actual: {cwd}\n"
        msg += "(workspace por defecto)" if is_default else "(proyecto activo via /cd)"
        send_message(chat_id, msg, reply_to_message_id=message_id)
        return True

    if command == "/pwd":
        # Alias de /cwd
        cwd = get_user_cwd(user_id)
        send_message(chat_id, str(cwd), reply_to_message_id=message_id)
        return True

    if command == "/projects":
        # v1.6 S2.3: listar proyectos disponibles
        if not ALLOWED_PROJECTS:
            send_message(
                chat_id,
                "No hay proyectos configurados.\n\n"
                "Para habilitar /cd, agregá al .env:\n"
                'ALLOWED_PROJECTS="nombre1:/path/1,nombre2:/path/2"\n\n'
                "Y reiniciá el servicio.",
                reply_to_message_id=message_id,
            )
            return True
        lines = ["Proyectos disponibles para /cd:\n"]
        cwd = get_user_cwd(user_id)
        for name, path in sorted(ALLOWED_PROJECTS.items()):
            marker = "← actual" if path == cwd else ""
            exists = "✓" if path.exists() else "✗ (no existe)"
            lines.append(f"  /cd {name:15s} {exists} {marker}")
            lines.append(f"      {path}")
        lines.append("\n/cd default  ← volver al workspace original")
        send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return True

    if command == "/cd":
        # v1.6 S2.3: cambiar cwd. Sintaxis: /cd <nombre> o /cd default
        parts = text.strip().split(None, 1)
        if len(parts) < 2:
            send_message(
                chat_id,
                "Uso: /cd <nombre>\n\nUsá /projects para ver disponibles.\n/cd default para volver al workspace.",
                reply_to_message_id=message_id,
            )
            return True
        target = parts[1].strip()
        if target.lower() in {"default", "workspace", "-"}:
            reset_user_cwd(user_id)
            send_message(chat_id, f"Cwd restaurado al workspace:\n{WORKSPACE_DIR}", reply_to_message_id=message_id)
            return True
        if target not in ALLOWED_PROJECTS:
            avail = ", ".join(sorted(ALLOWED_PROJECTS.keys())) or "(ninguno configurado)"
            send_message(
                chat_id,
                f"Proyecto '{target}' no está en la whitelist.\n\nDisponibles: {avail}",
                reply_to_message_id=message_id,
            )
            return True
        path = ALLOWED_PROJECTS[target]
        if not path.exists():
            send_message(chat_id, f"Path no existe en disco: {path}", reply_to_message_id=message_id)
            return True
        set_user_cwd(user_id, path)
        log(f"CD user={user_id} → {target} ({path})")
        send_message(
            chat_id,
            f"Cwd cambiado a proyecto '{target}':\n{path}\n\n"
            "Las próximas tareas se ejecutarán acá. /cd default para revertir.",
            reply_to_message_id=message_id,
        )
        return True

    if command == "/clear":
        # v1.7.0: /clear ahora rota el session_id nativo de Claude Code para el
        # cwd actual. El transcript viejo NO se borra del disco (queda
        # recuperable vía /sessions), pero la próxima interacción usará un
        # session_id fresco. También se limpia el log informativo del bot.
        clear_session(user_id)
        cwd = get_user_cwd(user_id)
        new_sid = rotate_native_session_id(user_id, cwd)
        send_message(
            chat_id,
            (
                f"✅ Contexto rotado.\n"
                f"• Nueva sesión nativa de Claude Code: {new_sid[:8]}...\n"
                f"• Proyecto: {cwd}\n"
                f"• Log local del bot limpiado\n\n"
                f"El transcript anterior queda en disco (usá /sessions para ver)."
            ),
            reply_to_message_id=message_id,
        )
        return True

    if command == "/newsession":
        # v1.7.0: equivalente a /clear pero solo rota sesión nativa, NO toca
        # el log local del bot. Útil cuando querés empezar conversación nueva
        # pero mantener /stats y /status con los registros previos.
        cwd = get_user_cwd(user_id)
        new_sid = rotate_native_session_id(user_id, cwd)
        send_message(
            chat_id,
            (
                f"✅ Nueva sesión nativa Claude Code creada.\n"
                f"• session_id: {new_sid[:8]}...\n"
                f"• Proyecto: {cwd}\n\n"
                f"La sesión anterior queda en disco y se puede consultar con /sessions."
            ),
            reply_to_message_id=message_id,
        )
        return True

    if command == "/sessions":
        # v1.7.0: lista las sesiones nativas registradas para este usuario y
        # marca cuál está activa en cada proyecto. También muestra los
        # transcripts en disco con tamaño y última modificación.
        mapping = list_native_sessions(user_id)
        if not mapping:
            send_message(
                chat_id,
                "No hay sesiones nativas registradas todavía.\n"
                "Mandá cualquier mensaje al bot para crear la primera.",
                reply_to_message_id=message_id,
            )
            return True

        transcripts = find_jsonl_transcripts(user_id)
        # Index por cwd para fácil lookup
        ts_by_cwd = {t["cwd"]: t for t in transcripts}

        cwd_now = get_user_cwd(user_id)
        lines = ["📋 Sesiones nativas Claude Code:\n"]
        for cwd_str, sid in sorted(mapping.items()):
            active = " ← activa (cwd actual)" if Path(cwd_str) == cwd_now.resolve() else ""
            lines.append(f"📁 {cwd_str}{active}")
            lines.append(f"   session_id: {sid[:8]}...{sid[-4:]}")
            info = ts_by_cwd.get(cwd_str)
            if info and info.get("exists"):
                size_kb = info.get("size_bytes", 0) / 1024
                mtime_str = ""
                try:
                    import datetime as _dt
                    mtime = info.get("mtime")
                    if mtime:
                        mtime_str = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
                lines.append(f"   transcript: {size_kb:.1f} KB · modificado {mtime_str}")
            else:
                lines.append(f"   transcript: (no creado todavía en disco)")
            lines.append("")

        lines.append("Comandos:")
        lines.append("/newsession — crea sesión nueva en el cwd actual")
        lines.append("/clear      — alias de /newsession + limpia log local")

        send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return True

    if command == "/cancel":
        # v1.6.1 BUG-2 fix: si el slot está en PENDING (run_claude_task no llegó
        # a crear el subprocess), liberarlo manualmente. Antes era "esperá 1-2s"
        # y quedaba pegado indefinidamente.
        with task_lock:
            task = running_tasks.get(user_id)
        if not isinstance(task, RunningTask):
            if task == "PENDING":
                with task_lock:
                    running_tasks.pop(user_id, None)
                try:
                    clear_running_pid(user_id)
                except Exception:
                    pass
                send_message(
                    chat_id,
                    "Slot PENDING liberado manualmente. Podés enviar una nueva tarea.",
                    reply_to_message_id=message_id,
                )
            else:
                send_message(chat_id, "No hay tarea activa para cancelar.", reply_to_message_id=message_id)
            return True
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(task.process.pid), signal.SIGINT)
            else:
                task.process.send_signal(signal.SIGINT)
            send_message(
                chat_id,
                "Señal SIGINT enviada. Si Claude no responde en 5s, usá /stop.",
                reply_to_message_id=message_id,
            )
        except Exception as exc:
            send_message(chat_id, f"No se pudo cancelar: {exc}", reply_to_message_id=message_id)
        return True

    if command == "/stop":
        # v1.6.1 BUG-3 fix: si el slot está en PENDING, liberarlo manualmente.
        with task_lock:
            task = running_tasks.get(user_id)
        if not isinstance(task, RunningTask):
            if task == "PENDING":
                with task_lock:
                    running_tasks.pop(user_id, None)
                try:
                    clear_running_pid(user_id)
                except Exception:
                    pass
                send_message(
                    chat_id,
                    "Slot PENDING huérfano liberado. Podés enviar una nueva tarea.",
                    reply_to_message_id=message_id,
                )
            else:
                send_message(chat_id, "No hay tarea activa para cancelar.", reply_to_message_id=message_id)
            return True
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(task.process.pid), signal.SIGTERM)
            else:
                task.process.terminate()
            send_message(chat_id, "⏹ SIGTERM enviado. Si en 3s sigue activo, se forzará SIGKILL.", reply_to_message_id=message_id)
            # Escalada: si en 3 segundos sigue vivo, SIGKILL
            def _force_kill_if_alive():
                time.sleep(3)
                try:
                    if task.process.poll() is None:
                        if hasattr(os, "killpg"):
                            os.killpg(os.getpgid(task.process.pid), signal.SIGKILL)
                        else:
                            task.process.kill()
                        send_message(chat_id, "⏹ SIGKILL aplicado.", reply_to_message_id=message_id, notify=False)
                except Exception:
                    pass
            threading.Thread(target=_force_kill_if_alive, daemon=True).start()
        except Exception as exc:
            send_message(chat_id, f"No se pudo cancelar: {exc}", reply_to_message_id=message_id)
        return True

    if command == "/reset":
        # v1.6.1 nuevo: limpia TODO el estado del usuario.
        # - slot en running_tasks (incluso PENDING huérfano)
        # - PID en disco
        # - cwd dinámico vuelve a workspace
        # NO limpia historial de sesión (usar /clear para eso).
        with task_lock:
            task = running_tasks.get(user_id)
            running_tasks.pop(user_id, None)
        try:
            if isinstance(task, RunningTask):
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(task.process.pid), signal.SIGKILL)
                else:
                    task.process.kill()
        except Exception:
            pass
        try:
            clear_running_pid(user_id)
        except Exception:
            pass
        reset_user_cwd(user_id)
        send_message(
            chat_id,
            "🔄 Reset completo:\n"
            "• Slot liberado\n"
            "• Proceso (si había) matado con SIGKILL\n"
            "• PID huérfano limpiado\n"
            "• cwd vuelto al workspace\n\n"
            "Historial preservado (usá /clear para borrarlo).",
            reply_to_message_id=message_id,
        )
        return True

    if command == "/health":
        result = qwen_health()
        send_message(chat_id, result, reply_to_message_id=message_id)
        return True

    return False


# ============================================================================
# Health check end-to-end (Patch 5)
# ============================================================================

def qwen_health() -> str:
    """
    Health de dos niveles:
      [1/2] GET /health → server vivo
      [2/2] POST /v1/messages → schema Anthropic + modelo cargado + roundtrip real
    """
    results: list[str] = []

    # Nivel 1: liveness
    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{QWEN_BASE_URL}/health",
            headers={"Authorization": f"Bearer {QWEN_API_KEY}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read(512).decode("utf-8", errors="ignore")
            results.append(f"[1/2] /health HTTP {response.status} ({int((time.time() - t0) * 1000)}ms)")
            if body.strip():
                results.append(f"      body: {body.strip()[:200]}")
    except Exception as exc:
        results.append(f"[1/2] /health FAILED: {type(exc).__name__}: {exc}")

    # Nivel 2: end-to-end con schema Anthropic
    t1 = time.time()
    try:
        payload = {
            "model": MODEL_ID,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
        req = urllib.request.Request(
            f"{QWEN_BASE_URL}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {QWEN_API_KEY}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            elapsed_ms = int((time.time() - t1) * 1000)
            raw = response.read(2048).decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(raw)
                model_returned = parsed.get("model", "?")
                stop_reason = parsed.get("stop_reason", "?")
                results.append(
                    f"[2/2] /v1/messages HTTP {response.status} ({elapsed_ms}ms) "
                    f"model={model_returned} stop_reason={stop_reason}"
                )
            except Exception:
                results.append(
                    f"[2/2] /v1/messages HTTP {response.status} ({elapsed_ms}ms) "
                    f"NON-JSON body: {raw[:200]}"
                )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(512).decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        results.append(f"[2/2] /v1/messages HTTP {exc.code}: {body[:300]}")
    except Exception as exc:
        results.append(f"[2/2] /v1/messages FAILED: {type(exc).__name__}: {exc}")

    return redact("\n".join(results))


# ============================================================================
# Handler principal de updates (Patch 3: reserva atómica)
# ============================================================================

def handle_callback_query(callback_query: dict[str, Any]) -> None:
    """
    v1.8.0: procesa taps en botones inline.

    Estructura del callback_query (Telegram Bot API):
      id: string (necesario para answerCallbackQuery)
      from: {id: user_id, ...}
      message: {chat: {id}, message_id, ...}  (el mensaje original con el teclado)
      data: string (callback_data del botón tocado)

    Acciones soportadas:
      mode:plan        → set permission_mode = plan
      mode:edit        → set permission_mode = default
      mode:auto        → set permission_mode = acceptEdits
      mode:bypass      → set permission_mode = bypassPermissions
      op:stop          → cancelar tarea activa (igual que /stop)
      op:reset         → /reset
      op:status        → /status (responde con info, no toca estado)
      op:sessions      → /sessions
      plan:approve     → ejecutar último prompt en bypassPermissions
      plan:refine      → "ok, esperando mensaje refinado"
      plan:discard     → "plan descartado"
    """
    cq_id = callback_query.get("id", "")
    from_user = callback_query.get("from", {})
    user_id_int = int(from_user.get("id", 0))
    user_id = str(user_id_int)
    data = callback_query.get("data", "")

    # Security check: solo usuarios allowlisted pueden disparar callbacks
    if not is_allowed(user_id_int):
        log(f"CALLBACK_DENY user={user_id} data={data!r}")
        if cq_id:
            answer_callback_query(cq_id, "No autorizado", show_alert=True)
        return

    message = callback_query.get("message", {})
    chat = message.get("chat", {})
    chat_id = int(chat.get("id", 0))
    message_id_orig = int(message.get("message_id", 0))

    log(f"CALLBACK user={user_id} data={data!r}")

    # Parsear "tipo:accion"
    if ":" not in data:
        if cq_id:
            answer_callback_query(cq_id, "Acción desconocida")
        return
    action_type, action_value = data.split(":", 1)

    # ── MODE: cambiar permission_mode del usuario ────────────────────────
    if action_type == "mode":
        full_mode = _MODE_KEY_TO_MODE.get(action_value)
        if not full_mode:
            answer_callback_query(cq_id, f"Modo desconocido: {action_value}")
            return
        old_mode = get_user_mode(user_id)
        if old_mode == full_mode:
            answer_callback_query(cq_id, f"Ya estás en {MODE_UI[full_mode][1]}")
            return
        ok = set_user_mode(user_id, full_mode)
        if not ok:
            answer_callback_query(cq_id, "No se pudo cambiar el modo", show_alert=True)
            return
        emoji, label, desc = MODE_UI[full_mode]
        answer_callback_query(cq_id, f"Modo cambiado a {emoji} {label}")

        # Refrescar el teclado del mensaje original para que muestre el check
        # nuevo en el modo activo. Usamos editMessageReplyMarkup.
        try:
            new_kb = build_inline_keyboard(
                user_id,
                include_operations=should_include_operations_kb(user_id),
                include_plan_actions=(full_mode == "plan"),
            )
            if new_kb is not None:
                telegram_api(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id_orig,
                        "reply_markup": new_kb,
                    },
                    timeout=20,
                )
        except Exception as exc:
            log(f"EDIT_REPLY_MARKUP_ERROR {type(exc).__name__}: {exc}")

        # Mensaje informativo aparte (con sound off para no molestar)
        send_message(
            chat_id,
            f"{emoji} Modo cambiado: **{label}**\n_{desc}_\n\n"
            f"El cambio aplica en tu próximo mensaje.",
            notify=False,
        )
        return

    # ── OP: operaciones (Stop, Reset, Status, Sessions) ──────────────────
    if action_type == "op":
        if action_value == "stop":
            # Equivalente a /stop (mismo handler)
            answer_callback_query(cq_id, "Deteniendo tarea…")
            # Reutilizamos la lógica del comando /stop
            _trigger_command(user_id, chat_id, message_id_orig, "/stop")
            return
        if action_value == "reset":
            answer_callback_query(cq_id, "Reset solicitado")
            _trigger_command(user_id, chat_id, message_id_orig, "/reset")
            return
        if action_value == "status":
            answer_callback_query(cq_id, "Consultando estado…")
            _trigger_command(user_id, chat_id, message_id_orig, "/status")
            return
        if action_value == "mode":
            answer_callback_query(cq_id, "Elegí el modo")
            send_message(chat_id, "⚙️ Modo de permisos:", notify=False,
                         reply_markup=build_mode_keyboard(user_id))
            return
        if action_value == "sessions":
            answer_callback_query(cq_id, "Cargando sesiones…")
            _trigger_command(user_id, chat_id, message_id_orig, "/sessions")
            return
        answer_callback_query(cq_id, f"Operación desconocida: {action_value}")
        return

    # ── PLAN: workflow Plan Mode (Approve / Refine / Discard) ────────────
    if action_type == "plan":
        if action_value == "approve":
            # Recuperar último prompt y re-ejecutarlo en bypassPermissions
            last = get_last_prompt(user_id)
            if not last or not last.get("prompt"):
                answer_callback_query(
                    cq_id,
                    "No hay plan previo para aprobar",
                    show_alert=True,
                )
                return
            # Cambiar modo a bypassPermissions
            set_user_mode(user_id, "bypassPermissions")
            answer_callback_query(cq_id, "✅ Aprobado. Ejecutando…")
            # Mandar mensaje informativo
            send_message(
                chat_id,
                "✅ Plan aprobado. Ejecutando ahora en modo Bypass.\n"
                "_Tu modo quedó cambiado a Bypass. Tocá 📋 Plan para volver._",
                notify=False,
            )
            # Construir prompt de ejecución
            execute_prompt = (
                "Ejecutá el plan que acabás de proponer en la respuesta "
                "anterior, sin cambiar la estrategia. Implementá los pasos "
                "uno por uno y reportá progreso."
            )
            # Marcar slot PENDING y lanzar thread (igual que handle_update normal)
            with task_lock:
                if running_tasks.get(user_id):
                    send_message(
                        chat_id,
                        "Ya hay una tarea activa. Esperá a que termine.",
                        notify=False,
                    )
                    return
                running_tasks[user_id] = "PENDING"
            threading.Thread(
                target=run_claude_task,
                args=(user_id, chat_id, message_id_orig, execute_prompt),
                daemon=True,
            ).start()
            # Limpiar last_prompt para no aprobar dos veces sin querer
            clear_last_prompt(user_id)
            return
        if action_value == "refine":
            answer_callback_query(cq_id, "OK, esperando refinamiento")
            send_message(
                chat_id,
                "✏️ Mandá ahora los ajustes al plan.\n"
                "Seguís en modo 📋 Plan: tu próximo mensaje va a analizar el plan refinado.",
                notify=False,
            )
            return
        if action_value == "discard":
            answer_callback_query(cq_id, "Plan descartado")
            clear_last_prompt(user_id)
            send_message(
                chat_id,
                "❌ Plan descartado. Seguís en modo 📋 Plan.\n"
                "Cambiá de modo con los botones si querés ejecutar otra cosa.",
                notify=False,
            )
            return
        answer_callback_query(cq_id, f"Acción plan desconocida: {action_value}")
        return

    # Acción desconocida
    answer_callback_query(cq_id, f"Acción no implementada: {action_type}")


def _trigger_command(user_id: str, chat_id: int, message_id: int, cmd: str) -> None:
    """
    Helper: ejecuta un comando como si el usuario lo hubiera tipeado.
    Usado por callback handlers (op:stop, op:reset, etc.).
    """
    try:
        handle_command(user_id, chat_id, message_id, cmd)
    except Exception as exc:
        log(f"TRIGGER_COMMAND_ERROR cmd={cmd} {type(exc).__name__}: {exc}")


def handle_update(update: dict[str, Any]) -> None:
    """
    v1.8.0: rutea entre message (texto/archivos normales) y callback_query
    (tap en botones inline). Ambos pasan por la misma allowlist de seguridad.
    """
    # v1.8.0: callback_query — tap en botones inline
    callback_query = update.get("callback_query")
    if callback_query:
        try:
            handle_callback_query(callback_query)
        except Exception as exc:
            log(f"HANDLE_CALLBACK_CRASH {type(exc).__name__}: {exc}")
            log(traceback.format_exc())
            # Intentar al menos cerrar el loading del botón
            try:
                cq_id = callback_query.get("id", "")
                if cq_id:
                    answer_callback_query(cq_id, "Error procesando acción.", show_alert=True)
            except Exception:
                pass
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat", {})
    from_user = message.get("from", {})
    chat_id = int(chat.get("id"))
    user_id_int = int(from_user.get("id"))
    user_id = str(user_id_int)
    message_id = int(message.get("message_id"))

    text = message.get("text") or message.get("caption") or ""

    if not is_allowed(user_id_int):
        log(f"DENY user={user_id}")
        send_message(chat_id, "Usuario no autorizado.", reply_to_message_id=message_id)
        return

    # v1.6 S2.2: detección y descarga de archivos
    file_info = extract_file_info(message)
    if file_info:
        file_id, suggested_name, size = file_info
        # Pre-validación de tamaño
        if size and size > MAX_UPLOAD_BYTES:
            send_message(
                chat_id,
                f"Archivo demasiado grande: {size:,} bytes (máximo {MAX_UPLOAD_BYTES:,}).",
                reply_to_message_id=message_id,
            )
            return
        # Asegurar nombre único en uploads/
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = UPLOADS_DIR / f"{ts}_{suggested_name}"
        i = 1
        while target.exists():
            target = UPLOADS_DIR / f"{ts}_{i}_{suggested_name}"
            i += 1
        log(f"UPLOAD user={user_id} → {target.name} ({size} bytes)")
        send_chat_action(chat_id, "upload_document")
        ok, info = telegram_download_file(file_id, target)
        if not ok:
            send_message(chat_id, f"❌ Fallo al descargar archivo: {info}", reply_to_message_id=message_id)
            return
        # Si hay caption, tratarla como tarea con path inyectado
        rel_path = target.resolve()
        if text.strip():
            # Inyectar el path en el texto que se procesará como tarea
            text = f"El usuario subió un archivo en: {rel_path}\n\nPetición del usuario:\n{text.strip()}"
            send_message(
                chat_id,
                f"📎 Archivo recibido: {target.name} ({info})\nProcesando con tu petición...",
                reply_to_message_id=message_id,
                notify=False,
            )
            # Cae al flujo normal: handle_command no aplicará por ser texto largo,
            # check_forbidden no aplicará, y va a run_claude_task.
        else:
            # Solo archivo sin caption: avisar al usuario que está disponible
            send_message(
                chat_id,
                f"📎 Archivo recibido: {target.name}\nPath: {rel_path}\n\n"
                "Envia un mensaje describiendo qué hacer con él.",
                reply_to_message_id=message_id,
            )
            return

    if not text.strip():
        send_message(chat_id, "Envíe texto, o adjunte un archivo con instrucciones en el caption.", reply_to_message_id=message_id)
        return

    if handle_command(user_id, chat_id, message_id, text):
        return

    # v1.2: Defensa pre-spawn contra comandos destructivos obvios.
    forbidden = check_forbidden_command(text)
    if forbidden:
        log(f"FORBIDDEN_COMMAND user={user_id} pattern={forbidden!r}")
        stats_increment("tasks_blocked_destructive")
        send_message(
            chat_id,
            f"⛔ Comando destructivo detectado y bloqueado:\n  {forbidden}\n\n"
            "Si necesitás operar en discos/dispositivos, hacelo manualmente en consola.",
            reply_to_message_id=message_id,
        )
        return

    # Patch 3: reserva atómica del slot. Si otro thread tomó el slot entre el
    # check y el thread.start, esta sección nunca permite doble registro.
    with task_lock:
        active = running_tasks.get(user_id)
        if isinstance(active, RunningTask):
            elapsed = int(time.time() - active.started_at)
            status_text = active.last_status
            release_lock = False
        elif active == "PENDING":
            elapsed = 0
            status_text = "inicializando"
            release_lock = False
        else:
            running_tasks[user_id] = "PENDING"  # marcador pre-Popen
            release_lock = True

    if not release_lock:
        send_message(
            chat_id,
            f"Ya hay una tarea activa desde hace {elapsed}s ({status_text}). Use /stop o espere.",
            reply_to_message_id=message_id,
        )
        return

    send_chat_action(chat_id)
    threading.Thread(
        target=run_claude_task,
        args=(user_id, chat_id, message_id, text),
        daemon=True,
    ).start()


# ============================================================================
# Polling Telegram
# ============================================================================

def get_updates(offset: int | None) -> list[dict[str, Any]]:
    """
    v1.6.1 BUG-5 fix: nunca propagar excepciones. telegram_api ya tiene retry
    interno con backoff; si igual falla, retornar lista vacía para que el
    main loop reintente en la próxima iteración sin morir.
    """
    payload: dict[str, Any] = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "edited_message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    try:
        result = telegram_api("getUpdates", payload, timeout=POLL_TIMEOUT + 10)
        if not result.get("ok"):
            # telegram_api ya logueó el detalle; respaldamos con log adicional
            err = result.get("description") or result.get("error") or "unknown"
            log(f"GET_UPDATES_NOT_OK {err}")
            time.sleep(2.0)  # backoff suave para no spammear si el endpoint está caído
            return []
        return result.get("result", [])
    except Exception as exc:
        log(f"GET_UPDATES_CRASH {type(exc).__name__}: {exc}")
        time.sleep(2.0)
        return []


_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: Any) -> None:
    """v1.4 S1.11: graceful shutdown ante SIGTERM/SIGINT."""
    global _shutdown_requested
    _shutdown_requested = True
    log(f"SHUTDOWN_SIGNAL signum={signum}")


def preflight_checks() -> None:
    """v1.4 S1.12 + S1.13: validar entorno antes de empezar a operar.
    v1.7.0: verifica versión claude (>=2.0.0 requerido para --session-id)."""
    # 1. Claude Code CLI accesible
    claude_resolved = shutil.which(CLAUDE_BIN)
    if not claude_resolved:
        log(f"PREFLIGHT_FAIL claude CLI no encontrado en PATH (CLAUDE_BIN={CLAUDE_BIN})")
    else:
        log(f"PREFLIGHT_OK claude={claude_resolved}")

        # v1.7.0: verificar versión claude
        try:
            result = subprocess.run(
                [claude_resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ver_line = (result.stdout or result.stderr or "").strip()
            # Parsear versión: formato típico "2.1.150 (Claude Code)" o similar
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", ver_line)
            if m:
                major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
                ver_tuple = (major, minor, patch)
                log(f"PREFLIGHT_OK claude_version={major}.{minor}.{patch} (raw: {ver_line[:80]})")
                # Requerido para --session-id: 2.0.0
                if ver_tuple < (2, 0, 0):
                    log(
                        f"PREFLIGHT_FAIL claude version {major}.{minor}.{patch} es <2.0.0. "
                        f"v1.7 requiere --session-id/--resume disponibles desde 2.0.0. "
                        f"Actualizá con: claude update"
                    )
                # Warning si <2.1.140 (estabilidad)
                elif ver_tuple < (2, 1, 140):
                    log(
                        f"PREFLIGHT_WARN claude version {major}.{minor}.{patch} es <2.1.140. "
                        f"Funciona pero recomendamos actualizar. Comando: claude update"
                    )
            else:
                log(f"PREFLIGHT_WARN no se pudo parsear versión claude (raw: {ver_line[:80]!r})")
        except subprocess.TimeoutExpired:
            log("PREFLIGHT_WARN claude --version timeout >10s")
        except Exception as exc:
            log(f"PREFLIGHT_WARN claude --version error: {type(exc).__name__}: {exc}")

    # 2. Backend Qwen accesible (retry x5 si devuelve 503 en arranque)
    health_ok = False
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                f"{QWEN_BASE_URL}/health",
                headers={"Authorization": f"Bearer {QWEN_API_KEY}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 503 and attempt < 4:
                    log(f"PREFLIGHT_WAIT backend 503 (intento {attempt+1}/5), reintentando en 2s...")
                    time.sleep(2)
                    continue
                log(f"PREFLIGHT_OK backend {QWEN_BASE_URL} HTTP {response.status}")
                health_ok = True
                break
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt < 4:
                log(f"PREFLIGHT_WAIT backend 503 (intento {attempt+1}/5), reintentando en 2s...")
                time.sleep(2)
                continue
            log(f"PREFLIGHT_WARN backend {QWEN_BASE_URL} HTTP {exc.code}")
            break
        except Exception as exc:
            if attempt < 4:
                log(f"PREFLIGHT_WAIT backend error (intento {attempt+1}/5): {type(exc).__name__}, reintentando en 2s...")
                time.sleep(2)
                continue
            log(f"PREFLIGHT_WARN backend {QWEN_BASE_URL} no responde: {type(exc).__name__}: {exc}")
            break
    if not health_ok:
        log(f"PREFLIGHT_WARN backend {QWEN_BASE_URL} no se recuperó tras 5 intentos")

    # 3. Workspace escribible
    test_file = WORKSPACE_DIR / ".write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        log(f"PREFLIGHT_OK workspace escribible: {WORKSPACE_DIR}")
    except Exception as exc:
        log(f"PREFLIGHT_FAIL workspace NO escribible: {exc}")

    # 4. State y logs escribibles
    for d in [STATE_DIR, LOG_DIR]:
        test = d / ".write_test"
        try:
            test.write_text("ok")
            test.unlink()
        except Exception as exc:
            log(f"PREFLIGHT_FAIL {d} NO escribible: {exc}")

    # 5. v1.7.0: verificar que ~/.claude/projects/ existe o se puede crear
    # (Claude Code lo crea solo en el primer --session-id, pero un check
    # temprano informa si hay un permission issue.)
    home = Path(os.path.expanduser("~"))
    projects_root = home / ".claude" / "projects"
    try:
        projects_root.mkdir(parents=True, exist_ok=True)
        log(f"PREFLIGHT_OK ~/.claude/projects accesible: {projects_root}")
    except Exception as exc:
        log(f"PREFLIGHT_WARN ~/.claude/projects no creable: {exc}")


def main() -> None:
    log(f"START {APP_NAME} v{VERSION} ({VERSION_DATE}) pid={os.getpid()}")
    log(f"WORKSPACE {WORKSPACE_DIR}")
    log(f"BACKEND {QWEN_BASE_URL} model={MODEL_ID}")
    log(f"SANDBOX_MODE {SANDBOX_MODE}")
    if SANDBOX_MODE == "open":
        log("WARNING modo OPEN — el bot tiene acceso a TODO $HOME")
    log(
        f"CONFIG max_output_tokens={CLAUDE_MAX_OUTPUT_TOKENS} "
        f"tool_concurrency={CLAUDE_TOOL_CONCURRENCY} "
        f"timeout={CLAUDE_TIMEOUT_SECONDS}s "
        f"bash_default={BASH_DEFAULT_TIMEOUT_MS}ms "
        f"bash_max={BASH_MAX_TIMEOUT_MS}ms "
        f"thinking_tokens={MAX_THINKING_TOKENS}"
    )
    log(
        f"V1.7 permission_mode={CLAUDE_DEFAULT_PERMISSION_MODE} "
        f"max_turns={CLAUDE_MAX_TURNS} "
        f"native_sessions={NATIVE_SESSIONS_FILE}"
    )

    # v1.4 S1.11: graceful shutdown
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
    except Exception:
        pass

    # v1.4 S1.4: matar procesos huérfanos de runs previas
    cleanup_orphan_pids()

    # v1.4 S1.1: crear CLAUDE.md si no existe
    ensure_claude_md()

    # v1.4 S1.12 + S1.13: validar entorno
    preflight_checks()

    offset: int | None = None

    me = telegram_api("getMe", {}, timeout=20)
    log(f"TELEGRAM_GETME {me}")

    register_bot_commands()

    log(f"READY polling Telegram")

    while not _shutdown_requested:
        try:
            updates = get_updates(offset)
            for update in updates:
                if _shutdown_requested:
                    break
                offset = int(update["update_id"]) + 1
                handle_update(update)
        except KeyboardInterrupt:
            log("STOP KeyboardInterrupt")
            break
        except Exception as exc:
            log(f"MAIN_LOOP_ERROR {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            time.sleep(5)

    # v1.4 S1.11: cleanup al salir
    log("SHUTTING_DOWN matando tareas activas...")
    with task_lock:
        active = [t for t in running_tasks.values() if isinstance(t, RunningTask)]
    for task in active:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(task.process.pid), signal.SIGTERM)
            else:
                task.process.terminate()
        except Exception:
            pass
    log("STOPPED")


if __name__ == "__main__":
    main()
