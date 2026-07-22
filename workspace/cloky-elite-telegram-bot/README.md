# Cloky Elite Telegram Bot

> **Production-grade Telegram bridge for Claude Code CLI with local LLM backend.**
> Stdlib-only Python, OS-level sandbox, streaming output, multi-project, file uploads.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![systemd](https://img.shields.io/badge/systemd-247+-orange.svg)](https://systemd.io/)
[![No deps](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)]()

---

## What it is

Cloky is a Telegram bot that bridges your phone to a **local Claude Code CLI** running against a **local Anthropic-compatible backend** (llama.cpp / TurboQuant / vLLM / Ollama). You send messages from anywhere, your local LLM does the work.

Unlike most Claude Code Telegram bridges, Cloky targets **a local, self-hosted setup** — no Anthropic API calls, no cost per token, no data leaving your network.

```
Telegram (your phone)
    │
    │ HTTPS
    ▼
bot.py (Python stdlib, no deps)
    │ subprocess
    ▼
Claude Code CLI 2.x
    │ HTTP (ANTHROPIC_BASE_URL=http://127.0.0.1:8080)
    ▼
llama.cpp / vLLM / Ollama with Qwen, Llama, Mistral, etc.
```

---

## Why Cloky

Most Claude Code Telegram bots target Anthropic's hosted API. **Cloky targets your local stack.** That's the point.

| | Cloky | Most others |
|---|---|---|
| Backend | Local LLM via `ANTHROPIC_BASE_URL` | Anthropic API only |
| Cost | $0 per token | $$$ per token |
| Privacy | Data stays on your machine | Routed through Anthropic |
| Dependencies | Python stdlib only | 20+ packages typically |
| Sandbox | systemd OS-level (`ProtectHome=tmpfs`, syscall filter, capabilities=0) | Logical allowlists in code |
| Pre-spawn destructive block | 11 regex patterns | Usually none |
| Token redaction | 11 patterns in logs | Usually none |
| Configurable security | `strict` / `open` modes runtime-switchable | Hardcoded |

---

## Features

### Core
- Telegram bridge → Claude Code CLI → local Anthropic-compatible backend
- Allowlist auth by Telegram user ID
- Auto-approve mode (`--dangerously-skip-permissions`)
- Stream-json output parsed in real time
- Session history persisted per user (file-locked JSON)

### Hardening (10 layers)
1. Telegram allowlist by user ID
2. Anti-prompt-injection (neutralizes `[system]`/`[user]` tags)
3. Pre-spawn block of 11 destructive command patterns
4. systemd sandbox (strict mode): `ProtectHome=tmpfs` + scoped `BindPaths`
5. `CapabilityBoundingSet=` empty
6. Restrictive `SystemCallFilter`
7. `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`
8. Token redaction in logs (11 patterns)
9. `fcntl.flock` on sessions and stats files
10. UID verification before killing orphan PIDs

### Operations (Sprint 1)
- `/cancel` (soft SIGINT) vs `/stop` (SIGTERM → SIGKILL escalation)
- CLAUDE.md auto-generated in workspace
- Orphan PID recovery at startup
- Log rotation internal (10MB × 3 files)
- Graceful shutdown on SIGTERM (systemd-friendly)
- Pre-flight checks at startup
- Long-task notification with sound (>120s)

### Sprint 2 (latest)
- **Streaming output to Telegram** — edits the message progressively as Claude generates, rate-limited to avoid 429
- **File uploads** — send photos/docs/audio/video; lands in `workspace/uploads/`, path injected in prompt
- **Multi-project with `/cd <name>`** — whitelist projects, switch cwd per user; in strict mode, install_service.sh auto-exposes paths

### Telemetry
- `/version`, `/uptime`, `/tasks`, `/stats`, `/health`, `/config`
- Accumulated metrics across restarts
- Structured logs with redacted tokens

---

## Quickstart

### Prerequisites

- Linux with systemd ≥ 247 (Ubuntu 22.04+, Debian 12+, WSL2 Ubuntu)
- Python 3.11+ (only stdlib, no pip install)
- Claude Code CLI installed
- Local Anthropic-compatible backend running
- A Telegram bot token from @BotFather

### Install

```bash
git clone https://github.com/YOUR_USER/cloky-elite-telegram-bot.git ~/cloky-elite-telegram-bot
cd ~/cloky-elite-telegram-bot

cp .env.example .env
chmod 600 .env
$EDITOR .env    # set TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_ID, ANTHROPIC_BASE_URL

bash install_service.sh
systemctl --user start cloky-elite-telegram-bot.service
journalctl --user -u cloky-elite-telegram-bot.service -f
```

### Switch to open sandbox (full $HOME access)

```bash
BOT_SANDBOX_MODE=open bash install_service.sh
systemctl --user daemon-reload
systemctl --user restart cloky-elite-telegram-bot.service
```

---

## Commands

```
/start /help    Commands + sandbox mode
/status         Active task state
/cancel         SIGINT (cancel current op, keep session)
/stop           SIGTERM → SIGKILL escalation
/clear          Clear user context history
/cwd /pwd       Show current working directory
/cd <project>   Switch to project from ALLOWED_PROJECTS
/cd default     Reset to workspace
/projects       List available projects
/health         End-to-end backend probe
/config         Tokens, timeouts, sandbox, streaming
/version        Version + Python + PID + sandbox mode
/uptime         Time since last restart
/tasks          Active tasks across users
/stats          Accumulated metrics
<free text>     Task for Claude Code (streamed)
<file + caption> Upload file, process with caption as task
```

---

## Configuration

```ini
# Required
TELEGRAM_BOT_TOKEN="123456:ABC..."
ALLOWED_TELEGRAM_USER_ID="123456789"
ANTHROPIC_BASE_URL="http://127.0.0.1:8080"
ANTHROPIC_AUTH_TOKEN="sk-your-local-token"

# Performance (Claude Code 2.x defaults, 2026-05)
CLAUDE_CODE_MAX_OUTPUT_TOKENS="32000"
CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY="8"
BASH_DEFAULT_TIMEOUT_MS="600000"
BASH_MAX_TIMEOUT_MS="1800000"
CLAUDE_TIMEOUT_SECONDS="1800"

# Sprint 2 features
STREAM_ENABLED="true"
STREAM_EDIT_INTERVAL="1.5"
MAX_UPLOAD_BYTES="52428800"
ALLOWED_PROJECTS="cobravivo:/home/user/cobravivo,viper:/home/user/viper"

# Autonomy
CLAUDE_AUTO_APPROVE="true"
```

See `.env.example` for the full annotated list.

---

## Sandbox modes

| | `strict` (default) | `open` |
|---|---|---|
| Filesystem | `ProtectHome=tmpfs` + scoped BindPaths | `ProtectHome=false` |
| Use case | Production autonomous | Auditing any project in $HOME |
| `systemd-analyze` score | 3–5 (OK / GOOD) | 5–7 (MEDIUM) |
| `NoNewPrivileges` | active | active |
| `CapabilityBoundingSet=` empty | active | active |
| `SystemCallFilter` restrictive | active | active |
| Pre-spawn destructive block | active | active |
| Anti-prompt-injection | active | active |

Switching modes is one command. The mode persists in `.sandbox_mode`.

---

## What it does NOT do

- Does **not** call Anthropic's hosted API. This is for local backends.
- Does **not** modify your llama.cpp / vLLM / Ollama. Cloky is a strict HTTP client.
- Does **not** ship with voice transcription. Wire Whisper externally if needed.
- Does **not** require an SDK. Uses `claude --print --output-format stream-json` directly.
- Does **not** require any pip install. Stdlib only.

---

## Comparison to similar projects

|  | Cloky | RichardAtCT | linuz90 | NachoSEO | seedprod |
|---|:-:|:-:|:-:|:-:|:-:|
| Backend: local LLM first-class | ✓ | partial | partial | ✗ | partial |
| systemd OS-level sandbox | ✓ | ✗ | ✗ | ✗ | ✗ |
| Pre-spawn destructive block | ✓ | partial | ✗ | ✗ | ✗ |
| Token redaction in logs | ✓ (11) | partial | ✗ | ✗ | ✗ |
| Streaming output | ✓ | ✓ | ✓ | ✓ | ✗ |
| File uploads | ✓ | ✓ | partial | partial | ✗ |
| Multi-project | ✓ `/cd` | ✓ topics | ✗ | ✗ | ✗ |
| Voice transcription | ✗ | ✗ | ✓ | ✓ | ✓ |
| Dependencies | stdlib | 20+ | 15+ | 25+ | 5+ |
| LOC | ~2000 | ~8000 | ~1500 | ~3000 | ~600 |

Cloky's niche: **secure local-first** Claude Code on Telegram.

---

## Security model

Even in `open` mode, the following remain active:

- `NoNewPrivileges=true` — no sudo escalation
- `CapabilityBoundingSet=` empty
- Restrictive `SystemCallFilter`
- `RestrictNamespaces`, `LockPersonality`, `RestrictSUIDSGID`
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`
- Pre-spawn regex block on 11 destructive commands
- Anti-prompt-injection
- Token redaction in all logs and outbound messages

See `SECURITY.md` for the full threat model.

---

## Contributing

PRs welcome. Please:

1. Keep dependencies = stdlib only. If you need a library, open a discussion first.
2. Maintain Python 3.11+ compatibility.
3. Update README and CHANGELOG for user-visible changes.
4. New shell features must support both `strict` and `open` sandbox modes.

See `CONTRIBUTING.md` for full guidelines.

---

## License

MIT. See `LICENSE`.

---

## Acknowledgments

- [Claude Code](https://github.com/anthropics/claude-code) by Anthropic — the CLI this bot wraps.
- [llama.cpp](https://github.com/ggerganov/llama.cpp) for the local inference layer.
- The Telegram Bot API team for the plain-HTTP interface that makes stdlib-only possible.

---

## Status

Active development. Production-tested by the maintainer in WSL2 Ubuntu on AMD Ryzen 9 + RTX 5090 with Qwen3.6-35B-A3B local. Not affiliated with Anthropic.
