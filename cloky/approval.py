from __future__ import annotations

import asyncio
import html
import secrets
from dataclasses import dataclass, field
from typing import Any

from .security import summarize_tool_input


@dataclass(slots=True)
class PendingApproval:
    request_id: str
    user_id: int
    chat_id: int
    tool_name: str
    input_data: dict[str, Any]
    context: Any
    future: asyncio.Future[str]


@dataclass(slots=True)
class PendingQuestion:
    request_id: str
    user_id: int
    chat_id: int
    question: dict[str, Any]
    future: asyncio.Future[Any]
    selected: set[int] = field(default_factory=set)
    message_id: int | None = None


class ApprovalBroker:
    """Bridges SDK permission callbacks to Telegram inline interactions."""

    def __init__(self, bot: Any, timeout_seconds: int = 900):
        self.bot = bot
        self.timeout_seconds = timeout_seconds
        self.approvals: dict[str, PendingApproval] = {}
        self.questions: dict[str, PendingQuestion] = {}
        self.question_by_user: dict[int, str] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _id() -> str:
        return secrets.token_hex(4)

    async def can_use_tool(self, user_id: int, chat_id: int, tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        if tool_name == "AskUserQuestion":
            return await self._ask_questions(user_id, chat_id, input_data)
        return await self._ask_approval(user_id, chat_id, tool_name, input_data, context)

    async def _ask_approval(self, user_id: int, chat_id: int, tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        request_id = self._id()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = PendingApproval(request_id, user_id, chat_id, tool_name, input_data, context, future)
        async with self._lock:
            self.approvals[request_id] = pending

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Una vez", callback_data=f"ap:{request_id}:once"),
                InlineKeyboardButton("✅ Siempre", callback_data=f"ap:{request_id}:always"),
            ],
            [
                InlineKeyboardButton("❌ Denegar", callback_data=f"ap:{request_id}:deny"),
                InlineKeyboardButton("⏹ Detener", callback_data=f"ap:{request_id}:stop"),
            ],
        ])
        summary = html.escape(summarize_tool_input(tool_name, input_data))
        await self.bot.send_message(
            chat_id=chat_id,
            text=f"Claude solicita permiso para <b>{tool_name}</b>\n\n<pre>{summary}</pre>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        try:
            choice = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError:
            choice = "deny"
        finally:
            async with self._lock:
                self.approvals.pop(request_id, None)

        if choice == "once":
            return PermissionResultAllow(updated_input=input_data)
        if choice == "always":
            suggestions = getattr(context, "suggestions", None) or []
            persist = [s for s in suggestions if getattr(s, "destination", None) == "localSettings"]
            return PermissionResultAllow(updated_input=input_data, updated_permissions=persist)
        if choice == "stop":
            return PermissionResultDeny(message="El usuario detuvo la tarea.", interrupt=True)
        return PermissionResultDeny(message="El usuario denegó esta acción.")

    async def _ask_questions(self, user_id: int, chat_id: int, input_data: dict[str, Any]) -> Any:
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        questions = input_data.get("questions")
        if not isinstance(questions, list) or not questions:
            return PermissionResultDeny(message="AskUserQuestion llegó sin preguntas válidas.")

        answers: dict[str, Any] = {}
        try:
            for question in questions:
                if not isinstance(question, dict):
                    continue
                answer = await self._ask_one_question(user_id, chat_id, question)
                answers[str(question.get("question", "Pregunta"))] = answer
        except TimeoutError:
            return PermissionResultDeny(message="La pregunta expiró sin respuesta del usuario.")

        return PermissionResultAllow(updated_input={"questions": questions, "answers": answers})

    async def _ask_one_question(self, user_id: int, chat_id: int, question: dict[str, Any]) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        request_id = self._id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        pending = PendingQuestion(request_id, user_id, chat_id, question, future)
        async with self._lock:
            old = self.question_by_user.get(user_id)
            if old and old in self.questions:
                previous = self.questions.pop(old)
                if not previous.future.done():
                    previous.future.set_exception(RuntimeError("Pregunta reemplazada por una nueva"))
            self.questions[request_id] = pending
            self.question_by_user[user_id] = request_id

        markup = self._question_markup(pending)
        text = self._question_text(question)
        sent = await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        pending.message_id = sent.message_id
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        finally:
            async with self._lock:
                self.questions.pop(request_id, None)
                if self.question_by_user.get(user_id) == request_id:
                    self.question_by_user.pop(user_id, None)

    @staticmethod
    def _question_text(question: dict[str, Any]) -> str:
        header = str(question.get("header", "Pregunta")).strip()
        text = str(question.get("question", "Claude necesita una respuesta.")).strip()
        options = question.get("options") or []
        descriptions = []
        for index, option in enumerate(options):
            if isinstance(option, dict):
                label = option.get("label", index + 1)
                desc = option.get("description", "")
                if desc:
                    descriptions.append(f"{index + 1}. {label}: {desc}")
        suffix = "\n\n" + "\n".join(descriptions) if descriptions else ""
        return f"{header}\n\n{text}{suffix}\n\nTambién podés responder con texto libre."

    @staticmethod
    def _question_markup(pending: PendingQuestion) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        options = pending.question.get("options") or []
        multi = bool(pending.question.get("multiSelect"))
        rows = []
        for index, option in enumerate(options):
            if not isinstance(option, dict):
                continue
            label = str(option.get("label", index + 1))
            prefix = "☑️ " if index in pending.selected else ""
            action = "toggle" if multi else "pick"
            rows.append([InlineKeyboardButton(prefix + label, callback_data=f"q:{pending.request_id}:{action}:{index}")])
        if multi:
            rows.append([
                InlineKeyboardButton("✅ Listo", callback_data=f"q:{pending.request_id}:done:0"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"q:{pending.request_id}:cancel:0"),
            ])
        else:
            rows.append([InlineKeyboardButton("❌ Cancelar", callback_data=f"q:{pending.request_id}:cancel:0")])
        return InlineKeyboardMarkup(rows)

    async def handle_callback(self, query: Any, user_id: int) -> bool:
        data = query.data or ""
        if data.startswith("ap:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                return False
            request_id, choice = parts[1], parts[2]
            pending = self.approvals.get(request_id)
            if not pending or pending.user_id != user_id:
                await query.answer("Solicitud vencida", show_alert=True)
                return True
            if not pending.future.done():
                pending.future.set_result(choice)
            await query.answer("Decisión registrada")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return True

        if data.startswith("q:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                return False
            request_id, action, raw_index = parts[1], parts[2], parts[3]
            pending = self.questions.get(request_id)
            if not pending or pending.user_id != user_id:
                await query.answer("Pregunta vencida", show_alert=True)
                return True
            options = pending.question.get("options") or []
            if action == "cancel":
                if not pending.future.done():
                    pending.future.set_exception(TimeoutError("Pregunta cancelada"))
                await query.answer("Cancelado")
                return True
            try:
                index = int(raw_index)
            except ValueError:
                index = -1
            if action == "pick" and 0 <= index < len(options):
                option = options[index]
                label = option.get("label", str(index + 1)) if isinstance(option, dict) else str(index + 1)
                if not pending.future.done():
                    pending.future.set_result(label)
                await query.answer(str(label))
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return True
            if action == "toggle" and 0 <= index < len(options):
                if index in pending.selected:
                    pending.selected.remove(index)
                else:
                    pending.selected.add(index)
                await query.answer("Selección actualizada")
                try:
                    await query.edit_message_reply_markup(reply_markup=self._question_markup(pending))
                except Exception:
                    pass
                return True
            if action == "done":
                labels = []
                for idx in sorted(pending.selected):
                    if 0 <= idx < len(options) and isinstance(options[idx], dict):
                        labels.append(options[idx].get("label", str(idx + 1)))
                if not labels:
                    await query.answer("Elegí al menos una opción", show_alert=True)
                    return True
                if not pending.future.done():
                    pending.future.set_result(labels)
                await query.answer("Respuesta enviada")
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return True
        return False

    async def consume_text(self, user_id: int, text: str) -> bool:
        request_id = self.question_by_user.get(user_id)
        if not request_id:
            return False
        pending = self.questions.get(request_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(text.strip())
        return True
