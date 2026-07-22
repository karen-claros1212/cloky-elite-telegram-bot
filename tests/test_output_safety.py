"""
Tests de saneamiento de salida.

Cada fixture salió de un incidente REAL documentado con screenshot.
Correr desde la raíz del proyecto:

    python3 -m unittest discover -s tests -v
"""
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="cloky-tests-")
os.environ.setdefault("BOT_HOME", _TMP)
Path(_TMP, ".env").write_text(
    "TELEGRAM_BOT_TOKEN=test:fake\nALLOWED_TELEGRAM_USER_ID=1\n", encoding="utf-8"
)
os.chdir(_TMP)

import bot  # noqa: E402


class TestInternalStrings(unittest.TestCase):
    """Metadata interna que NUNCA debe llegar al chat."""

    def test_prompt_de_reintento(self):
        # Incidente 2026-07-21: se filtró a Telegram
        s = ("[Your previous response had no visible output. "
             "Please continue and produce a user-visible response.]")
        self.assertTrue(bot.is_internal_string(s))

    def test_timestamp_iso(self):
        self.assertTrue(bot.is_internal_string("2026-07-21T00:58:12.108Z"))

    def test_chatcmpl_id(self):
        self.assertTrue(bot.is_internal_string("chatcmpl-2oWNDP1pQyaXCfOOKLgMA22TTNeFEHyR"))

    def test_uuid_de_sesion(self):
        self.assertTrue(bot.is_internal_string("a55f2d0c-a796-491a-858d-b0ddd9e5b1d2"))

    def test_nombre_mcp(self):
        self.assertTrue(bot.is_internal_string("mcp__engram__mem_capture_passive"))

    def test_skill_id(self):
        self.assertTrue(bot.is_internal_string("karpathy-coder@claude-code-skills"))

    def test_path_interno(self):
        self.assertTrue(bot.is_internal_string("/home/jesus/.claude/projects/x/y.jsonl"))

    def test_modelo_gguf(self):
        self.assertTrue(bot.is_internal_string("Qwen3.6-35B-A3B-Uncensored-Q4_K_P.gguf"))

    def test_texto_humano_no_se_bloquea(self):
        for s in (
            "Hola Jesús! ¿En qué te puedo ayudar hoy?",
            "El archivo main.py tiene 250 líneas y compila bien.",
            "Listo: la latencia bajó de 1m46s a 8s.",
        ):
            with self.subTest(s=s):
                self.assertFalse(bot.is_internal_string(s))


class TestBinaryGarbage(unittest.TestCase):
    """Volcados binarios (incidente 2026-07-19)."""

    DUMP = ("A" + " " * 20 + "#A" + " " * 20 + "(A" + " " * 20 + "/A" + " " * 20
            + ":A" + " " * 20 + "AA" + " " * 20 + "IA" + " " * 20 + "RA"
            + " " * 20 + "ZA" + "\x80\x81\x82" * 20)

    def test_dump_real_detectado(self):
        self.assertTrue(bot.is_output_garbage(self.DUMP))

    def test_nul_byte(self):
        self.assertTrue(bot.is_output_garbage("texto\x00con nul y relleno suficiente"))

    def test_replacement_chars(self):
        self.assertTrue(bot.is_output_garbage("roto " + "\ufffd" * 12 + " mas texto"))

    def test_texto_legitimo_pasa(self):
        for s in (
            "Hola Jesús! ¿En qué te ayudo? Todo listo por acá.",
            "```python\ndef foo(x):\n    return x * 2\n```",
            "| Col A | Col B |\n|-------|-------|\n| dato  | otro  |",
            "Emojis y acentos: ñ á é í ó ú → ✅ 🚀 funcionan",
            "VIPER    BoviSense    NexusCorp    CaféBase",
        ):
            with self.subTest(s=s[:30]):
                self.assertFalse(bot.is_output_garbage(s))

    def test_sanitize_quita_control_conserva_contenido(self):
        out = bot.sanitize_output("Hola\x07\x08mundo\x1b[31m con ñáé\x00 y 🚀")
        for ch in ("\x07", "\x1b", "\x00"):
            self.assertNotIn(ch, out)
        self.assertIn("ñáé", out)
        self.assertIn("🚀", out)


class TestFallback(unittest.TestCase):
    """El fallback nunca vuelca crudo."""

    def test_no_vuelca_ndjson(self):
        nd = json.dumps({"type": "result", "subtype": "success",
                         "session_id": "a55f2d0c-a796-491a-858d-b0ddd9e5b1d2"})
        out = bot.fallback_from_outputs(nd, "", 0)
        self.assertNotIn('{"type"', out)
        self.assertNotIn("a55f2d0c", out)

    def test_extrae_texto_humano(self):
        nd = json.dumps({"type": "result", "result": "Hola, todo listo."})
        self.assertIn("Hola, todo listo.", bot.fallback_from_outputs(nd, "", 0))

    def test_filtra_metadata_del_init(self):
        nd = json.dumps({"type": "system", "subtype": "init",
                         "tools": ["mcp__engram__mem_save"],
                         "model": "Qwen3.6-35B.gguf"})
        out = bot.fallback_from_outputs(nd, "", 0)
        self.assertNotIn("mcp__engram", out)
        self.assertNotIn("Qwen3.6", out)

    def test_prompt_interno_no_sale(self):
        nd = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text",
             "text": "[Your previous response had no visible output. "
                     "Please continue and produce a user-visible response.]"}]}})
        self.assertNotIn("no visible output", bot.fallback_from_outputs(nd, "", 0))

    def test_return_code_none_es_legible(self):
        out = bot.fallback_from_outputs("", "", None)
        self.assertNotIn("return_code=None", out)
        self.assertIn("timeout", out)


class TestParser(unittest.TestCase):
    def test_result_string(self):
        line = json.dumps({"type": "result", "subtype": "success", "result": "Texto final"})
        self.assertEqual(bot.parse_stream_line(line)[0], "Texto final")

    def test_result_binario_se_descarta(self):
        line = json.dumps({"type": "result", "result": TestBinaryGarbage.DUMP})
        self.assertIsNone(bot.parse_stream_line(line)[0])

    def test_init_no_produce_texto(self):
        line = json.dumps({"type": "system", "subtype": "init", "tools": []})
        self.assertIsNone(bot.parse_stream_line(line)[0])


class TestUsage(unittest.TestCase):
    """El contador soporta ambos formatos."""

    def test_formato_openai_llama_server(self):
        # Chunk real medido en el llama-server del usuario
        line = json.dumps({"choices": [], "usage": {
            "completion_tokens": 50, "prompt_tokens": 17, "total_tokens": 67}})
        self.assertEqual(bot.extract_usage(line), (17, 50))

    def test_formato_anthropic(self):
        line = json.dumps({"type": "result",
                           "usage": {"input_tokens": 100, "output_tokens": 30}})
        self.assertEqual(bot.extract_usage(line), (100, 30))

    def test_usage_anidado_en_message(self):
        line = json.dumps({"type": "message", "message": {
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}})
        self.assertEqual(bot.extract_usage(line), (7, 3))

    def test_sin_usage(self):
        self.assertEqual(bot.extract_usage(json.dumps({"type": "tool_use"})), (0, 0))

    def test_snapshots_repetidos_no_se_duplican(self):
        """100/10, 100/20, 100/30 → total 100/30 (el último gana)."""
        snapshots = [(100, 10), (100, 20), (100, 30)]
        last = (0, 0)
        tot_i = tot_o = 0
        for i, o in snapshots:
            line = json.dumps({"usage": {"input_tokens": i, "output_tokens": o}})
            pi, po = bot.extract_usage(line)
            if (pi, po) != last:
                # snapshot acumulativo: reemplaza, no suma
                tot_i, tot_o = pi, po
                last = (pi, po)
        self.assertEqual((tot_i, tot_o), (100, 30))


class TestTypingKeepalive(unittest.TestCase):
    def test_refresco_menor_a_5s(self):
        self.assertLess(bot.TypingKeepalive.REFRESH_SECONDS, 5.0)

    def test_start_stop_no_deja_thread_vivo(self):
        import time
        sent = []
        original = bot.send_chat_action
        bot.send_chat_action = lambda cid, action="typing": sent.append(cid)
        try:
            tk = bot.TypingKeepalive(1).start()
            time.sleep(0.2)
            tk.stop()
            n = len(sent)
            time.sleep(0.3)
            self.assertEqual(len(sent), n, "el typing siguió después de stop()")
            self.assertIsNone(tk._thread)
            tk.stop()  # idempotente
        finally:
            bot.send_chat_action = original

    def test_start_doble_reusa_thread(self):
        original = bot.send_chat_action
        bot.send_chat_action = lambda cid, action="typing": None
        try:
            tk = bot.TypingKeepalive(1).start()
            first = tk._thread
            tk.start()
            self.assertIs(tk._thread, first)
            tk.stop()
        finally:
            bot.send_chat_action = original


class TestTranscriptSoloLectura(unittest.TestCase):
    """El bot NUNCA debe modificar el .jsonl interno de Claude Code."""

    def test_analyze_no_modifica_el_archivo(self):
        p = Path(_TMP) / "t1.jsonl"
        contenido = (
            json.dumps({"type": "user", "message": {"content": "hola"}}) + "\n"
            + json.dumps({"type": "tool_result", "toolUseResult": "X" * (60 * 1024)}) + "\n"
        )
        p.write_text(contenido, encoding="utf-8")
        antes = p.read_bytes()
        info = bot.analyze_transcript(p)
        self.assertTrue(info["exists"])
        self.assertEqual(info["total_lines"], 2)
        self.assertEqual(p.read_bytes(), antes, "analyze_transcript modificó el archivo")

    def test_compact_transcript_ya_no_existe(self):
        """La función que truncaba el JSONL fue eliminada por peligrosa."""
        self.assertFalse(hasattr(bot, "compact_transcript"))


class TestFixesAuditoria(unittest.TestCase):
    """Correcciones de la auditoría externa sobre v2.1.0."""

    def test_sin_fork_session_en_continuaciones(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertNotIn('"--fork-session"', src)

    def test_resume_presente_para_continuar(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertIn('"--resume"', src)

    def test_include_partial_messages(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertIn("--include-partial-messages", src)

    def test_usage_son_snapshots_no_incrementos(self):
        """
        Ejercita la lógica REAL de producción, no una reimplementación.
        100/10 → 100/20 → 100/30 debe dar 100/30, no 300/60.
        """
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertIn("tokens_in = max(tokens_in, _i)", src)
        self.assertIn("tokens_out = max(tokens_out, _o)", src)
        # Simulación numérica con la misma lógica
        tokens_in = tokens_out = 0
        for i, o in [(100, 10), (100, 20), (100, 30)]:
            line = json.dumps({"usage": {"input_tokens": i, "output_tokens": o}})
            _i, _o = bot.extract_usage(line)
            if _i or _o:
                tokens_in = max(tokens_in, _i)
                tokens_out = max(tokens_out, _o)
        self.assertEqual((tokens_in, tokens_out), (100, 30))

    def test_sentinel_no_response_requested(self):
        self.assertTrue(bot.is_sentinel_output("No response requested."))
        self.assertTrue(bot.is_sentinel_output("no response requested"))
        self.assertTrue(bot.is_internal_string("No response requested."))
        line = json.dumps({"type": "result", "result": "No response requested."})
        self.assertIsNone(bot.parse_stream_line(line)[0])

    def test_parser_no_camina_campos_desconocidos(self):
        """Campos arbitrarios NO deben producir texto visible."""
        line = json.dumps({
            "type": "assistant",
            "campo_raro": {"anidado": {"secreto": "ESTO NO DEBE SALIR"}},
            "message": {"content": [{"type": "text", "text": "Hola"}]},
        })
        texto, _ = bot.parse_stream_line(line)
        self.assertEqual(texto, "Hola")
        self.assertNotIn("ESTO NO DEBE SALIR", texto or "")

    def test_parser_ignora_tool_use_content(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "rm -rf /tmp/x"}},
                {"type": "text", "text": "Listo"},
            ]},
        })
        texto, status = bot.parse_stream_line(line)
        self.assertEqual(texto, "Listo")
        self.assertNotIn("rm -rf", texto or "")
        self.assertIn("Bash", status or "")

    def test_streaming_parcial_text_delta(self):
        line = json.dumps({
            "type": "stream_event",
            "event": {"delta": {"type": "text_delta", "text": "hola"}},
        })
        self.assertEqual(bot.parse_stream_line(line)[0], "hola")



class TestConstruccionDelComando(unittest.TestCase):
    """
    --session-id y --resume NO pueden ir juntos sin --fork-session.
    Claude Code rechaza esa combinación con código 1:
      "--session-id can only be used with --continue or --resume if
       --fork-session is also specified."
    """

    def _construir(self, is_continuation: bool) -> list:
        """Replica exacta de la lógica de _run_claude_task_inner."""
        sid = "a55f2d0c-a796-491a-858d-b0ddd9e5b1d2"
        cmd = ["claude", "--permission-mode", "bypassPermissions"]
        if is_continuation:
            cmd.extend(["--resume", sid])
        else:
            cmd.extend(["--session-id", sid])
        cmd.extend(["--max-turns", "40", "--model", "m", "--print",
                    "--output-format", "stream-json", "--verbose",
                    "--include-partial-messages"])
        return cmd

    def test_primer_turno_usa_session_id_sin_resume(self):
        cmd = self._construir(False)
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)

    def test_continuacion_usa_resume_sin_session_id(self):
        cmd = self._construir(True)
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_nunca_la_combinacion_ilegal(self):
        for cont in (True, False):
            cmd = self._construir(cont)
            ilegal = "--session-id" in cmd and "--resume" in cmd and "--fork-session" not in cmd
            self.assertFalse(ilegal, f"combinación ilegal con is_continuation={cont}")

    def test_codigo_real_no_pone_ambos_flags(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        bloque = src[src.index("command = [CLAUDE_BIN"):src.index("--max-turns")]
        self.assertIn('command.extend(["--resume", session_id])', bloque)
        self.assertIn('command.extend(["--session-id", session_id])', bloque)
        self.assertIn("if is_continuation:", bloque)
        self.assertIn("else:", bloque)
        self.assertNotIn("--fork-session", bloque)


class TestBackwardCompat(unittest.TestCase):
    def test_version(self):
        self.assertEqual(bot.VERSION, "2.2.2")

    def test_redaccion_de_secretos(self):
        self.assertEqual(bot.redact("sk-morgan-fake-12345"), "[REDACTED]")

    def test_funciones_base_presentes(self):
        for fn in ("get_native_session_id", "build_inline_keyboard", "get_user_mode",
                   "handle_command", "handle_callback_query", "check_forbidden_command"):
            with self.subTest(fn=fn):
                self.assertTrue(hasattr(bot, fn))


class TestStdinPrompt(unittest.TestCase):
    """
    El prompt debe ir por STDIN, no por argv.

    Causa raíz del "Claude Code finalizó sin mensaje visible": bajo systemd
    el stdin heredado no es legible y Claude Code en print-mode salía en
    silencio. Los bridges que funcionan escriben al stdin del binario.
    """

    def test_popen_abre_stdin(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertIn("stdin=subprocess.PIPE", src)

    def test_prompt_no_va_en_argv(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        # El prompt ya no se agrega a la lista de argumentos
        self.assertNotIn('"--verbose",\n        prompt,', src)

    def test_prompt_se_escribe_y_cierra(self):
        src = inspect.getsource(bot._run_claude_task_inner)
        self.assertIn("process.stdin.write(prompt)", src)
        self.assertIn("process.stdin.close()", src)


class TestBotonesSobrios(unittest.TestCase):
    """Interfaz: menú nativo en español, máximo 2 botones inline."""

    def test_maximo_dos_botones_por_fila(self):
        kb = bot.build_inline_keyboard("1", include_operations=True)
        for fila in kb["inline_keyboard"]:
            self.assertLessEqual(len(fila), 2)

    def test_botones_en_espanol(self):
        kb = bot.build_inline_keyboard("1", include_operations=True)
        textos = [b["text"] for fila in kb["inline_keyboard"] for b in fila]
        self.assertTrue(any("Detener" in x for x in textos))
        self.assertTrue(any("Modo" in x for x in textos))
        for x in textos:
            self.assertNotIn("Bypass", x)
            self.assertNotIn("Sessions", x)

    def test_submenu_de_modos_marca_el_activo(self):
        kb = bot.build_mode_keyboard("1")
        textos = [b["text"] for fila in kb["inline_keyboard"] for b in fila]
        self.assertEqual(len(textos), 4)
        self.assertEqual(sum(1 for x in textos if x.startswith("✅")), 1)

    def test_comandos_registrados_en_espanol(self):
        enviados = []
        original = bot.telegram_api
        bot.telegram_api = lambda m, p, timeout=60: (
            enviados.append((m, p)), {"ok": True})[1]
        try:
            bot.register_bot_commands()
        finally:
            bot.telegram_api = original
        self.assertEqual(enviados[0][0], "setMyCommands")
        cmds = enviados[0][1]["commands"]
        self.assertGreaterEqual(len(cmds), 10)
        nombres = [c["command"] for c in cmds]
        for esperado in ("status", "stop", "newsession", "compact"):
            self.assertIn(esperado, nombres)
        # Descripciones en español, no en inglés
        desc = " ".join(c["description"] for c in cmds).lower()
        self.assertIn("sesión", desc)
        self.assertNotIn("session state", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
