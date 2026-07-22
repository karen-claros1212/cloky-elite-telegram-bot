# Changelog

All notable changes to this project will be documented in this file.

## [1.6.0] - 2026-05-11

### Added — Sprint 2

- **Streaming incremental to Telegram** (S2.1). The bot edits the message progressively as Claude generates output, with rate limiting (1.5s default) to avoid Telegram 429 errors. When content exceeds 3500 chars, the message is finalized and a new one is opened.
- **File uploads** (S2.2). Send photos, documents, audio, voice notes, or video to the bot. Files land in `workspace/uploads/<timestamp>_<safename>` and the absolute path is injected into the prompt if a caption is provided. Hard cap via `MAX_UPLOAD_BYTES` (default 50 MB).
- **Multi-project with `/cd <name>`** (S2.3). Whitelist projects in `ALLOWED_PROJECTS` env var (`"name1:/path1,name2:/path2"`). Per-user cwd state. In strict sandbox mode, `install_service.sh` auto-exposes the listed paths via additional `BindPaths`. New commands: `/cd`, `/projects`, `/pwd`.
- New env vars: `STREAM_ENABLED`, `STREAM_EDIT_INTERVAL`, `STREAM_MIN_CHUNK_LEN`, `MAX_UPLOAD_BYTES`, `ALLOWED_PROJECTS`.

### Changed

- `/help` reorganized into Operación / Workspace / Diagnóstico / Archivos sections.
- `/config` now reports streaming, uploads dir, project count, current cwd.
- `run_claude_task` uses `cwd = get_user_cwd(user_id)` instead of hardcoded `WORKSPACE_DIR`.
- `handle_update` detects and processes file attachments before falling through to text handling.

### Documentation

- Marketing-quality README with comparison table to similar projects.
- LICENSE (MIT), CONTRIBUTING.md, SECURITY.md added for GitHub publication.

## [1.5.0] - 2026-05-10

### Added

- **Sandbox systemd configurable** via `BOT_SANDBOX_MODE` env var. Modes: `strict` (default, `ProtectHome=tmpfs`) and `open` (full `$HOME` access).
- Mode persists in `~/cloky-elite-telegram-bot/.sandbox_mode` between reinstalls.
- `/version`, `/config`, `/help` now display active sandbox mode.
- Warning log at startup when mode is `open`.

### Changed

- `install_service.sh` generates the unit file conditionally based on chosen mode.
- All non-filesystem defenses (`NoNewPrivileges`, `CapabilityBoundingSet`, `SystemCallFilter`, etc.) remain active in both modes.

## [1.4.0] - 2026-05-10

### Added — Sprint 1

- **`CLAUDE.md` auto-generated** in workspace on first start (S1.1).
- **`/cancel`** as soft cancellation via SIGINT, distinct from `/stop` (SIGTERM → SIGKILL escalation in 3s) (S1.2).
- **Long-task notification** with sound for tasks >120s (S1.3).
- **Orphan PID recovery** at startup: `state/running.json` tracks active PIDs, killed at restart with UID verification (S1.4).
- **Log rotation** internal (10MB × 3 files) (S1.5).
- **File locking** with `fcntl.flock` on `sessions.json` and `stats.json` (S1.6).
- **Subprocess cleanup** robust against `TimeoutExpired` (S1.7).
- **Thread crash handler** logs exceptions in reader threads (S1.8).
- New commands: `/version`, `/uptime`, `/tasks`, `/stats` (S1.9-S1.10).
- **Graceful shutdown** on SIGTERM, cleans up active tasks before exit (S1.11).
- **Pre-flight checks** at startup: Claude CLI available, backend reachable, workspace writable (S1.12-S1.13).
- `KillSignal=SIGTERM`, `TimeoutStopSec=30`, `KillMode=mixed` in systemd unit.

## [1.3.0] - 2026-05-10

### Changed

- Token defaults aligned to official Claude Code 2.x documentation:
  - `CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000` (was 8192)
  - `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=8` (was 4)
  - `CLAUDE_TIMEOUT_SECONDS=1800` (was 900)
- Added `BASH_DEFAULT_TIMEOUT_MS=600000`, `BASH_MAX_TIMEOUT_MS=1800000`, `BASH_MAX_OUTPUT_LENGTH=50000`.
- Added `MAX_THINKING_TOKENS=31999`, `MCP_TIMEOUT`, `MCP_TOOL_TIMEOUT`, `MAX_MCP_OUTPUT_TOKENS`.
- `MAX_CONTEXT_MESSAGES` raised to 12.
- All these now propagated to the Claude Code subprocess environment.
- Optional `~/.claude/settings.json` shipped (`claude-settings.json` in repo).

## [1.2.0] - 2026-05-10

### Added

- **Autonomous mode** via `--dangerously-skip-permissions` (`CLAUDE_AUTO_APPROVE=true` default). Claude executes tools without prompting.
- **Pre-spawn destructive command block**: 11 regex patterns catch `rm -rf /`, `mkfs.*`, `dd of=/dev/sd*`, fork bombs, `shutdown`, `chmod -R 777 /`, etc.
- `~/.claude` exposed as writable `BindPaths` in strict mode (was read-only, broke autonomy).

### Changed

- `install_service.sh` adds `BindPaths` for `~/.config/claude-code` and `~/.cache/claude-code` (Claude Code 2.x paths).

## [1.1.0] - 2026-05-10

### Added — 6 hardening patches

- **P1**: Token redaction in `fallback_from_outputs` stdout/stderr. Extended `TOKEN_PATTERNS` (Telegram, OpenAI sk-, AWS AKIA, GitHub ghp_, HF hf_, Slack xoxb-, Bearer, ANTHROPIC_*, TELEGRAM_BOT_TOKEN).
- **P2**: Anti-prompt-injection — `INJECTION_TAG_RE` neutralizes `[system]`, `[user]`, `[assistant]` tags in user input and stored history.
- **P3**: Atomic slot reservation in `running_tasks` with `"PENDING"` marker to prevent race condition between check and Popen.
- **P4**: Deduplication of `visible_parts` by SHA256 hash to avoid duplicate text from `walk()` recursion.
- **P5**: `/health` end-to-end probe (GET `/health` + POST `/v1/messages` with `max_tokens=1`).
- **P6**: systemd unit with `ProtectHome=tmpfs`, scoped `BindPaths`, `CapabilityBoundingSet=` empty, `SystemCallFilter`, `RestrictAddressFamilies`, `NoNewPrivileges`, full hardening profile.

## [1.0.0] - 2026-05-09

### Added — Initial release

- Telegram bridge to Claude Code CLI via subprocess.
- Stream-json output parsing.
- Allowlist by user ID.
- Per-user session lock and task management.
- `--print --output-format stream-json --verbose` command construction.
- systemd `--user` service via `install_service.sh`.
- `.env` config loading.
- Stdlib-only Python (no pip install).
- Basic commands: `/start`, `/help`, `/status`, `/stop`, `/clear`, `/cwd`, `/health`.
