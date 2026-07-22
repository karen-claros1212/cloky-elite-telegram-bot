from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

from . import __version__
from .approval import ApprovalBroker
from .config import Config
from .doctor import format_checks, run_checks
from .logging_config import configure_logging
from .models import VALID_MODES, UserState
from .render import escape, split_telegram
from .runtime import RuntimeCallbacks, RuntimeManager
from .security import ensure_allowed_path
from .state import StateStore

logger = logging.getLogger("cloky.app")


class CallbackRegistry:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, str, Any]] = {}

    def put(self, kind: str, payload: Any, ttl: int = 900) -> str:
        token = secrets.token_hex(5)
        self._items[token] = (time.time() + ttl, kind, payload)
        return token

    def get(self, token: str, kind: str) -> Any | None:
        item = self._items.get(token)
        if not item:
            return None
        expires, stored_kind, payload = item
        if expires < time.time() or stored_kind != kind:
            self._items.pop(token, None)
            return None
        return payload

    def cleanup(self) -> None:
        now = time.time()
        for token, (expires, _, _) in list(self._items.items()):
            if expires < now:
                self._items.pop(token, None)


class TelegramTurnUI:
    def __init__(self, bot: Any, chat_id: int, reply_to: int, interval: float):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.interval = interval
        self.message_id: int | None = None
        self.status = "Iniciando…"
        self.preview = ""
        self.last_edit = 0.0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Detener", callback_data="ui:stop"),
            InlineKeyboardButton("📋 Modo", callback_data="ui:mode"),
        ]])
        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text="✶ Iniciando Claude Code…",
            reply_to_message_id=self.reply_to,
            reply_markup=keyboard,
        )
        self.message_id = message.message_id

    async def on_status(self, status: str) -> None:
        self.status = status
        await self._edit(force=False)

    async def on_preview(self, preview: str) -> None:
        self.preview = preview
        await self._edit(force=False)

    async def on_session(self, session_id: str) -> None:
        logger.info("session_selected", extra={"session_id": session_id})

    async def _edit(self, force: bool) -> None:
        if self.message_id is None:
            return
        now = time.time()
        if not force and now - self.last_edit < self.interval:
            return
        async with self._lock:
            now = time.time()
            if not force and now - self.last_edit < self.interval:
                return
            body = f"✶ {self.status}"
            if self.preview:
                body += "\n\n" + self.preview[-3500:]
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=body[:3900],
                    reply_markup=self._running_keyboard(),
                )
                self.last_edit = now
            except Exception as exc:
                if "Message is not modified" not in str(exc):
                    logger.debug("status_edit_failed %s", exc)

    @staticmethod
    def _running_keyboard() -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Detener", callback_data="ui:stop"),
            InlineKeyboardButton("📋 Modo", callback_data="ui:mode"),
        ]])

    @staticmethod
    def _final_keyboard() -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Modo", callback_data="ui:mode"),
            InlineKeyboardButton("🧵 Sesiones", callback_data="ui:sessions"),
        ]])

    async def finalize(self, text: str) -> None:
        chunks = split_telegram(text)
        if self.message_id is None:
            for index, chunk in enumerate(chunks):
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=chunk,
                    reply_markup=self._final_keyboard() if index == len(chunks) - 1 else None,
                )
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=chunks[0],
                reply_markup=self._final_keyboard() if len(chunks) == 1 else None,
            )
        except Exception:
            await self.bot.send_message(chat_id=self.chat_id, text=chunks[0])
        for index, chunk in enumerate(chunks[1:]):
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=chunk,
                reply_markup=self._final_keyboard() if index == len(chunks) - 2 else None,
            )


class ClokyBot:
    def __init__(self, config: Config):
        self.config = config
        self.store = StateStore(config.db_path, config.default_project, config.default_mode, config.claude_model)
        self.manager = RuntimeManager(config, self.store)
        self.callback_registry = CallbackRegistry()
        self.approval_broker: ApprovalBroker | None = None
        self.active_tasks: dict[int, asyncio.Task[Any]] = {}
        self._eviction_task: asyncio.Task[Any] | None = None

    def allowed(self, user_id: int) -> bool:
        return user_id in self.config.allowed_user_ids

    async def post_init(self, application: Any) -> None:
        from telegram import BotCommand

        self.approval_broker = ApprovalBroker(application.bot, self.config.approval_timeout_seconds)
        commands = [
            BotCommand("status", "Estado de sesión y tarea"),
            BotCommand("new", "Crear sesión nueva"),
            BotCommand("sessions", "Ver y reanudar sesiones"),
            BotCommand("projects", "Cambiar proyecto"),
            BotCommand("worktrees", "Cambiar worktree Git"),
            BotCommand("mode", "Cambiar Plan/Edit/Bypass"),
            BotCommand("stop", "Detener la tarea activa"),
            BotCommand("history", "Ver mensajes recientes"),
            BotCommand("doctor", "Diagnóstico del bot y backend"),
            BotCommand("usage", "Uso del último turno"),
            BotCommand("version", "Versiones de Cloky y Claude Code"),
            BotCommand("help", "Ayuda"),
        ]
        await application.bot.set_my_commands(commands)
        self._eviction_task = asyncio.create_task(self._eviction_loop())
        logger.info("cloky_ready")

    async def post_shutdown(self, application: Any) -> None:
        if self._eviction_task:
            self._eviction_task.cancel()
        await self.manager.close()
        self.store.close()

    async def _eviction_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                count = await self.manager.evict_idle()
                self.callback_registry.cleanup()
                if count:
                    logger.info("idle_runtimes_evicted %s", count)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("eviction_loop_failed")

    async def _require_allowed(self, update: Any) -> bool:
        user = update.effective_user
        if not user or not self.allowed(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("Usuario no autorizado.")
            return False
        return True

    async def cmd_start(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        await update.effective_message.reply_text(
            "Cloky Enterprise está listo. Enviá una tarea o usá /sessions, /projects, /mode y /doctor."
        )

    async def cmd_help(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        await update.effective_message.reply_text(
            "/new nueva sesión\n"
            "/sessions sesiones oficiales de Claude Code\n"
            "/projects cambiar proyecto\n"
            "/worktrees cambiar worktree\n"
            "/mode cambiar permisos\n"
            "/stop interrumpir tarea\n"
            "/history historial de sesión\n"
            "/doctor diagnóstico\n"
            "/usage uso del último turno"
        )

    async def cmd_status(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        user_id = update.effective_user.id
        state = self.store.get_user(user_id)
        runtime = self.manager.status(user_id)
        lines = [
            f"Proyecto: {state.project_path}",
            f"Sesión: {state.session_id or 'nueva'}",
            f"Modo: {state.mode}",
            f"Modelo: {state.model or self.config.claude_model or 'default'}",
        ]
        if runtime:
            lines.append(f"Runtime: {'ocupado' if runtime.busy else 'idle'} — {runtime.status}")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_new(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        user_id = update.effective_user.id
        await self.manager.reset_user(user_id)
        state = self.store.update_user(user_id, session_id=None, fork_next=False)
        self.store.audit("session_new", user_id=user_id, project=state.project_path)
        await update.effective_message.reply_text("Nueva sesión preparada. El próximo mensaje será el primer turno.")

    async def cmd_sessions(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        await self._show_sessions(update.effective_user.id, update.effective_chat.id, context.bot)

    async def _show_sessions(self, user_id: int, chat_id: int, bot: Any) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        state = self.store.get_user(user_id)
        try:
            from claude_agent_sdk import list_sessions

            sessions = await asyncio.to_thread(
                list_sessions,
                directory=state.project_path,
                limit=self.config.session_list_limit,
            )
        except Exception as exc:
            await bot.send_message(chat_id=chat_id, text=f"No se pudieron listar sesiones: {type(exc).__name__}: {exc}")
            return
        rows = []
        for session in sessions:
            sid = str(getattr(session, "session_id", ""))
            if not sid:
                continue
            title = (
                getattr(session, "custom_title", None)
                or getattr(session, "summary", None)
                or sid[:8]
            )
            active = "✓ " if sid == state.session_id else ""
            rows.append([InlineKeyboardButton(f"{active}{str(title)[:45]}", callback_data=f"sess:{sid}")])
        rows.append([InlineKeyboardButton("➕ Nueva sesión", callback_data="ui:new")])
        await bot.send_message(
            chat_id=chat_id,
            text=f"Sesiones en {state.project_path}",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def cmd_resume(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        if not context.args:
            await update.effective_message.reply_text("Uso: /resume <session_id>")
            return
        await self._resume_session(update.effective_user.id, context.args[0])
        await update.effective_message.reply_text("Sesión seleccionada.")

    async def _resume_session(self, user_id: int, session_id: str) -> None:
        await self.manager.reset_user(user_id)
        self.store.update_user(user_id, session_id=session_id, fork_next=False)
        self.store.audit("session_resume", user_id=user_id, session_id=session_id)

    async def cmd_rename(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        title = " ".join(context.args).strip()
        state = self.store.get_user(update.effective_user.id)
        if not state.session_id:
            await update.effective_message.reply_text("No hay sesión activa para renombrar.")
            return
        if not title:
            await update.effective_message.reply_text("Uso: /rename <nombre>")
            return
        try:
            from claude_agent_sdk import rename_session

            await asyncio.to_thread(rename_session, state.session_id, title, directory=state.project_path)
            await update.effective_message.reply_text("Sesión renombrada.")
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo renombrar: {type(exc).__name__}: {exc}")

    async def cmd_fork(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        user_id = update.effective_user.id
        state = self.store.get_user(user_id)
        if not state.session_id:
            await update.effective_message.reply_text("No hay sesión activa para bifurcar.")
            return
        await self.manager.reset_user(user_id)
        self.store.update_user(user_id, fork_next=True)
        await update.effective_message.reply_text("El próximo mensaje creará una rama de la sesión actual.")

    async def cmd_mode(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        if context.args:
            requested = context.args[0]
            aliases = {"edit": "default", "auto": "acceptEdits", "bypass": "bypassPermissions"}
            requested = aliases.get(requested, requested)
            if requested not in VALID_MODES:
                await update.effective_message.reply_text("Modo inválido.")
                return
            await self._set_mode(update.effective_user.id, requested)
            await update.effective_message.reply_text(f"Modo activo: {requested}")
            return
        await update.effective_message.reply_text("Elegí el modo:", reply_markup=self._mode_keyboard())

    @staticmethod
    def _mode_keyboard() -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Plan", callback_data="mode:plan"),
                InlineKeyboardButton("✏️ Manual", callback_data="mode:default"),
            ],
            [
                InlineKeyboardButton("⚡ Auto edits", callback_data="mode:acceptEdits"),
                InlineKeyboardButton("🚀 Bypass", callback_data="mode:bypassPermissions"),
            ],
        ])

    async def _set_mode(self, user_id: int, mode: str) -> None:
        self.store.update_user(user_id, mode=mode)
        await self.manager.set_mode(user_id, mode)
        self.store.audit("mode_changed", user_id=user_id, mode=mode)

    async def cmd_plan(self, update: Any, context: Any) -> None:
        context.args = ["plan"]
        await self.cmd_mode(update, context)

    async def cmd_edit(self, update: Any, context: Any) -> None:
        context.args = ["default"]
        await self.cmd_mode(update, context)

    async def cmd_bypass(self, update: Any, context: Any) -> None:
        context.args = ["bypassPermissions"]
        await self.cmd_mode(update, context)

    async def cmd_stop(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        user_id = update.effective_user.id
        stopped = await self.manager.interrupt(user_id)
        task = self.active_tasks.get(user_id)
        if task and not task.done() and not stopped:
            task.cancel()
            stopped = True
        await update.effective_message.reply_text("Detención solicitada." if stopped else "No hay tarea activa.")

    async def cmd_projects(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        state = self.store.get_user(update.effective_user.id)
        rows = []
        for name, path in self.config.projects.items():
            token = self.callback_registry.put("project", str(path))
            active = "✓ " if Path(state.project_path).resolve() == path.resolve() else ""
            rows.append([InlineKeyboardButton(f"{active}{name}", callback_data=f"proj:{token}")])
        await update.effective_message.reply_text("Proyectos permitidos:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_worktrees(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        state = self.store.get_user(update.effective_user.id)
        paths = await asyncio.to_thread(self._git_worktrees, Path(state.project_path))
        if not paths:
            await update.effective_message.reply_text("No se encontraron worktrees Git.")
            return
        rows = []
        for path in paths:
            try:
                ensure_allowed_path(path, list(self.config.projects.values()))
            except PermissionError:
                continue
            token = self.callback_registry.put("worktree", str(path))
            rows.append([InlineKeyboardButton(path.name or str(path), callback_data=f"wt:{token}")])
        await update.effective_message.reply_text("Worktrees:", reply_markup=InlineKeyboardMarkup(rows))

    @staticmethod
    def _git_worktrees(project: Path) -> list[Path]:
        try:
            result = subprocess.run(
                ["git", "-C", str(project), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return []
        paths = []
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[9:]).resolve())
        return paths

    async def _switch_project(self, user_id: int, path: str, *, trusted: bool = False) -> None:
        target = Path(path).expanduser().resolve() if trusted else ensure_allowed_path(Path(path), list(self.config.projects.values()))
        await self.manager.reset_user(user_id)
        self.store.update_user(user_id, project_path=str(target), session_id=None, fork_next=False)
        self.store.audit("project_changed", user_id=user_id, project=str(target))

    async def cmd_history(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        state = self.store.get_user(update.effective_user.id)
        if not state.session_id:
            await update.effective_message.reply_text("No hay sesión activa.")
            return
        try:
            from claude_agent_sdk import get_session_messages

            messages = await asyncio.to_thread(
                get_session_messages,
                state.session_id,
                directory=state.project_path,
                limit=10,
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo leer la sesión: {type(exc).__name__}: {exc}")
            return
        lines = []
        for item in messages:
            role = getattr(item, "type", "?")
            raw = getattr(item, "message", None)
            text = self._message_summary(raw)
            if text:
                lines.append(f"[{role}] {text}")
        await update.effective_message.reply_text("\n\n".join(lines)[-3900:] or "Sin mensajes visibles.")

    @staticmethod
    def _message_summary(raw: Any) -> str:
        if isinstance(raw, str):
            return raw[:600]
        if isinstance(raw, dict):
            content = raw.get("content")
            if isinstance(content, str):
                return content[:600]
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        texts.append(block["text"])
                return " ".join(texts)[:600]
        return ""

    async def cmd_doctor(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        checks = await asyncio.to_thread(run_checks, self.config, bool(context.args and context.args[0] == "full"))
        await update.effective_message.reply_text(format_checks(checks))

    async def cmd_usage(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        task = self.store.last_task(update.effective_user.id)
        if not task:
            await update.effective_message.reply_text("Todavía no hay métricas.")
            return
        await update.effective_message.reply_text(
            f"Estado: {task['status']}\n"
            f"Input: {task['input_tokens']}\n"
            f"Output: {task['output_tokens']}\n"
            f"Costo estimado: ${task['cost_usd']:.6f}"
        )

    async def cmd_version(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [self.config.claude_cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            cli_version = (result.stdout or result.stderr).strip()
        except Exception as exc:
            cli_version = f"error: {type(exc).__name__}: {exc}"
        await update.effective_message.reply_text(
            f"Cloky: {__version__}\nClaude Code: {cli_version}\nBackend: {self.config.anthropic_base_url}"
        )

    async def cmd_mcp(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        user_id = update.effective_user.id
        state = self.store.get_user(user_id)
        if self.approval_broker is None:
            return
        runtime = await self.manager.get(state, update.effective_chat.id, self.approval_broker)
        try:
            await runtime.connect()
            status = await runtime.client.get_mcp_status()
            await update.effective_message.reply_text(str(status)[:3900])
        except Exception as exc:
            await update.effective_message.reply_text(f"MCP no disponible: {type(exc).__name__}: {exc}")

    async def on_text(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        user_id = update.effective_user.id
        if self.approval_broker and await self.approval_broker.consume_text(user_id, text):
            await update.effective_message.reply_text("Respuesta enviada a Claude.")
            return
        await self._run_prompt(update, text)

    async def on_document(self, update: Any, context: Any) -> None:
        if not await self._require_allowed(update):
            return
        document = update.effective_message.document
        if not document:
            return
        if document.file_size and document.file_size > self.config.max_upload_bytes:
            await update.effective_message.reply_text("Archivo demasiado grande.")
            return
        file_name = Path(document.file_name or f"document-{document.file_unique_id}").name
        target = self.config.uploads_dir / f"{int(time.time())}-{file_name}"
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=target)
        caption = (update.effective_message.caption or "Analizá este archivo").strip()
        await self._run_prompt(update, f"Archivo recibido en {target}.\n\nPetición: {caption}")

    async def _run_prompt(self, update: Any, prompt: str) -> None:
        user_id = update.effective_user.id
        if user_id in self.active_tasks and not self.active_tasks[user_id].done():
            await update.effective_message.reply_text("Ya hay una tarea activa. Usá /stop.")
            return
        if self.approval_broker is None:
            await update.effective_message.reply_text("El bot todavía está inicializando.")
            return

        async def runner() -> None:
            state = self.store.get_user(user_id)
            runtime = await self.manager.get(state, update.effective_chat.id, self.approval_broker)
            ui = TelegramTurnUI(
                update.effective_message.bot,
                update.effective_chat.id,
                update.effective_message.message_id,
                self.config.stream_edit_interval,
            )
            await ui.start()
            callbacks = RuntimeCallbacks(
                on_status=ui.on_status,
                on_preview=ui.on_preview,
                on_session=ui.on_session,
            )
            result = await runtime.query(prompt, callbacks)
            await ui.finalize(result.text)

        task = asyncio.create_task(runner())
        self.active_tasks[user_id] = task
        try:
            await task
        finally:
            self.active_tasks.pop(user_id, None)

    async def on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        if not query or not query.from_user or not self.allowed(query.from_user.id):
            return
        user_id = query.from_user.id
        if self.approval_broker and await self.approval_broker.handle_callback(query, user_id):
            return
        data = query.data or ""
        if data == "ui:stop":
            stopped = await self.manager.interrupt(user_id)
            await query.answer("Detención solicitada" if stopped else "No hay tarea activa")
            return
        if data == "ui:mode":
            await query.answer()
            await context.bot.send_message(chat_id=query.message.chat_id, text="Elegí el modo:", reply_markup=self._mode_keyboard())
            return
        if data == "ui:sessions":
            await query.answer()
            await self._show_sessions(user_id, query.message.chat_id, context.bot)
            return
        if data == "ui:new":
            await query.answer("Nueva sesión")
            await self.manager.reset_user(user_id)
            self.store.update_user(user_id, session_id=None, fork_next=False)
            return
        if data.startswith("mode:"):
            mode = data.split(":", 1)[1]
            if mode in VALID_MODES:
                await self._set_mode(user_id, mode)
                await query.answer(f"Modo: {mode}")
                try:
                    await query.edit_message_text(f"Modo activo: {mode}")
                except Exception:
                    pass
            return
        if data.startswith("sess:"):
            sid = data.split(":", 1)[1]
            await self._resume_session(user_id, sid)
            await query.answer("Sesión seleccionada")
            try:
                await query.edit_message_text(f"Sesión activa: {sid[:8]}…")
            except Exception:
                pass
            return
        if data.startswith("proj:"):
            token = data.split(":", 1)[1]
            path = self.callback_registry.get(token, "project")
            if path:
                await self._switch_project(user_id, path)
                await query.answer("Proyecto cambiado")
                try:
                    await query.edit_message_text(f"Proyecto activo: {path}")
                except Exception:
                    pass
            else:
                await query.answer("Opción vencida", show_alert=True)
            return
        if data.startswith("wt:"):
            token = data.split(":", 1)[1]
            path = self.callback_registry.get(token, "worktree")
            if path:
                await self._switch_project(user_id, path, trusted=True)
                await query.answer("Worktree cambiado")
                try:
                    await query.edit_message_text(f"Worktree activo: {path}")
                except Exception:
                    pass
            else:
                await query.answer("Opción vencida", show_alert=True)
            return
        await query.answer("Acción no reconocida")

    async def on_error(self, update: object, context: Any) -> None:
        logger.exception("telegram_handler_error", exc_info=context.error)

    def build_application(self) -> Any:
        from telegram.ext import (
            AIORateLimiter,
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        application = (
            Application.builder()
            .token(self.config.telegram_token)
            .rate_limiter(AIORateLimiter())
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CommandHandler(["new", "newsession", "clear"], self.cmd_new))
        application.add_handler(CommandHandler("sessions", self.cmd_sessions))
        application.add_handler(CommandHandler("resume", self.cmd_resume))
        application.add_handler(CommandHandler("rename", self.cmd_rename))
        application.add_handler(CommandHandler("fork", self.cmd_fork))
        application.add_handler(CommandHandler("mode", self.cmd_mode))
        application.add_handler(CommandHandler("plan", self.cmd_plan))
        application.add_handler(CommandHandler("edit", self.cmd_edit))
        application.add_handler(CommandHandler("bypass", self.cmd_bypass))
        application.add_handler(CommandHandler(["stop", "cancel", "abort"], self.cmd_stop))
        application.add_handler(CommandHandler("projects", self.cmd_projects))
        application.add_handler(CommandHandler(["worktree", "worktrees"], self.cmd_worktrees))
        application.add_handler(CommandHandler("history", self.cmd_history))
        application.add_handler(CommandHandler("doctor", self.cmd_doctor))
        application.add_handler(CommandHandler("usage", self.cmd_usage))
        application.add_handler(CommandHandler(["version", "claudeversion"], self.cmd_version))
        application.add_handler(CommandHandler(["mcp", "mcps"], self.cmd_mcp))
        application.add_handler(CallbackQueryHandler(self.on_callback))
        application.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        application.add_error_handler(self.on_error)
        return application


def main() -> None:
    config = Config.load()
    configure_logging(config.log_dir, os.environ.get("LOG_LEVEL", "INFO"))
    errors = config.validate()
    if errors:
        raise SystemExit("Configuración inválida: " + "; ".join(errors))
    bot = ClokyBot(config)
    application = bot.build_application()
    application.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=False,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
