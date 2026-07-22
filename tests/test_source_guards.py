from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_no_proxy_architecture():
    assert not (ROOT / "adapter_proxy.py").exists()
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "cloky").glob("*.py"))
    assert "127.0.0.1:8081" not in text


def test_env_is_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignore


def test_no_recursive_json_walker_in_runtime():
    text = (ROOT / "cloky" / "runtime.py").read_text(encoding="utf-8")
    assert "for v in node.values()" not in text
    assert "walk(" not in text
