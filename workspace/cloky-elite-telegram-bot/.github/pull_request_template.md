# Description

What does this PR do? Why?

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change (would require version bump and migration notes)
- [ ] Documentation update
- [ ] Refactor (no functional change)
- [ ] Security fix

## Checklist

- [ ] `python3 -m py_compile bot.py` passes
- [ ] `bash -n install_service.sh` passes
- [ ] No new dependencies on third-party Python packages
- [ ] Backward compatible with v1.5+ commands (or migration documented)
- [ ] New env vars documented in `.env.example`
- [ ] New commands documented in `/help` and `README.md`
- [ ] If shell changes: tested in both `strict` and `open` sandbox modes
- [ ] No reduction in defense layers (see `SECURITY.md`)
- [ ] `CHANGELOG.md` updated under the appropriate version section
- [ ] Commit messages follow `vN.N.N: short description` format where applicable

## Testing performed

How did you verify this works?

## Security review

If this PR touches any of these, justify the change:

- [ ] systemd unit
- [ ] Subprocess env vars
- [ ] User input handling before sanitization
- [ ] Writes outside `state/`, `logs/`, or `workspace/`
- [ ] PATH modifications
- [ ] Token redaction patterns

## Screenshots / logs (optional)
