from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_MODES = {"default", "dontAsk", "acceptEdits", "bypassPermissions", "plan"}


@dataclass(slots=True)
class UserState:
    user_id: int
    project_path: str
    session_id: str | None = None
    mode: str = "bypassPermissions"
    model: str | None = None
    fork_next: bool = False
    updated_at: float = 0.0

    @property
    def project(self) -> Path:
        return Path(self.project_path)


@dataclass(slots=True)
class TaskTelemetry:
    session_id: str | None = None
    subtype: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    context_window: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    model_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskResult:
    text: str
    telemetry: TaskTelemetry = field(default_factory=TaskTelemetry)
    sentinel_detected: bool = False
    error: str | None = None


@dataclass(slots=True)
class RuntimeStatus:
    user_id: int
    project_path: str
    session_id: str | None
    mode: str
    model: str | None
    busy: bool
    status: str
    started_at: float | None = None
