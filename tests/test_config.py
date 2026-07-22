from pathlib import Path

from cloky.config import Config, _parse_allowed_projects


def test_parse_projects(tmp_path: Path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    result = _parse_allowed_projects(f"api:{one},web:{two}", tmp_path)
    assert result["api"] == one.resolve()
    assert result["web"] == two.resolve()


def test_config_load_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BOT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "123,456")
    monkeypatch.setenv("CLAUDE_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("CLAUDE_DEFAULT_PERMISSION_MODE", "plan")
    cfg = Config.load()
    assert cfg.allowed_user_ids == {123, 456}
    assert cfg.default_mode == "plan"
    assert cfg.anthropic_base_url == "http://127.0.0.1:8080"
    assert cfg.validate() == []


def test_invalid_mode_falls_back(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BOT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "123")
    monkeypatch.setenv("CLAUDE_DEFAULT_PERMISSION_MODE", "invalid")
    cfg = Config.load()
    assert cfg.default_mode == "bypassPermissions"
