from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SENTINELS = {
    "no response requested.",
    "no response requested",
}
_INTERNAL_PREFIXES = (
    "[your previous response had no visible output",
    "chatcmpl-",
)
_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
]


def is_sentinel(text: str | None) -> bool:
    return bool(text and text.strip().lower() in _SENTINELS)


def is_internal_text(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _INTERNAL_PREFIXES)


def redact(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", out)
    return out


def clean_visible_text(text: str | None) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.replace("\x00", "").strip()
    if not value or is_sentinel(value) or is_internal_text(value):
        return None
    replacement_ratio = value.count("\ufffd") / max(1, len(value))
    if replacement_ratio > 0.01:
        return None
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in value)
    if printable / max(1, len(value)) < 0.92:
        return None
    return redact(value)


def summarize_tool_input(tool_name: str, input_data: dict[str, Any], limit: int = 900) -> str:
    if tool_name == "Bash":
        command = str(input_data.get("command", ""))
        description = str(input_data.get("description", ""))
        value = f"Comando: {command}"
        if description:
            value += f"\nDescripción: {description}"
    elif tool_name in {"Write", "Edit", "Read", "Glob", "Grep"}:
        fields = []
        for key in ("file_path", "path", "pattern", "glob", "description"):
            if key in input_data:
                fields.append(f"{key}: {input_data[key]}")
        value = "\n".join(fields) or json.dumps(input_data, ensure_ascii=False, default=str)
    else:
        value = json.dumps(input_data, ensure_ascii=False, default=str)
    return redact(value[:limit])


def ensure_allowed_path(path: Path, allowed_roots: list[Path]) -> Path:
    resolved = path.expanduser().resolve()
    for root in allowed_roots:
        root_resolved = root.expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise PermissionError(f"Ruta fuera de los proyectos permitidos: {resolved}")
