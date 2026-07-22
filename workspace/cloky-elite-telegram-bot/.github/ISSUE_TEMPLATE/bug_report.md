---
name: Bug report
about: Report a problem with Cloky
title: "[BUG] "
labels: bug
assignees: ''
---

## Environment

- Cloky version (output of `/version` in Telegram):
- systemd version (`systemctl --version | head -1`):
- Python version (`python3 --version`):
- OS (`uname -a` or `/etc/os-release`):
- Sandbox mode (`strict` or `open`):
- Backend (llama.cpp / vLLM / Ollama / other), and version:
- Model name (string passed in `ANTHROPIC_DEFAULT_SONNET_MODEL`):

## What happened

A clear description of the problem.

## Expected behavior

What you expected to happen.

## Steps to reproduce

1. ...
2. ...
3. ...

## Relevant logs

Paste `journalctl --user -u cloky-elite-telegram-bot.service -n 100 --no-pager` here.

**Cloky redacts tokens automatically**, but verify nothing sensitive is leaking before pasting.

```
<paste logs here>
```

## Output of `/config` and `/health`

```
<paste here>
```

## Additional context

Anything else relevant (custom env vars, other services running, recent changes).
