from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from .security import clean_visible_text


@dataclass(slots=True)
class ResponseAccumulator:
    partial_text: str = ""
    assistant_segments: list[str] = field(default_factory=list)
    result_text: str = ""
    sentinel_detected: bool = False

    def add_delta(self, text: str | None) -> None:
        if text is None:
            return
        if text.strip().lower() in {"no response requested.", "no response requested"}:
            self.sentinel_detected = True
            return
        # Deltas are fragments. Do not strip or add separators.
        self.partial_text += text

    def add_assistant(self, text: str | None) -> None:
        clean = clean_visible_text(text)
        if clean and clean not in self.assistant_segments:
            self.assistant_segments.append(clean)

    def set_result(self, text: str | None) -> None:
        clean = clean_visible_text(text)
        if clean:
            self.result_text = clean
        elif text and text.strip().lower() in {"no response requested.", "no response requested"}:
            self.sentinel_detected = True

    def final_text(self) -> str:
        if self.result_text:
            return self.result_text
        if self.assistant_segments:
            return "\n\n".join(self.assistant_segments)
        clean = clean_visible_text(self.partial_text)
        return clean or ""

    def preview(self, limit: int = 3500) -> str:
        value = self.partial_text or (self.assistant_segments[-1] if self.assistant_segments else "")
        clean = clean_visible_text(value) or ""
        if len(clean) > limit:
            return "…" + clean[-limit:]
        return clean


def split_telegram(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if not text:
        return ["Sin contenido visible."]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)
