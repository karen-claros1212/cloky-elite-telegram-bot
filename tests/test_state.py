from pathlib import Path

from cloky.state import StateStore


def test_user_state_round_trip(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.db", project, "bypassPermissions", "sonnet")
    state = store.get_user(7)
    assert state.project_path == str(project.resolve())
    assert state.session_id is None
    state = store.update_user(7, session_id="abc", mode="plan", fork_next=True)
    loaded = store.get_user(7)
    assert loaded.session_id == "abc"
    assert loaded.mode == "plan"
    assert loaded.fork_next is True
    store.close()


def test_task_metrics(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.db", project, "default", None)
    task_id = store.start_task(1, str(project), None)
    store.finish_task(task_id, status="success", session_id="sid", input_tokens=100, output_tokens=30, cost_usd=0.1)
    last = store.last_task(1)
    assert last is not None
    assert last["input_tokens"] == 100
    assert last["output_tokens"] == 30
    assert last["session_id"] == "sid"
    store.close()
