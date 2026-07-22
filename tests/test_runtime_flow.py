import asyncio
from pathlib import Path
from types import SimpleNamespace

from cloky.config import Config
from cloky.models import UserState
from cloky.runtime import ClaudeRuntime, RuntimeCallbacks
from cloky.state import StateStore


class TextBlock:
    def __init__(self, text):
        self.text = text


class StreamEvent:
    def __init__(self, text, session_id="sid-new"):
        self.session_id = session_id
        self.event = {"delta": {"type": "text_delta", "text": text}}


class AssistantMessage:
    def __init__(self, text, session_id="sid-new"):
        self.session_id = session_id
        self.content = [TextBlock(text)]


class ResultMessage:
    def __init__(self, result, session_id="sid-new", is_error=False):
        self.session_id = session_id
        self.result = result
        self.subtype = "success" if not is_error else "error_during_execution"
        self.is_error = is_error
        self.num_turns = 1
        self.duration_ms = 100
        self.total_cost_usd = 0.0
        self.usage = {"input_tokens": 100, "output_tokens": 30}
        self.model_usage = {}


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.mode = None
        self.model = None
        self.queries = []

    async def set_permission_mode(self, mode):
        self.mode = mode

    async def set_model(self, model):
        self.model = model

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def interrupt(self):
        pass

    async def disconnect(self):
        pass


class Broker:
    pass


def make_config(tmp_path: Path) -> Config:
    project = tmp_path / "project"
    project.mkdir()
    return Config(
        base_dir=tmp_path,
        telegram_token="token",
        allowed_user_ids={1},
        projects={"project": project},
        default_project=project,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        uploads_dir=tmp_path / "uploads",
        claude_cli_path="claude",
        claude_model="sonnet",
        default_mode="bypassPermissions",
        anthropic_base_url="http://127.0.0.1:8080",
        anthropic_auth_token="local",
        anthropic_api_key="",
        anthropic_model="sonnet",
    )


def test_runtime_uses_result_once_and_persists_session(tmp_path: Path):
    async def scenario():
        config = make_config(tmp_path)
        store = StateStore(config.db_path, config.default_project, config.default_mode, config.claude_model)
        state = UserState(1, str(config.default_project), None, "bypassPermissions", "sonnet")
        runtime = ClaudeRuntime(config, store, Broker(), state, 10)
        runtime.client = FakeClient([
            StreamEvent("Ho"),
            StreamEvent("la"),
            AssistantMessage("Hola"),
            ResultMessage("Hola"),
        ])
        runtime._connected = True
        previews = []
        sessions = []
        result = await runtime.query(
            "Hola",
            RuntimeCallbacks(
                on_status=lambda _: asyncio.sleep(0),
                on_preview=lambda text: previews.append(text) or asyncio.sleep(0),
                on_session=lambda sid: sessions.append(sid) or asyncio.sleep(0),
            ),
        )
        assert result.text == "Hola"
        assert result.telemetry.input_tokens == 100
        assert result.telemetry.output_tokens == 30
        assert store.get_user(1).session_id == "sid-new"
        assert previews[-1] == "Hola"
        store.close()

    asyncio.run(scenario())


def test_runtime_stops_retry_cycle_on_sentinel(tmp_path: Path):
    async def scenario():
        config = make_config(tmp_path)
        store = StateStore(config.db_path, config.default_project, config.default_mode, config.claude_model)
        state = UserState(1, str(config.default_project), None, "bypassPermissions", "sonnet")
        runtime = ClaudeRuntime(config, store, Broker(), state, 10)
        runtime.client = FakeClient([ResultMessage("No response requested.")])
        runtime._connected = True
        result = await runtime.query(
            "Hola",
            RuntimeCallbacks(
                on_status=lambda _: asyncio.sleep(0),
                on_preview=lambda _: asyncio.sleep(0),
                on_session=lambda _: asyncio.sleep(0),
            ),
        )
        assert result.sentinel_detected is True
        assert "No se reintentó" in result.text
        assert runtime.client.queries == ["Hola"]
        store.close()

    asyncio.run(scenario())
