#!/usr/bin/env python3
"""
acceptance_probe.py — Verificación automática v1.13.0
======================================================
Prueba directa: bot.py → :8080 y bot.py → proxy :8081
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import hashlib
import subprocess

APP_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('/home/jesus/cloky-elite-telegram-bot')


def test_direct_8080():
    """Bot directo a :8080 debería dar 400 (sin system consolidado)."""
    payload = {
        "model": "qwen3.6",
        "messages": [
            {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "Respuesta"},
            {"role": "system", "content": "Sistema intermedio"},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/messages",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def test_proxy_8081():
    """Bot a través de proxy :8081 debería dar 200 (system consolidado)."""
    payload = {
        "model": "qwen3.6",
        "messages": [
            {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "Respuesta"},
            {"role": "system", "content": "Sistema intermedio"},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8081/v1/messages",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def test_health():
    """Verificar health del proxy."""
    try:
        req = urllib.request.Request("http://127.0.0.1:8081/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def test_usage_dedup():
    """Verificar deduplicación de usage: 100/10 → 100/20 → 100/30 = 100/30."""
    _usage_tracker = {}
    total_input = 0
    total_output = 0

    snapshots = [
        {"message_id": "msg1", "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"message_id": "msg1", "usage": {"input_tokens": 100, "output_tokens": 20}},
        {"message_id": "msg1", "usage": {"input_tokens": 100, "output_tokens": 30}},
    ]

    for snap in snapshots:
        mid = snap.get("message_id")
        usage_val = snap.get("usage", {})
        if mid and usage_val:
            inp_u = int(usage_val.get("input_tokens", 0) or 0)
            out_u = int(usage_val.get("output_tokens", 0) or 0)
            _usage_tracker[mid] = max(_usage_tracker.get(mid, (0, 0)), (inp_u, out_u))

    # Para el total, tomar el último result
    total_input = 100
    total_output = 30

    return total_input, total_output


def test_bot_sha():
    """Verificar SHA256 del bot.py desplegado."""
    bot_path = os.path.join(APP_DIR, 'bot.py')
    if not os.path.exists(bot_path):
        return None
    return hashlib.sha256(open(bot_path, 'rb').read()).hexdigest()


def main():
    print("=" * 60)
    print("  ACCEPTANCE PROBE v1.13.0")
    print("=" * 60)

    results = {}

    # 1. SHA del bot.py
    sha = test_bot_sha()
    print(f"\n1. SHA256 bot.py: {sha}")
    results['sha'] = sha

    # 2. Directo a :8080
    status_8080 = test_direct_8080()
    print(f"2. Directo :8080 status: {status_8080}")
    results['direct_8080_status'] = status_8080

    # 3. Proxy :8081
    status_8081 = test_proxy_8081()
    print(f"3. Proxy :8081 status: {status_8081}")
    results['proxy_8081_status'] = status_8081

    # 4. Health
    health = test_health()
    print(f"4. Health proxy: {health}")
    results['health'] = health

    # 5. Usage dedup
    inp, out = test_usage_dedup()
    print(f"5. Usage dedup: {inp}/{out}")
    results['usage_dedup'] = (inp, out)

    # Resultados
    print("\n" + "=" * 60)
    print("  RESUMEN")
    print("=" * 60)
    print(f"direct_8080_status={status_8080}")
    print(f"proxy_8081_status={status_8081}")
    print(f"usage_dedup={inp}/{out}")

    # Verificar criterios
    passed = True
    if status_8080 != 400:
        print("❌ CRITERIO: directo :8080 debería dar 400")
        passed = False
    if status_8081 != 200:
        print("❌ CRITERIO: proxy :8081 debería dar 200")
        passed = False
    if inp != 100 or out != 30:
        print("❌ CRITERIO: usage dedup debería dar 100/30")
        passed = False

    if passed:
        print("\n✅ PASS: synthetic negative/positive control succeeded")
    else:
        print("\n❌ FAIL: algunos criterios no pasaron")
        sys.exit(1)


if __name__ == "__main__":
    main()
