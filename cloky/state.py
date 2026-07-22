from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import UserState


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    project_path TEXT NOT NULL,
    session_id TEXT,
    mode TEXT NOT NULL,
    model TEXT,
    fork_next INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    session_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    user_id INTEGER,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_user_started ON task_log(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
"""


class StateStore:
    def __init__(self, path: Path, default_project: Path, default_mode: str, default_model: str | None):
        self.path = path
        self.default_project = str(default_project.resolve())
        self.default_mode = default_mode
        self.default_model = default_model
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_user(self, user_id: int) -> UserState:
        with self._lock:
            row = self._conn.execute("SELECT * FROM user_state WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                state = UserState(
                    user_id=user_id,
                    project_path=self.default_project,
                    session_id=None,
                    mode=self.default_mode,
                    model=self.default_model,
                    fork_next=False,
                    updated_at=time.time(),
                )
                self.save_user(state)
                return state
            return UserState(
                user_id=int(row["user_id"]),
                project_path=str(row["project_path"]),
                session_id=row["session_id"],
                mode=str(row["mode"]),
                model=row["model"],
                fork_next=bool(row["fork_next"]),
                updated_at=float(row["updated_at"]),
            )

    def save_user(self, state: UserState) -> None:
        state.updated_at = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_state(user_id, project_path, session_id, mode, model, fork_next, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  project_path=excluded.project_path,
                  session_id=excluded.session_id,
                  mode=excluded.mode,
                  model=excluded.model,
                  fork_next=excluded.fork_next,
                  updated_at=excluded.updated_at
                """,
                (
                    state.user_id,
                    state.project_path,
                    state.session_id,
                    state.mode,
                    state.model,
                    1 if state.fork_next else 0,
                    state.updated_at,
                ),
            )
            self._conn.commit()

    def update_user(self, user_id: int, **changes: Any) -> UserState:
        state = self.get_user(user_id)
        for key, value in changes.items():
            if not hasattr(state, key):
                raise AttributeError(key)
            setattr(state, key, value)
        self.save_user(state)
        return state

    def start_task(self, user_id: int, project_path: str, session_id: str | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO task_log(user_id,project_path,session_id,started_at,status) VALUES(?,?,?,?,?)",
                (user_id, project_path, session_id, time.time(), "running"),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        session_id: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_log SET finished_at=?, status=?, session_id=?, input_tokens=?, output_tokens=?, cost_usd=?, error=?
                WHERE id=?
                """,
                (time.time(), status, session_id, input_tokens, output_tokens, cost_usd, error, task_id),
            )
            self._conn.commit()

    def last_task(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_log WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def audit(self, event: str, *, user_id: int | None = None, **details: Any) -> None:
        safe = json.dumps(details, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(created_at,user_id,event,details_json) VALUES(?,?,?,?)",
                (time.time(), user_id, event, safe),
            )
            self._conn.commit()

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
            return [dict(row) for row in rows]
