# Contributing to Cloky Elite Telegram Bot

Thank you for considering a contribution. This project values **simplicity, security, and minimalism** above feature count. Please read this guide before opening a PR.

## Project principles

1. **Stdlib only.** No `pip install` should be required to run the bot. If a feature requires a third-party library, open a discussion issue first to evaluate alternatives.
2. **Single file.** `bot.py` is one file on purpose. It's easier to audit, ship, and reason about than 30 files behind a framework.
3. **Defense in depth.** Every input goes through sanitization, every output through redaction, every subprocess through the sandbox. Don't bypass these layers without justification.
4. **Local-first.** Cloky is for local LLM backends. Features should make sense for self-hosted use, not assume hosted APIs.
5. **Production over polish.** Robustness (file locking, orphan recovery, graceful shutdown) beats convenience features.

## Development setup

```bash
git clone https://github.com/YOUR_USER/cloky-elite-telegram-bot.git
cd cloky-elite-telegram-bot

# No virtualenv needed — stdlib only.
python3 -m py_compile bot.py
bash -n install_service.sh
```

Run locally without systemd:

```bash
export BOT_HOME=$(pwd)
export TELEGRAM_BOT_TOKEN="..."
export ALLOWED_TELEGRAM_USER_ID="..."
export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"
python3 bot.py
```

## Pull request checklist

Before opening a PR:

- [ ] `python3 -m py_compile bot.py` passes
- [ ] `bash -n install_service.sh` passes
- [ ] `python3 -c "import json; json.load(open('claude-settings.json'))"` passes
- [ ] No new dependencies on third-party Python packages
- [ ] Backward compatibility with v1.5+ commands maintained
- [ ] New env vars documented in `.env.example` with comments
- [ ] New commands documented in `/help` output and `README.md`
- [ ] New shell features tested in BOTH `strict` and `open` sandbox modes
- [ ] No reduction in defense layers (see "Security review" below)
- [ ] `CHANGELOG.md` updated
- [ ] Commit messages follow the format `vN.N.N: short description`

## Security review

Changes that affect any of these require explicit security justification in the PR description:

- Anything that modifies the systemd unit
- Anything that adds env vars passed to subprocesses
- Anything that handles user input from Telegram before sanitization
- Anything that writes outside `state/`, `logs/`, or `workspace/`
- Anything that adds executables to PATH
- Anything that modifies token redaction patterns

## What we won't accept

- Web dashboards, GUI tools, or anything that ships an HTTP server
- Migration to async/asyncio (the current threading model works for this use case)
- Migration to `python-telegram-bot` or `aiogram` (we are stdlib-only)
- SQL databases (JSON files with `fcntl.flock` are sufficient at this scale)
- Cost tracking against Anthropic API (this project is local-first)
- Anything that introduces a build step or compilation requirement
- Anything that adds CI-only features without making them optional

## Reporting bugs

Open an issue with:

- Cloky version (`/version` output)
- systemd version (`systemctl --version`)
- Python version
- Sandbox mode (`strict` or `open`)
- Backend (llama.cpp / vLLM / Ollama / other)
- Full reproduction steps
- Relevant logs (with tokens redacted — Cloky redacts them automatically, but double-check)

## Reporting security issues

**Do not open public issues for security vulnerabilities.**

Email the maintainer privately, or open a GitHub security advisory if available.

Include:

- Affected version(s)
- Attack vector and prerequisites
- Reproduction steps
- Suggested mitigation if you have one

## Code style

- Follow PEP 8 with reasonable line length (we use ~100 chars).
- Type hints on function signatures.
- Docstrings on public functions, especially anything that touches subprocess, fs, or Telegram.
- Comments in Spanish or English are both fine; the existing code mixes both.
- Magic numbers should be named constants at the top of `bot.py`.

## License

By contributing, you agree your contributions are licensed under MIT.
