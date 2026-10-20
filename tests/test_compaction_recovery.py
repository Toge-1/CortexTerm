from copy import deepcopy

import pytest

from cortexterm.agent_loop import run_agent_turn
from cortexterm.context_manager import ContextManager
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult
from cortexterm.types import AgentStep


def history():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old request " * 200},
        {"role": "assistant", "content": "old answer " * 200},
        {"role": "user", "content": "current task " * 8},
    ]


class RecoveryModel:
    def __init__(self, summaries):
        self.summaries = iter(summaries)
        self.summary_calls = 0
        self.next_calls = []

    def summarize(self, prompt, *, max_tokens):
        self.summary_calls += 1
        result = next(self.summaries)
        if isinstance(result, Exception):
            raise result
        return result

    def next(self, messages):
        self.next_calls.append(deepcopy(messages))
        return AgentStep(type="assistant", content="done")


@pytest.mark.parametrize(
    "failure", [RuntimeError("temporarily unavailable"), "", "too long " * 500],
    ids=["transient", "empty", "oversized"],
)
def test_retry_recovers_without_committing_failed_checkpoint(failure):
    original = history()
    manager = ContextManager(context_window=1000, reserve_tokens=200, keep_recent_tokens=20)
    model = RecoveryModel([failure, "small checkpoint"])
    events = []
    result = run_agent_turn(
        model=model, tools=ToolRegistry([]), messages=original, cwd=".", context_manager=manager,
        on_context_event=lambda event, payload: events.append(event),
    )
    assert result[-1]["content"] == "done"
    assert model.summary_calls == 2
    assert len(model.next_calls) == 1
    assert events == ["compact_start", "compact_retry", "compact_done"]
    assert len(manager.compaction_history) == 1
    checkpoint = next(m for m in result if m.get("isCompactionSummary"))
    assert checkpoint["compactedMessages"] == original[1:3]
    assert original == history()


@pytest.mark.parametrize("summary", ["", "oversized " * 1000], ids=["empty", "oversized"])
def test_invalid_summaries_pause_and_preserve_previous_archive(summary):
    old_archive = [{"role": "user", "content": "older evidence"}]
    original = history()
    original.insert(1, {"role": "system", "content": "previous checkpoint",
                        "isCompactionSummary": True, "compactedMessages": old_archive})
    before = deepcopy(original)
    manager = ContextManager(context_window=1000, reserve_tokens=200, keep_recent_tokens=20)
    model = RecoveryModel([summary] * 3)
    result = run_agent_turn(model=model, tools=ToolRegistry([]), messages=original, cwd=".", context_manager=manager)
    assert model.summary_calls == 3
    assert not model.next_calls
    assert result[:-1] == before
    assert original == before
    assert manager.compaction_history == []


def test_no_safe_cut_pauses_without_retrying_or_calling_summary_api():
    model = RecoveryModel([])
    manager = ContextManager(context_window=500, reserve_tokens=100, keep_recent_tokens=20)
    original = [{"role": "user", "content": "single enormous message " * 300}]
    result = run_agent_turn(model=model, tools=ToolRegistry([]), messages=original, cwd=".", context_manager=manager)
    assert "No safe" in result[-1]["content"]
    assert result[:-1] == original
    assert model.summary_calls == 0
    assert model.next_calls == []


@pytest.mark.parametrize("strategy", ["pi", "legacy"])
def test_disabled_compaction_still_blocks_oversized_model_requests(strategy):
    model = RecoveryModel([])
    manager = ContextManager(context_window=500, reserve_tokens=100, enabled=False, strategy=strategy)
    result = run_agent_turn(model=model, tools=ToolRegistry([]), messages=history(), cwd=".", context_manager=manager)
    assert "disabled" in result[-1]["content"]
    assert not model.next_calls


def test_disabled_compaction_allows_normal_sized_requests():
    model = RecoveryModel([])
    manager = ContextManager(context_window=500, reserve_tokens=100, enabled=False)
    result = run_agent_turn(model=model, tools=ToolRegistry([]), messages=[{"role": "user", "content": "hi"}], cwd=".", context_manager=manager)
    assert result[-1]["content"] == "done"
    assert len(model.next_calls) == 1


def test_failed_compaction_after_tool_execution_keeps_call_and_result():
    class ToolModel(RecoveryModel):
        def next(self, messages):
            self.next_calls.append(deepcopy(messages))
            return AgentStep(type="tool_calls", calls=[{"id": "A", "toolName": "read_log", "input": {}}])

    model = ToolModel([RuntimeError("offline")] * 3)
    manager = ContextManager(context_window=500, reserve_tokens=100, keep_recent_tokens=20)
    registry = ToolRegistry([ToolDefinition(
        name="read_log", description="log", input_schema={"type": "object"},
        validator=lambda data: data, run=lambda data, ctx: ToolResult(ok=True, output="large log " * 500),
    )])
    result = run_agent_turn(model=model, tools=registry, messages=[{"role": "user", "content": "read log"}], cwd=".", context_manager=manager)
    assert len(model.next_calls) == 1
    assert [m["role"] for m in result] == ["user", "assistant_tool_call", "tool_result", "assistant"]
    assert result[1]["toolUseId"] == result[2]["toolUseId"] == "A"
    assert "large log" in result[2]["content"]


def test_oversized_retained_suffix_is_not_sent_even_with_successful_summary():
    model = RecoveryModel(["checkpoint"] * 3)
    manager = ContextManager(context_window=500, reserve_tokens=100, keep_recent_tokens=20)
    original = [{"role": "user", "content": "old " * 500}, {"role": "assistant", "content": "uncuttable " * 500}]
    result = run_agent_turn(model=model, tools=ToolRegistry([]), messages=original, cwd=".", context_manager=manager)
    assert not model.next_calls
    assert result[:-1] == original
    assert manager.compaction_history == []


def test_split_turn_failure_does_not_commit_partial_history_summary():
    original = [
        {"role": "user", "content": "old " * 500},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "recent " * 20},
    ]
    before = deepcopy(original)
    manager = ContextManager(context_window=500, reserve_tokens=100, keep_recent_tokens=20, messages=original)
    model = RecoveryModel(["history checkpoint", RuntimeError("prefix failure")])
    with pytest.raises(RuntimeError, match="prefix failure"):
        manager.compact_messages(lambda prompt: model.summarize(prompt, max_tokens=80))
    assert manager.messages == before
    assert manager.compaction_history == []


def test_tui_releases_input_and_keeps_messages_after_failed_compaction(tmp_path, monkeypatch):
    from cortexterm.permissions import PermissionManager
    from cortexterm.tui.agent_turn import start_agent_turn
    from cortexterm.tui.event_loop import _harvest_completed_agent_turn
    from cortexterm.tui.state import ScreenState, TtyAppArgs

    monkeypatch.setattr("cortexterm.permissions._read_permission_store", lambda: {})
    model = RecoveryModel([RuntimeError("summary service offline")] * 3)
    old_message = {"role": "user", "content": "old evidence " * 10000}
    args = TtyAppArgs(
        runtime={}, tools=ToolRegistry([]), model=model,
        messages=[{"role": "system", "content": "sys"}, old_message],
        cwd=str(tmp_path), permissions=PermissionManager(str(tmp_path)),
        context_mgr=ContextManager(context_window=20000, reserve_tokens=200, keep_recent_tokens=20),
    )
    state = ScreenState(input="draft for after recovery", cursor_offset=5)
    statuses = []
    start_agent_turn(args, state, lambda: statuses.append(state.status), "current task " * 8)
    state.agent_thread.join(timeout=2)
    assert not state.agent_thread.is_alive()
    assert model.summary_calls == 3 and not model.next_calls
    assert any(status and "Retrying context compression" in status for status in statuses)
    assert state.agent_result["done"] and state.is_busy
    assert _harvest_completed_agent_turn(args, state)
    assert not state.is_busy
    assert state.input == "draft for after recovery" and state.cursor_offset == 5
    assert args.messages[1] == old_message
    assert "Turn paused" in args.messages[-1]["content"]
    assert any("Next model request was not sent" in entry.body for entry in state.transcript)
