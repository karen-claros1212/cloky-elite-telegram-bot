#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-$HOME/cloky-elite-telegram-bot}"
cd "$REPO"
[[ -d .git ]] || { echo "ERROR: $REPO no es un repo Git"; exit 2; }
[[ -f .env ]] || { echo "ERROR: falta .env local"; exit 3; }
ORIGIN="$(git remote get-url origin)"
TMP_ENV="$(mktemp /tmp/cloky-env.XXXXXX)"
cp .env "$TMP_ENV"
chmod 600 "$TMP_ENV"
trap 'rm -f "$TMP_ENV"' EXIT
printf '\n.env\n.env.*\n!.env.example\n' >> .gitignore
sort -u .gitignore -o .gitignore
git add -A
git rm --cached .env 2>/dev/null || true
git commit -m "chore: publish sanitized Cloky source" || true
command -v git-filter-repo >/dev/null || python3 -m pip install --user git-filter-repo
git filter-repo --path .env --invert-paths --force
cp "$TMP_ENV" .env
chmod 600 .env
git remote remove origin 2>/dev/null || true
git remote add origin "$ORIGIN"
git push --force --all origin
git push --force --tags origin
git ls-files --error-unmatch .env >/dev/null 2>&1 && { echo "FAIL: .env sigue tracked"; exit 4; } || true
echo "CLEAN_OK: .env local conservado y eliminado del historial remoto"
