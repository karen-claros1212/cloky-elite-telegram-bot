from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approval import ApprovalBroker
from .config import Config
from .models import RuntimeStatus, TaskResult, TaskTelemetry, UserState
from .render import ResponseAccumulator
from .security import clean_visible_text
from .state import StateStore

logger = logging.getLogger("cloky.runtime")


@dataclass(slots=True)
class RuntimeCallbacks:
    on_status: Callable[[str], Awaitable[None]]
    on_preview: Callable[[str], Awaitable[None]]
    on_session: Callable[[str], Awaitable[None]]


class ClaudeRuntime:
    """One persistent ClaudeSDKClient for one Telegram user/project."""

    def __init__(
        self,
        config: Config,
        store: StateStore,
        broker: ApprovalBroker,
        user_state: UserState,
        chat_id: int,
    ):
        self.config = config
        self.store = store
        self.broker = broker
        self.user_id = user_state.user_id
        self.chat_id = chat_id
        self.project = Path(user_state.project_path).resolve()
        self.session_id = user_state.session_id
        self.mode = user_state.mode
        self.model = user_state.model or config.claude_model
        self.fork_next = user_state.fork_next
        self.client: Any = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._busy = False
        self._status = "idle"
        self._started_at: float | None = None
        self.last_used = time.time()

    def matches(self, state: UserState, chat_id: int) -> bool:
        return (
            self.chat_id == chat_id
            and self.project == Path(state.project_path).resolve()
            and self.session_id == state.session_id
            and self.fork_next == state.fork_next
        )

    async def _build_client(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
            return await self.broker.can_use_tool(self.user_id, self.chat_id, tool_name, input_data, context)

        def stderr_callback(line: str) -> None:
            value = line.strip()
            if value:
                logger.warning("claude_stderr %s", value[:1000], extra={"user_id": self.user_id, "session_id": self.session_id})

        options = ClaudeAgentOptions(
            cli_path=self.config.claude_cli_path,
            cwd=self.project,
            model=self.model,
            permission_mode=self.mode,
            resume=self.session_id,
            fork_session=bool(self.fork_next and self.session_id),
            max_turns=self.config.max_turns,
            include_partial_messages=True,
            include_hook_events=self.config.include_hook_events,
            setting_sources=["user", "project", "local"],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "You are being controlled through a Telegram engineering console. "
                    "Always produce a concise user-visible final response. Never emit synthetic "
                    "messages such as 'No response requested.' Keep tool details out of the final "
                    "answer unless they are relevant to the result."
                ),
            },
            env=self.config.claude_env(),
            can_use_tool=can_use_tool,
            disallowed_tools=self.config.disallowed_tools,
            enable_file_checkpointing=self.config.enable_file_checkpointing,
            stderr=stderr_callback,
            user=str(self.user_id),
        )
        return ClaudeSDKClient(options=options)

    async def connect(self) -> None:
        if self._connected and self.client is not None:
            return
        self.client = await self._build_client()
        await self.client.connect()
        self._connected = True
        self.last_used = time.time()
        logger.info(
            "runtime_connected",
            extra={"user_id": self.user_id, "session_id": self.session_id, "project": str(self.project)},
        )

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        self._connected = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("runtime_disconnect_failed", extra={"user_id": self.user_id})

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.client is not None and self._connected:
            await self.client.set_permission_mode(mode)

    async def set_model(self, model: str | None) -> None:
        self.model = model
        if self.client is not None and self._connected:
            await self.client.set_model(model)

    async def interrupt(self) -> bool:
        if not self._busy or self.client is None:
            return False
        await self.client.interrupt()
        self._status = "interrupting"
        return True

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            user_id=self.user_id,
            project_path=str(self.project),
            session_id=self.session_id,
            mode=self.mode,
            model=self.model,
            busy=self._busy,
            status=self._status,
            started_at=self._started_at,
        )

    async def query(self, prompt: str, callbacks: RuntimeCallbacks) -> TaskResult:
        async with self._lock:
            self._busy = True
            self._status = "starting"
            self._started_at = time.time()
            self.last_used = self._started_at
            task_id = self.store.start_task(self.user_id, str(self.project), self.session_id)
            accumulator = ResponseAccumulator()
            telemetry = TaskTelemetry(session_id=self.session_id)
            assistant_segments: list[str] = []
            error: str | None = None
            try:
                await self.connect()
                await self.client.set_permission_mode(self.mode)
                if self.model:
                    await self.client.set_model(self.model)
                self._status = "running"
                await callbacks.on_status("Claude Code está trabajando…")
                await self.client.query(prompt)

                async for message in self.client.receive_response():
                    class_name = type(message).__name__
                    message_session_id = getattr(message, "session_id", None)
                    if isinstance(message_session_id, str) and message_session_id:
                        telemetry.session_id = message_session_id
                        if message_session_id != self.session_id:
                            self.session_id = message_session_id
                            self.fork_next = False
                            self.store.update_user(self.user_id, session_id=message_session_id, fork_next=False)
                            await callbacks.on_session(message_session_id)

                    if class_name == "StreamEvent":
                        event = getattr(message, "event", None)
                        delta_text = self._stream_delta(event)
                        if delta_text is not None:
                            accumulator.add_delta(delta_text)
                            preview = accumulator.preview()
                            if preview:
                                await callbacks.on_preview(preview)
                        continue

                    if class_name == "AssistantMessage":
                        content = getattr(message, "content", None) or []
                        texts: list[str] = []
                        for block in content:
                            block_name = type(block).__name__
                            if block_name == "TextBlock":
                                text = clean_visible_text(getattr(block, "text", None))
                                if text:
                                    texts.append(text)
                            elif block_name == "ToolUseBlock":
                                tool_name = str(getattr(block, "name", "tool"))
                                self._status = f"tool:{tool_name}"
                                await callbacks.on_status(f"Usando {tool_name}…")
                            elif block_name == "ToolResultBlock":
                                await callbacks.on_status("Herramienta completada; Claude continúa…")
                        if texts:
                            joined = "\n\n".join(texts)
                            accumulator.add_assistant(joined)
                            assistant_segments.append(joined)
                        continue

                    if class_name == "ResultMessage":
                        telemetry.subtype = str(getattr(message, "subtype", "")) or None
                        telemetry.num_turns = int(getattr(message, "num_turns", 0) or 0)
                        telemetry.duration_ms = int(getattr(message, "duration_ms", 0) or 0)
                        telemetry.cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                        telemetry.model_usage = getattr(message, "model_usage", None) or {}
                        self._read_usage(getattr(message, "usage", None), telemetry)
                        accumulator.set_result(getattr(message, "result", None))
                        if getattr(message, "is_error", False) and not error:
                            error = str(getattr(message, "result", "Claude Code reportó un error"))
                        continue

                    if class_name in {"RateLimitEvent", "TaskNotificationMessage", "HookEventMessage"}:
                        await callbacks.on_status(self._status_for_event(class_name, message))

                final_text = accumulator.final_text()
                if accumulator.sentinel_detected and not final_text:
                    final_text = (
                        "La sesión produjo una respuesta sintética vacía. No se reintentó para evitar un ciclo. "
                        "Usá /new para continuar en una sesión limpia."
                    )
                elif not final_text:
                    final_text = "Claude Code terminó sin una respuesta visible. Revisá /doctor y los logs del turno."

                status = "success" if not error else "error"
                self.store.finish_task(
                    task_id,
                    status=status,
                    session_id=telemetry.session_id,
                    input_tokens=telemetry.input_tokens,
                    output_tokens=telemetry.output_tokens,
                    cost_usd=telemetry.cost_usd,
                    error=error,
                )
                return TaskResult(
                    text=final_text,
                    telemetry=telemetry,
                    sentinel_detected=accumulator.sentinel_detected,
                    error=error,
                )
            except asyncio.CancelledError:
                error = "Tarea cancelada"
                self.store.finish_task(task_id, status="cancelled", session_id=self.session_id, error=error)
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "runtime_query_failed",
                    extra={"user_id": self.user_id, "session_id": self.session_id, "project": str(self.project)},
                )
                self.store.finish_task(task_id, status="error", session_id=self.session_id, error=error)
                return TaskResult(text=f"Error ejecutando Claude Code: {error}", telemetry=telemetry, error=error)
            finally:
                self._busy = False
                self._status = "idle"
                self._started_at = None
                self.last_used = time.time()

    @staticmethod
    def _stream_delta(event: Any) -> str | None:
        if not isinstance(event, dict):
            return None
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            value = delta.get("text")
            return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _read_usage(usage: Any, telemetry: TaskTelemetry) -> None:
        if not isinstance(usage, dict):
            return
        telemetry.input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        telemetry.output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        telemetry.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        telemetry.cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
        telemetry.context_window = int(usage.get("context_window", 0) or 0)

    @staticmethod
    def _status_for_event(class_name: str, message: Any) -> str:
        if class_name == "RateLimitEvent":
            return "Claude Code reportó estado de límite de uso"
        if class_name == "TaskNotificationMessage":
            return "Actualización de tarea en segundo plano"
        if class_name == "HookEventMessage":
            event = getattr(message, "hook_event_name", None) or getattr(message, "event", None)
            return f"Evento: {event or 'hook'}"
        return "Procesando…"


class RuntimeManager:
    def __init__(self, config: Config, store: StateStore):
        self.config = config
        self.store = store
        self._runtimes: dict[int, ClaudeRuntime] = {}
        self._lock = asyncio.Lock()

    async def get(self, state: UserState, chat_id: int, broker: ApprovalBroker) -> ClaudeRuntime:
        async with self._lock:
            current = self._runtimes.get(state.user_id)
            if current and current.matches(state, chat_id):
                if current.mode != state.mode:
                    await current.set_mode(state.mode)
                if current.model != state.model:
                    await current.set_model(state.model)
                return current
            if current:
                await current.disconnect()
            runtime = ClaudeRuntime(self.config, self.store, broker, state, chat_id)
            self._runtimes[state.user_id] = runtime
            return runtime

    async def reset_user(self, user_id: int) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(user_id, None)
        if runtime:
            await runtime.disconnect()

    async def interrupt(self, user_id: int) -> bool:
        runtime = self._runtimes.get(user_id)
        return await runtime.interrupt() if runtime else False

    async def set_mode(self, user_id: int, mode: str) -> None:
        runtime = self._runtimes.get(user_id)
        if runtime:
            await runtime.set_mode(mode)

    def status(self, user_id: int) -> RuntimeStatus | None:
        runtime = self._runtimes.get(user_id)
        return runtime.status() if runtime else None

    async def evict_idle(self) -> int:
        cutoff = time.time() - self.config.idle_client_ttl_seconds
        stale: list[tuple[int, ClaudeRuntime]] = []
        async with self._lock:
            for user_id, runtime in list(self._runtimes.items()):
                if not runtime.status().busy and runtime.last_used < cutoff:
                    stale.append((user_id, self._runtimes.pop(user_id)))
        for _, runtime in stale:
            await runtime.disconnect()
        return len(stale)

    async def close(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        await asyncio.gather(*(runtime.disconnect() for runtime in runtimes), return_exceptions=True)
