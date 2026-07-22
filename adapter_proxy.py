#!/usr/bin/env python3
"""
adapter_proxy.py — v1.14.0 Adapter Proxy
=========================================
Micro-proxy stdlib entre Claude Code y llama-server.
Reescribe requests Anthropic-compat para consolidar system messages
intermedios en el system inicial, sin tocar el server.

Regla: el cliente se adapta al server, no al revés.
~200 líneas, stdlib puro, streaming passthrough intacto.
"""

import http.server
import json
import logging
import socket
import sys
import threading
import urllib.request
import urllib.error
import uuid
from io import BytesIO
from collections import OrderedDict

LISTEN = ("127.0.0.1", 8081)
UPSTREAM = "http://127.0.0.1:8080"

# --- Redacted logging ---
LOG_FORMAT = "%(asctime)s [proxy] %(request_id)s %(path)s sys_before=%(sys_before)d sys_after=%(sys_after)d backend_status=%(backend_status)s"
log_lock = threading.Lock()
request_count = 0

# --- Request counter (for /health) ---
request_counter_lock = threading.Lock()


def get_request_count():
    with request_counter_lock:
        return request_count


# --- System normalization ---

def extract_system_text(content):
    """Extract text from any system content format."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type == "text":
            return content.get("text", "")
        if block_type == "system":
            return extract_system_text(content.get("content", ""))
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return extract_system_text(content["content"])
        return str(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            parts.append(extract_system_text(block))
        return "\n".join(parts)
    return str(content)


def count_system_roles(messages):
    """Count how many messages have role='system'."""
    count = 0
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                count += 1
    return count


def has_system_before_non_system(messages):
    """Check if there's a system message AFTER a non-system message."""
    seen_non_system = False
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict):
                role = m.get("role", "")
                if role != "system":
                    seen_non_system = True
                elif seen_non_system:
                    return True
    return False


def rewrite_messages(body: dict) -> dict:
    """Consolida system messages intermedios en el system inicial.

    Claude Code 2.1.2xx inserta bloques system en posicion no-cero
    durante resume. El template Jinja de Qwen los rechaza con 400.
    Este proxy los mueve al campo `system` de nivel superior antes
    de enviar al server, sin perder contexto.

    Handles:
    - "system" top-level as string
    - "system" top-level as list of blocks
    - "role=system" messages inside "messages" array
    - Nested system blocks/reminders from Claude Code 2.1.215
    """
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return body

    # Collect ALL system content from everywhere
    systems = []

    # 1. Top-level "system" field (string or list)
    top_system = body.get("system")
    if top_system:
        systems.append(extract_system_text(top_system))

    # 2. "system" messages inside the messages array
    kept = []
    for m in messages:
        if not isinstance(m, dict):
            kept.append(m)
            continue
        if m.get("role") == "system":
            systems.append(extract_system_text(m.get("content", "")))
        else:
            kept.append(m)

    if len(systems) <= 1:
        # Si hay exactamente un sistema, asegurar que body["system"] sea string
        if systems:
            body["system"] = systems[0]
            body["messages"] = kept
        return body

    # Merge all systems into one
    first = systems[0]
    extras = systems[1:]

    merged = first
    for extra in extras:
        if extra.strip():
            merged += "\n\n" + extra

    body["system"] = merged
    body["messages"] = kept
    return body


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    request_id_counter = 0

    def log_message(self, format, *args):
        pass  # silent — we use our own logging

    def _log_redacted(self, path, sys_before, sys_after, backend_status):
        """Log in redacted format."""
        global request_count
        with log_lock:
            request_count += 1
            rid = request_count
        try:
            msg = LOG_FORMAT % {
                "asctime": self.log_date_time_string(),
                "request_id": rid,
                "path": path,
                "sys_before": sys_before,
                "sys_after": sys_after,
                "backend_status": backend_status,
            }
            print(msg, flush=True)
        except Exception:
            pass

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length else b""

        upstream_url = UPSTREAM + self.path

        sys_before = 0
        sys_after = 0

        # Only rewrite /v1/messages; everything else passthrough
        if self.path == "/v1/messages":
            try:
                body = json.loads(raw_body)
                sys_before = count_system_roles(body.get("messages", []))
                body = rewrite_messages(body)
                sys_after = count_system_roles(body.get("messages", []))
                raw_body = json.dumps(body).encode("utf-8")
            except (json.JSONDecodeError, Exception):
                pass  # forward as-is on parse failure

        # Forward headers (skip hop-by-hop)
        fwd_headers = {}
        skip = {"host", "connection", "proxy-connection", "keep-alive",
                "transfer-encoding", "te", "trailer", "upgrade"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                fwd_headers[k] = v
        fwd_headers["Content-Length"] = str(len(raw_body))

        req = urllib.request.Request(upstream_url, data=raw_body,
                                      headers=fwd_headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self._log_redacted(self.path, sys_before, sys_after, resp.status)

        except urllib.error.HTTPError as e:
            body_data = e.read()
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(body_data)
            self._log_redacted(self.path, sys_before, sys_after, e.code)

    def do_GET(self):
        upstream_url = UPSTREAM + self.path
        skip = {"host", "connection", "proxy-connection", "keep-alive",
                "transfer-encoding", "te", "trailer", "upgrade"}
        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                fwd_headers[k] = v

        req = urllib.request.Request(upstream_url, headers=fwd_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()


def main():
    print(f"[adapter_proxy] {LISTEN[0]}:{LISTEN[1]} → {UPSTREAM}")
    httpd = http.server.HTTPServer(LISTEN, ProxyHandler)
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
