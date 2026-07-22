from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import VALID_MODES


def load_dotenv(path: Path) -> None:
    """Small, deterministic .env loader. Existing environment wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _parse_allowed_projects(raw: str, default_path: Path) -> dict[str, Path]:
    projects: dict[str, Path] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, value = item.split(":", 1)
        else:
            value = item
            name = Path(value).name or "project"
        path = Path(value).expanduser().resolve()
        projects[name.strip() or path.name] = path
    if not projects:
        projects[default_path.name or "workspace"] = default_path.resolve()
    return projects


@dataclass(slots=True)
class Config:
    base_dir: Path
    telegram_token: str
    allowed_user_ids: set[int]
    projects: dict[str, Path]
    default_project: Path
    db_path: Path
    log_dir: Path
    uploads_dir: Path
    claude_cli_path: str
    claude_model: str | None
    default_mode: str
    anthropic_base_url: str
    anthropic_auth_token: str
    anthropic_api_key: str
    anthropic_model: str
    max_turns: int = 50
    api_timeout_ms: int = 600_000
    max_retries: int = 2
    stream_idle_timeout_ms: int = 300_000
    approval_timeout_seconds: int = 900
    idle_client_ttl_seconds: int = 1800
    stream_edit_interval: float = 1.5
    session_list_limit: int = 10
    max_upload_bytes: int = 50 * 1024 * 1024
    include_hook_events: bool = True
    enable_file_checkpointing: bool = True
    disable_experimental_betas: bool = True
    disallowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, base_dir: Path | None = None) -> "Config":
        base = (base_dir or Path(os.environ.get("BOT_HOME", Path.home() / "cloky-elite-telegram-bot"))).expanduser().resolve()
        load_dotenv(base / ".env")

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_raw = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
        allowed: set[int] = set()
        for item in allowed_raw.split(","):
            item = item.strip()
            if item.isdigit():
                allowed.add(int(item))

        workspace = Path(os.environ.get("CLAUDE_WORKSPACE", base / "workspace")).expanduser().resolve()
        projects = _parse_allowed_projects(os.environ.get("ALLOWED_PROJECTS", ""), workspace)
        default_name = os.environ.get("DEFAULT_PROJECT", "").strip()
        default_project = projects.get(default_name) if default_name else next(iter(projects.values()))
        if default_project is None:
            default_project = workspace

        mode = os.environ.get("CLAUDE_DEFAULT_PERMISSION_MODE", "bypassPermissions").strip()
        if mode not in VALID_MODES:
            mode = "bypassPermissions"

        deny = [
            "Bash(sudo *)",
            "Bash(su *)",
            "Bash(rm -rf /)",
            "Bash(rm -rf /*)",
            "Bash(mkfs*)",
            "Bash(dd if=*)",
            "Write(/etc/*)",
            "Edit(/etc/*)",
            "Write(/usr/*)",
            "Edit(/usr/*)",
        ]
        extra_deny = os.environ.get("CLAUDE_DISALLOWED_TOOLS", "")
        deny.extend(x.strip() for x in extra_deny.split(",") if x.strip())

        config = cls(
            base_dir=base,
            telegram_token=token,
            allowed_user_ids=allowed,
            projects=projects,
            default_project=default_project,
            db_path=Path(os.environ.get("CLOKY_DB_PATH", base / "state" / "cloky.sqlite3")).expanduser().resolve(),
            log_dir=Path(os.environ.get("CLOKY_LOG_DIR", base / "logs")).expanduser().resolve(),
            uploads_dir=Path(os.environ.get("CLOKY_UPLOADS_DIR", base / "workspace" / "uploads")).expanduser().resolve(),
            claude_cli_path=os.environ.get("CLAUDE_BIN", "claude").strip() or "claude",
            claude_model=(os.environ.get("CLAUDE_MODEL") or "sonnet").strip() or None,
            default_mode=mode,
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
            anthropic_auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip(),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", "sonnet").strip() or "sonnet",
            max_turns=_int("CLAUDE_MAX_TURNS", 50),
            api_timeout_ms=_int("API_TIMEOUT_MS", 600_000),
            max_retries=_int("CLAUDE_CODE_MAX_RETRIES", 2),
            stream_idle_timeout_ms=_int("CLAUDE_STREAM_IDLE_TIMEOUT_MS", 300_000),
            approval_timeout_seconds=_int("APPROVAL_TIMEOUT_SECONDS", 900),
            idle_client_ttl_seconds=_int("IDLE_CLIENT_TTL_SECONDS", 1800),
            stream_edit_interval=_float("STREAM_EDIT_INTERVAL", 1.5),
            session_list_limit=_int("SESSIONS_LIST_LIMIT", 10),
            max_upload_bytes=_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024),
            include_hook_events=_bool("CLAUDE_INCLUDE_HOOK_EVENTS", True),
            enable_file_checkpointing=_bool("CLAUDE_ENABLE_FILE_CHECKPOINTING", True),
            disable_experimental_betas=_bool("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", True),
            disallowed_tools=deny,
        )
        config.ensure_directories()
        return config

    def ensure_directories(self) -> None:
        for path in (self.base_dir, self.db_path.parent, self.log_dir, self.uploads_dir):
            path.mkdir(parents=True, exist_ok=True)
        # Create the default workspace only when it belongs to BOT_HOME. External
        # project paths are configuration and must already exist; typos must fail.
        try:
            if self.default_project == self.base_dir or self.base_dir in self.default_project.parents:
                self.default_project.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.telegram_token:
            errors.append("TELEGRAM_BOT_TOKEN no configurado")
        if not self.allowed_user_ids:
            errors.append("ALLOWED_TELEGRAM_USER_ID no configurado")
        for name, path in self.projects.items():
            if not path.exists():
                errors.append(f"Proyecto {name!r} no existe: {path}")
        if self.default_mode not in VALID_MODES:
            errors.append(f"Modo inválido: {self.default_mode}")
        return errors

    def claude_env(self) -> dict[str, str]:
        env = {
            "ANTHROPIC_BASE_URL": self.anthropic_base_url,
            "ANTHROPIC_MODEL": self.anthropic_model,
            "API_TIMEOUT_MS": str(self.api_timeout_ms),
            "CLAUDE_CODE_MAX_RETRIES": str(self.max_retries),
            "CLAUDE_ENABLE_STREAM_WATCHDOG": "1",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(self.stream_idle_timeout_ms),
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
        if self.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_auth_token
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        if self.disable_experimental_betas:
            env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
        return env
