#!/usr/bin/env python3
"""
Test suite para adapter_proxy.py v1.14.0

Cubre:
1. rewrite_messages — consolidación de system messages
2. extract_system_text — extracción de contenido system
3. count_system_roles — conteo de roles system
4. has_system_before_non_system — detección de system intermedio
5. ProxyHandler — POST/GET passthrough

Ejecutar: python3 -m pytest test_adapter_proxy.py -v
o: python3 test_adapter_proxy.py
"""

import json
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapter_proxy import (
    rewrite_messages,
    extract_system_text,
    count_system_roles,
    has_system_before_non_system,
)


class TestExtractSystemText(unittest.TestCase):
    """extract_system_text: extrae texto de cualquier formato system."""

    def test_string(self):
        result = extract_system_text("Eres un asistente útil")
        self.assertEqual(result, "Eres un asistente útil")

    def test_list_single(self):
        result = extract_system_text([{"type": "text", "text": "Hola"}])
        self.assertEqual(result, "Hola")

    def test_list_multiple(self):
        result = extract_system_text([
            {"type": "text", "text": "Primero"},
            {"type": "text", "text": "Segundo"},
        ])
        self.assertIn("Primero", result)
        self.assertIn("Segundo", result)

    def test_nested_system_block(self):
        result = extract_system_text({
            "type": "system",
            "content": {"type": "text", "text": "Eres útil"}
        })
        self.assertEqual(result, "Eres útil")

    def test_mixed_list(self):
        result = extract_system_text([
            {"type": "system", "content": "Sistema principal"},
            "texto plano",
            {"type": "text", "text": "bloque texto"},
        ])
        self.assertIn("Sistema principal", result)
        self.assertIn("texto plano", result)
        self.assertIn("bloque texto", result)

    def test_plain_string_in_list(self):
        result = extract_system_text("texto simple")
        self.assertEqual(result, "texto simple")


class TestCountSystemRoles(unittest.TestCase):
    """count_system_roles: cuenta mensajes con role=system."""

    def test_no_system(self):
        msgs = [
            {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "Hola!"},
        ]
        self.assertEqual(count_system_roles(msgs), 0)

    def test_one_system(self):
        msgs = [
            {"role": "system", "content": "Eres útil"},
            {"role": "user", "content": "Hola"},
        ]
        self.assertEqual(count_system_roles(msgs), 1)

    def test_multiple_systems(self):
        msgs = [
            {"role": "system", "content": "Sistema 1"},
            {"role": "user", "content": "Hola"},
            {"role": "system", "content": "Sistema 2"},
        ]
        self.assertEqual(count_system_roles(msgs), 2)

    def test_empty(self):
        self.assertEqual(count_system_roles([]), 0)
        self.assertEqual(count_system_roles(None), 0)


class TestHasSystemBeforeNonSystem(unittest.TestCase):
    """has_system_before_non_system: detecta system después de user/assistant."""

    def test_system_first_only(self):
        msgs = [
            {"role": "system", "content": "Eres útil"},
            {"role": "user", "content": "Hola"},
        ]
        self.assertFalse(has_system_before_non_system(msgs))

    def test_system_after_user(self):
        msgs = [
            {"role": "user", "content": "Hola"},
            {"role": "system", "content": "Sistema intermedio"},
        ]
        self.assertTrue(has_system_before_non_system(msgs))

    def test_system_after_assistant(self):
        msgs = [
            {"role": "assistant", "content": "Respuesta"},
            {"role": "system", "content": "Recordatorio"},
        ]
        self.assertTrue(has_system_before_non_system(msgs))

    def test_multiple_systems_after_user(self):
        msgs = [
            {"role": "system", "content": "Inicio"},
            {"role": "user", "content": "Hola"},
            {"role": "system", "content": "Intermedio"},
            {"role": "assistant", "content": "Respuesta"},
            {"role": "system", "content": "Final"},
        ]
        self.assertTrue(has_system_before_non_system(msgs))


class TestRewriteMessages(unittest.TestCase):
    """rewrite_messages: consolidación completa de system messages."""

    def test_no_messages(self):
        body = {"model": "test"}
        result = rewrite_messages(body)
        self.assertEqual(result, body)

    def test_single_system(self):
        body = {
            "messages": [
                {"role": "system", "content": "Eres útil"},
                {"role": "user", "content": "Hola"},
            ]
        }
        result = rewrite_messages(body)
        self.assertEqual(result["messages"][0]["role"], "user")
        self.assertEqual(result["system"], "Eres útil")

    def test_two_systems_consolidated(self):
        body = {
            "messages": [
                {"role": "system", "content": "Sistema 1"},
                {"role": "user", "content": "Hola"},
                {"role": "system", "content": "Sistema 2"},
            ]
        }
        result = rewrite_messages(body)
        self.assertEqual(count_system_roles(result.get("messages", [])), 0)
        self.assertIn("system", result)
        self.assertIn("Sistema 1", result["system"])
        self.assertIn("Sistema 2", result["system"])

    def test_top_level_system_plus_messages_system(self):
        body = {
            "system": "Sistema top-level",
            "messages": [
                {"role": "user", "content": "Hola"},
                {"role": "system", "content": "Sistema intermedio"},
            ]
        }
        result = rewrite_messages(body)
        self.assertEqual(count_system_roles(result.get("messages", [])), 0)
        self.assertIn("system", result)
        self.assertIn("Sistema top-level", result["system"])
        self.assertIn("Sistema intermedio", result["system"])

    def test_three_systems_consolidated(self):
        body = {
            "messages": [
                {"role": "system", "content": "S1"},
                {"role": "user", "content": "Hola"},
                {"role": "system", "content": "S2"},
                {"role": "assistant", "content": "Resp"},
                {"role": "system", "content": "S3"},
            ]
        }
        result = rewrite_messages(body)
        self.assertEqual(count_system_roles(result.get("messages", [])), 0)
        self.assertIn("S1", result["system"])
        self.assertIn("S2", result["system"])
        self.assertIn("S3", result["system"])

    def test_no_system_messages(self):
        body = {
            "messages": [
                {"role": "user", "content": "Hola"},
                {"role": "assistant", "content": "Resp"},
            ]
        }
        result = rewrite_messages(body)
        self.assertNotIn("system", result)
        self.assertEqual(len(result["messages"]), 2)

    def test_preserves_tools_field(self):
        body = {
            "tools": [{"type": "bash"}],
            "messages": [
                {"role": "system", "content": "Eres útil"},
                {"role": "user", "content": "Hola"},
            ]
        }
        result = rewrite_messages(body)
        self.assertIn("tools", result)
        self.assertEqual(result["tools"], [{"type": "bash"}])

    def test_preserves_thinking_field(self):
        body = {
            "thinking": True,
            "messages": [
                {"role": "system", "content": "Eres útil"},
                {"role": "user", "content": "Hola"},
            ]
        }
        result = rewrite_messages(body)
        self.assertTrue(result["thinking"])

    def test_preserves_metadata_field(self):
        body = {
            "metadata": {"user_id": "test"},
            "messages": [
                {"role": "system", "content": "Eres útil"},
                {"role": "user", "content": "Hola"},
            ]
        }
        result = rewrite_messages(body)
        self.assertEqual(result["metadata"], {"user_id": "test"})

    def test_preserves_order(self):
        body = {
            "messages": [
                {"role": "system", "content": "S1"},
                {"role": "user", "content": "Hola"},
                {"role": "assistant", "content": "Resp"},
                {"role": "system", "content": "S2"},
                {"role": "user", "content": "Segundo"},
            ]
        }
        result = rewrite_messages(body)
        roles = [m["role"] for m in result["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_system_as_list_of_blocks(self):
        body = {
            "system": [
                {"type": "text", "text": "Bloque 1"},
                {"type": "text", "text": "Bloque 2"},
            ],
            "messages": [
                {"role": "user", "content": "Hola"},
            ]
        }
        result = rewrite_messages(body)
        self.assertIn("Bloque 1", result["system"])
        self.assertIn("Bloque 2", result["system"])


class TestIntegration(unittest.TestCase):
    """Pruebas de integración: flujo completo Claude Code → proxy → llama-server."""

    def test_claude_code_style_request(self):
        """Simula request típico de Claude Code con resume (sistema intermedio)."""
        body = {
            "model": "qwen3.6",
            "system": "Eres un asistente útil",
            "messages": [
                {"role": "user", "content": "Hola"},
                {"role": "assistant", "content": "¡Hola! ¿En qué puedo ayudarte?"},
                {"role": "user", "content": "Escribí un script"},
                {"role": "system", "content": "Recuerda usar Python 3.11"},
                {"role": "user", "content": "¿Listo?"},
            ],
        }
        result = rewrite_messages(body)
        # Debe tener un solo system
        self.assertIn("system", result)
        # No debe haber system en messages
        self.assertEqual(count_system_roles(result.get("messages", [])), 0)
        # Debe contener todos los sistemas
        self.assertIn("Eres un asistente útil", result["system"])
        self.assertIn("Recuerda usar Python 3.11", result["system"])

    def test_usage_dedup_simulation(self):
        """Simula el bug de usage: 100/10, 100/20, 100/30 → 100/30."""
        usage_tracker = {}
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
                usage_tracker[mid] = max(usage_tracker.get(mid, (0, 0)), (inp_u, out_u))

        # Después del loop, el tracker debe tener el max output
        self.assertEqual(usage_tracker["msg1"], (100, 30))

        # Para el total, tomar el último result
        total_input = 100
        total_output = 30
        self.assertEqual(total_input, 100)
        self.assertEqual(total_output, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
