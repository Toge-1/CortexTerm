from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from cortexterm.agent_loop import run_agent_turn
from cortexterm.context_manager import ContextManager
from cortexterm.prompt import build_runtime_time_context
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult
from cortexterm.types import AgentStep


def test_runtime_clock_contains_local_iso_timestamp_and_utc_offset(monkeypatch):
    local_now = datetime(2026, 10, 21, 0, 1, 2, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    class Clock:
        @staticmethod
        def now():
            class LocalTime:
                def astimezone(self):
                    return local_now
            return LocalTime()

    monkeypatch.setattr("cortexterm.prompt.datetime", Clock)
    context = build_runtime_time_context()
    assert "2026-10-21T00:01:02+05:30" in context
    assert "relative dates" in context


@pytest.mark.parametrize("base", [[], [{"role": "system", "content": "existing policy"}]])
def test_clock_refreshes_after_tools_without_growing_or_saving_context(monkeypatch, base):
    snapshots = []
    # Each step snapshots time for budgeting and again just before sending.
    clocks = iter(["before midnight", "2026-10-20T23:59:59+08:00",
                   "after midnight", "2026-10-21T00:00:01+08:00"])
    monkeypatch.setattr("cortexterm.agent_loop.build_runtime_time_context", lambda: next(clocks))

    class Model:
        def next(self, messages):
            snapshots.append(deepcopy(messages))
            if len(snapshots) == 1:
                return AgentStep(type="tool_calls", calls=[{"id": "1", "toolName": "echo", "input": {}}])
            return AgentStep(type="assistant", content="done")

    tools = ToolRegistry([ToolDefinition(
        name="echo", description="echo", input_schema={"type": "object"},
        validator=lambda data: data, run=lambda data, context: ToolResult(ok=True, output="ok"),
    )])
    original = deepcopy(base) + [{"role": "user", "content": "what happened yesterday?"}]
    before = deepcopy(original)
    result = run_agent_turn(model=Model(), tools=tools, messages=original, cwd=".")

    for snapshot, expected in zip(snapshots, ["2026-10-20T23:59:59+08:00", "2026-10-21T00:00:01+08:00"]):
        runtime = [m for m in snapshot if m.get("isRuntimeTimeContext")]
        assert len(runtime) == 1
        assert runtime[0]["role"] == "system" and runtime[0]["content"] == expected
    assert original == before
    assert not any(m.get("isRuntimeTimeContext") for m in result)
    assert result[:len(original)] == original


def test_clock_is_included_in_budget_and_removed_after_blocking():
    class Model:
        def next(self, messages):
            pytest.fail("The full request exceeds the input budget")

    original = [{"role": "user", "content": "hi"}]
    manager = ContextManager(context_window=40, reserve_tokens=20, enabled=False)
    result = run_agent_turn(model=Model(), tools=ToolRegistry([]), messages=original,
                            cwd=".", context_manager=manager)
    assert "disabled" in result[-1]["content"]
    assert result[:-1] == original
    assert manager.messages == result


def test_clock_refreshes_after_compaction_and_on_the_next_turn(monkeypatch):
    clocks = iter(["2026-10-21T23:59:59+08:00", "2026-10-22T00:00:01+08:00",
                   "2026-10-23T09:00:00+08:00", "2026-10-23T09:00:01+08:00"])
    monkeypatch.setattr("cortexterm.agent_loop.build_runtime_time_context", lambda: next(clocks))
    snapshots = []

    class Model:
        def summarize(self, prompt, *, max_tokens):
            return "Earlier conversation checkpoint"

        def next(self, messages):
            snapshots.append(deepcopy(messages))
            return AgentStep(type="assistant", content="done")

    original = [
        {"role": "system", "content": "existing policy"},
        {"role": "user", "content": "old request " * 200},
        {"role": "assistant", "content": "old answer " * 200},
        {"role": "user", "content": "current task"},
    ]
    manager = ContextManager(context_window=1000, reserve_tokens=200, keep_recent_tokens=20)
    result = run_agent_turn(model=Model(), tools=ToolRegistry([]), messages=original,
                            cwd=".", context_manager=manager)
    assert any(m.get("isCompactionSummary") for m in result)
    assert not any(m.get("isRuntimeTimeContext") for m in manager.messages)
    result.append({"role": "user", "content": "next day's task"})
    run_agent_turn(model=Model(), tools=ToolRegistry([]), messages=result, cwd=".")
    assert [next(m["content"] for m in snapshot if m.get("isRuntimeTimeContext"))
            for snapshot in snapshots] == ["2026-10-22T00:00:01+08:00", "2026-10-23T09:00:01+08:00"]


def test_clock_context_is_not_captured_as_public_memory():
    captured = []

    class Memory:
        def capture(self, recorder):
            captured.append(recorder)

    class Model:
        def next(self, messages):
            assert any(m.get("isRuntimeTimeContext") for m in messages)
            return AgentStep(type="assistant", content="done")

    result = run_agent_turn(model=Model(), tools=ToolRegistry([]),
                            messages=[{"role": "user", "content": "hello"}],
                            cwd=".", memory_service=Memory())
    assert [m["role"] for m in result] == ["user", "assistant"]
    assert [m["role"] for m in captured[0].events] == ["user", "assistant"]
    assert "Current local time" not in str(captured[0].events)


def test_interrupt_cleans_clock_from_context_manager():
    class Model:
        def next(self, messages):
            raise KeyboardInterrupt

    original = [{"role": "user", "content": "hello"}]
    manager = ContextManager()
    with pytest.raises(KeyboardInterrupt):
        run_agent_turn(model=Model(), tools=ToolRegistry([]), messages=original,
                       cwd=".", context_manager=manager)
    assert manager.messages == original
