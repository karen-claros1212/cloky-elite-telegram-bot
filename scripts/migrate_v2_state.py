from __future__ import annotations

import json
import sys
from pathlib import Path

from cloky.config import Config
from cloky.state import StateStore


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def main() -> None:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "cloky-elite-telegram-bot").resolve()
    config = Config.load(base)
    store = StateStore(config.db_path, config.default_project, config.default_mode, config.claude_model)
    native = read_json(base / "state" / "native_sessions.json")
    modes = read_json(base / "state" / "user_modes.json")
    migrated = 0

    users = set(config.allowed_user_ids)
    users.update(int(key) for key in native if str(key).isdigit())
    users.update(int(key) for key in modes if str(key).isdigit())

    for user_id in users:
        state = store.get_user(user_id)
        user_native = native.get(str(user_id), {})
        if isinstance(user_native, dict):
            project_key = str(config.default_project.resolve())
            session_id = user_native.get(project_key)
            if not session_id and user_native:
                project_key, session_id = next(iter(user_native.items()))
            if isinstance(session_id, str) and session_id:
                state.project_path = str(Path(project_key).expanduser().resolve())
                state.session_id = session_id
        user_mode = modes.get(str(user_id), {})
        if isinstance(user_mode, dict):
            mode = user_mode.get("mode")
            if mode in {"default", "dontAsk", "acceptEdits", "bypassPermissions", "plan"}:
                state.mode = mode
        store.save_user(state)
        migrated += 1

    store.audit("migration_v2", migrated_users=migrated)
    store.close()
    print(f"MIGRATION_OK users={migrated} db={config.db_path}")


if __name__ == "__main__":
    main()
