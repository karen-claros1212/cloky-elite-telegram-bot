from pathlib import Path

import pytest

from cloky.security import clean_visible_text, ensure_allowed_path, is_internal_text, is_sentinel, redact, summarize_tool_input


def test_sentinel():
    assert is_sentinel("No response requested.")
    assert clean_visible_text("No response requested.") is None


def test_internal_prompt_hidden():
    value = "[Your previous response had no visible output. Please continue.]"
    assert is_internal_text(value)
    assert clean_visible_text(value) is None


def test_garbage_hidden():
    assert clean_visible_text("�" * 100 + "abc") is None


def test_redacts_token():
    secret = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    value = redact("token " + secret)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in value
    assert "[REDACTED]" in value


def test_tool_summary_does_not_dump_huge_input():
    value = summarize_tool_input("Bash", {"command": "echo hello", "description": "x" * 2000})
    assert len(value) <= 900
    assert "echo hello" in value


def test_allowed_path(tmp_path: Path):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    assert ensure_allowed_path(child, [root]) == child.resolve()
    with pytest.raises(PermissionError):
        ensure_allowed_path(tmp_path / "outside", [root])
