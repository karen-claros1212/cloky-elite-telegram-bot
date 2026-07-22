# Security Policy

## Threat model

Cloky bridges Telegram to a Claude Code CLI subprocess that has the autonomy to execute shell commands, edit files, and call HTTP backends. This is **inherently powerful**. The security model assumes:

1. **The Telegram user is trusted.** The allowlist (`ALLOWED_TELEGRAM_USER_ID`) is the first gate. If your Telegram session is compromised, the attacker can issue any command the bot would accept.
2. **The local backend (llama.cpp, etc.) is trusted.** The bot is a strict HTTP client and does not validate the model's outputs beyond the redaction layer. A compromised or malicious model can return prompts intended to manipulate behavior; the bot mitigates with anti-injection sanitization but does not eliminate the risk.
3. **The host system is trusted up to root.** The sandbox limits what the bot can do, but the bot runs as your user. Anything that user can do, the bot can eventually do unless explicitly blocked.

## Defense layers (always active)

Even in `open` sandbox mode, these remain:

| Layer | Mechanism |
|-------|-----------|
| Access | Telegram user ID allowlist |
| Input | Anti-prompt-injection tag neutralization |
| Pre-spawn | Regex block of 11 destructive command patterns |
| Privileges | `NoNewPrivileges=true` |
| Capabilities | `CapabilityBoundingSet=` empty |
| Syscalls | Restrictive `SystemCallFilter` |
| Namespaces | `RestrictNamespaces=true` |
| Network | `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` |
| Personality | `LockPersonality=true`, `RestrictSUIDSGID=true` |
| Logs | Token redaction (11 regex patterns) |
| Persistence | `fcntl.flock` on sessions and stats |
| Recovery | UID-verified orphan PID cleanup |

## Defense layers (strict mode only)

Add these when running `BOT_SANDBOX_MODE=strict`:

- `ProtectHome=tmpfs` — entire `$HOME` is hidden from the service
- `BindPaths` only for workspace and `~/.claude`
- Projects in `ALLOWED_PROJECTS` exposed individually via `BindPaths`

## Known limitations

1. **Prompt injection through the model's own output.** If a tool result contains crafted text, that text enters the session history and may influence future turns. We neutralize structural tags (`[system]`, `[user]`) but cannot prevent semantic manipulation. Mitigation: review session history with `/clear` periodically; limit `MAX_CONTEXT_MESSAGES`.
2. **Bypass of pre-spawn block via obfuscation.** A creative phrasing could ask the model to perform a destructive action without the literal command text appearing in user input. The systemd sandbox is the real defense; the pre-spawn block is a tripwire for obvious cases.
3. **Symlink attacks.** If a project path in `ALLOWED_PROJECTS` is a symlink to a sensitive location, the BindPaths follows it. We do not resolve symlinks at install time.
4. **Disk usage from uploads.** `MAX_UPLOAD_BYTES` caps a single file, but there is no quota on total `workspace/uploads/` size. Configure `MemoryMax` and disk quotas externally if needed.
5. **No rate limit at the bot level.** Telegram's per-bot rate limits apply to outbound, but inbound has no application-level throttle beyond the per-user task lock. A malicious approved user can queue many tasks.
6. **Streaming edits and message history.** Telegram retains edit history. Sensitive content shown during streaming remains visible in the chat even if redacted in the final message.

## Reporting a vulnerability

**Do not open public issues for security vulnerabilities.**

Use one of:

1. GitHub Security Advisory (if the repository is public): https://github.com/YOUR_USER/cloky-elite-telegram-bot/security/advisories/new
2. Email: maintainer's email (set in repo metadata)

Please include:

- Affected version(s) — `/version` output
- Attack vector and any prerequisites
- Steps to reproduce
- Impact assessment
- Suggested mitigation if available

We aim to respond within 7 days for critical issues.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.6.x   | ✓ (latest) |
| 1.5.x   | ✓ security fixes only |
| 1.4.x   | ✓ security fixes only |
| < 1.4   | ✗ |

Upgrade to 1.6+ for the full hardening profile.

## Recommended deployment

For maximum security:

1. Run in `strict` sandbox mode.
2. Keep `ALLOWED_TELEGRAM_USER_ID` minimal (your user only).
3. Use a unique `ANTHROPIC_AUTH_TOKEN` value (not reused elsewhere).
4. Set `chmod 600 .env` (the install script does this automatically).
5. Avoid putting sensitive paths in `ALLOWED_PROJECTS`.
6. Review `/stats` and `journalctl` periodically for unexpected activity.
7. Bind your local backend (llama.cpp, vLLM) to `127.0.0.1` only — never `0.0.0.0`.
8. Do not expose port 8080 (or whatever your backend uses) to LAN.

## Disclosures

None to date.
