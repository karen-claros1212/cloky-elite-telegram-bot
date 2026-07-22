from cloky.models import TaskTelemetry
from cloky.runtime import ClaudeRuntime


def test_stream_delta():
    assert ClaudeRuntime._stream_delta({"delta": {"type": "text_delta", "text": "abc"}}) == "abc"
    assert ClaudeRuntime._stream_delta({"delta": {"type": "input_json_delta", "partial_json": "{}"}}) is None


def test_usage_result_is_authoritative():
    telemetry = TaskTelemetry()
    ClaudeRuntime._read_usage({"input_tokens": 100, "output_tokens": 30}, telemetry)
    assert telemetry.input_tokens == 100
    assert telemetry.output_tokens == 30
