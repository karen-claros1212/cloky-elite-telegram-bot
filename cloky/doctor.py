from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config


def _http(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: int = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "body": body[:1000]}
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "body": body[:1000]}
    except Exception as exc:
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}


def run_checks(config: Config, full: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    errors = config.validate()
    checks["config"] = {"ok": not errors, "errors": errors}

    try:
        import claude_agent_sdk  # type: ignore

        checks["agent_sdk"] = {"ok": True, "version": getattr(claude_agent_sdk, "__version__", "unknown")}
    except Exception as exc:
        checks["agent_sdk"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    cli = shutil.which(config.claude_cli_path) or (config.claude_cli_path if Path(config.claude_cli_path).exists() else None)
    if cli:
        try:
            result = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=15)
            checks["claude_cli"] = {
                "ok": result.returncode == 0,
                "path": cli,
                "version": (result.stdout or result.stderr).strip()[:200],
                "return_code": result.returncode,
            }
        except Exception as exc:
            checks["claude_cli"] = {"ok": False, "path": cli, "error": f"{type(exc).__name__}: {exc}"}
    else:
        checks["claude_cli"] = {"ok": False, "error": "Claude Code CLI no encontrado"}

    auth_headers: dict[str, str] = {}
    if config.anthropic_auth_token:
        auth_headers["Authorization"] = f"Bearer {config.anthropic_auth_token}"
    elif config.anthropic_api_key:
        auth_headers["x-api-key"] = config.anthropic_api_key

    checks["gateway_health"] = _http(f"{config.anthropic_base_url}/health", headers=auth_headers, timeout=10)
    checks["gateway_health"].pop("body", None) if checks["gateway_health"].get("ok") else None

    if full:
        payload = json.dumps(
            {
                "model": config.anthropic_model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Respond only OK"}],
            }
        ).encode("utf-8")
        headers = {**auth_headers, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        checks["gateway_messages"] = _http(
            f"{config.anthropic_base_url}/v1/messages",
            headers=headers,
            data=payload,
            timeout=60,
        )

    checks["database"] = {"ok": config.db_path.parent.exists(), "path": str(config.db_path)}
    checks["overall_ok"] = all(v.get("ok", False) for k, v in checks.items() if isinstance(v, dict) and k != "gateway_messages")
    return checks


def format_checks(checks: dict[str, Any]) -> str:
    lines = ["Diagnóstico Cloky"]
    for name, result in checks.items():
        if name == "overall_ok" or not isinstance(result, dict):
            continue
        mark = "OK" if result.get("ok") else "FAIL"
        detail = result.get("version") or result.get("status") or result.get("error") or ""
        lines.append(f"{mark} {name}: {detail}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--full", action="store_true", help="Hace una inferencia mínima contra /v1/messages")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.base_dir)
    checks = run_checks(config, full=args.full)
    if args.json:
        print(json.dumps(checks, indent=2, ensure_ascii=False))
    else:
        print(format_checks(checks))
    raise SystemExit(0 if checks.get("overall_ok") else 1)


if __name__ == "__main__":
    main()
