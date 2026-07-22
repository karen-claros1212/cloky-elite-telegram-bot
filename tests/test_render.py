from cloky.render import ResponseAccumulator, split_telegram


def test_stream_deltas_are_concatenated_without_separators():
    acc = ResponseAccumulator()
    acc.add_delta("Ho")
    acc.add_delta("la")
    assert acc.partial_text == "Hola"
    assert acc.final_text() == "Hola"


def test_result_is_authoritative_and_not_duplicated():
    acc = ResponseAccumulator()
    acc.add_delta("Ho")
    acc.add_delta("la")
    acc.add_assistant("Hola")
    acc.set_result("Hola")
    assert acc.final_text() == "Hola"


def test_sentinel_is_control_signal():
    acc = ResponseAccumulator()
    acc.set_result("No response requested.")
    assert acc.sentinel_detected is True
    assert acc.final_text() == ""


def test_split_telegram_preserves_text():
    text = "x" * 9000
    chunks = split_telegram(text, limit=3900)
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert "".join(chunks) == text
