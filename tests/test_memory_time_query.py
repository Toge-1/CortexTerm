from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cortexterm import agent_loop
from cortexterm.memory_v2 import service as service_module
from cortexterm.memory_v2.retrieval import parse_time_range
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.memory_v2.store import make_record
from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.tools.memory_tools import memory_read_tool, memory_search_tool
from cortexterm.types import AgentStep


NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
START = "2026-10-20T18:00:00+08:00"
END = "2026-10-20T11:00:00Z"
START_STAMP = datetime(2026, 10, 20, 10, tzinfo=timezone.utc).timestamp()
END_STAMP = START_STAMP + 3600


@pytest.fixture
def memory(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", FixedDatetime)
    root = tmp_path / "repo"
    root.mkdir()
    return MemoryService(root, tmp_path / "memory.sqlite3")


def capture(service, identity, text, recorded_at, *, tool_output=None):
    recorder = TurnRecorder(text, session="original-session", turn_id=identity)
    recorder.recorded_at = recorded_at
    if tool_output is not None:
        recorder.append({"role": "assistant_tool_call", "toolName": "shell",
                         "toolUseId": "call-1", "input": {"command": "inspect worker"}})
        recorder.append({"role": "tool_result", "toolUseId": "call-1", "content": tool_output})
    for event in recorder.events:
        event["recorded_at"] = recorded_at
    service.capture(recorder)
    return recorder


def save_fact(service, identity, text, recorded_at, **kwargs):
    record = make_record(
        project=service.project, workspace=service.workspace, record_id=identity,
        statement=text, recorded_at=recorded_at, origin="explicit", **kwargs,
    )
    service.store.apply([record])
    return record


def test_parse_time_range_accepts_offset_and_z_as_instants():
    assert parse_time_range(START, END) == (START_STAMP, END_STAMP)
    assert parse_time_range("2026-10-20T10:00:00Z", "2026-10-20T19:00:00+08:00") == (
        START_STAMP, END_STAMP,
    )
    assert parse_time_range(None, None) is None


@pytest.mark.parametrize("bounds", [
    {"start_time": START},
    {"end_time": END},
    {"start_time": START, "end_time": None},
    {"start_time": "", "end_time": END},
    {"start_time": START, "end_time": "  "},
    {"start_time": 1792490400, "end_time": END},
    {"start_time": START, "end_time": []},
    {"start_time": "yesterday evening", "end_time": END},
    {"start_time": "2026-02-30T10:00:00Z", "end_time": END},
    {"start_time": "2026-10-20T10:00:00", "end_time": END},
    {"start_time": START, "end_time": "2026-10-20T11:00:00"},
    {"start_time": START, "end_time": "2026-10-20T10:00:00Z"},
    {"start_time": END, "end_time": START},
])
def test_invalid_bounds_fail_without_reading_database(memory, monkeypatch, bounds):
    save_fact(memory, "must-not-fallback", "needle", START_STAMP)

    def unexpected_database_read():
        pytest.fail("Invalid time bounds must be rejected before accessing the database")

    monkeypatch.setattr(memory.store, "connect", unexpected_database_read)
    with pytest.raises(ValueError):
        memory.search("needle", now=NOW, **bounds)
    registry = ToolRegistry([memory_search_tool])
    result = registry.execute(
        "memory_search", {"query": "needle", **bounds},
        ToolContext(cwd=str(memory.root), memory_service=memory),
    )
    assert not result.ok
    assert "time" in result.output
    assert "must-not-fallback" not in result.output


def test_empty_query_without_bounds_is_rejected_before_database_access(memory, monkeypatch):
    def unexpected_database_read():
        pytest.fail("An empty unrestricted search must not load memory")

    monkeypatch.setattr(memory.store, "connect", unexpected_database_read)
    with pytest.raises(ValueError):
        memory.search("  ", now=NOW)
    registry = ToolRegistry([memory_search_tool])
    for data in ({}, {"query": "  "}):
        assert not registry.execute(
            "memory_search", data, ToolContext(cwd=str(memory.root), memory_service=memory),
        ).ok


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_registry_time_only_search_reads_durable_original_evidence(memory, mode):
    memory.mode = mode
    quote = "Original stderr: idle lease after reconnect."
    capture(memory, "before-start", "outside before", START_STAMP - .001)
    recorder = capture(memory, "at-start", "inspect upload worker", START_STAMP, tool_output=quote)
    capture(memory, "before-end", "different topic", END_STAMP - .001)
    capture(memory, "at-end", "outside at exclusive end", END_STAMP)
    save_fact(memory, "explicit-fact", "Incident occurred in 2024; use the verified worker command.",
              START_STAMP + 30, occurred_at=START_STAMP - 86400)

    # Reopen the database so this path cannot accidentally depend on recorder objects.
    service = MemoryService(memory.root, memory.store.path, mode=mode)
    registry = ToolRegistry([memory_search_tool, memory_read_tool])
    context = ToolContext(cwd=str(service.root), memory_service=service)
    searched = registry.execute("memory_search", {"start_time": START, "end_time": END, "limit": 10}, context)
    assert searched.ok, searched.output
    response = json.loads(searched.output)
    assert {row["id"] for row in response["results"]} == {"at-start", "before-end"}
    assert response["trace"]["time_window"] == [START_STAMP, END_STAMP]
    raw = next(row for row in response["results"] if row["id"] == "at-start")
    assert raw["source_ids"] == [recorder.events[0]["id"]]
    assert raw["result_type"] == "history_turn"
    assert raw["score"] is None
    assert quote not in raw["statement"]  # History pages list requests; read(id) opens the complete turn.

    read_result = registry.execute("memory_read", {"id": raw["id"]}, context)
    assert read_result.ok, read_result.output
    evidence = json.loads(json.loads(read_result.output)["text"])["evidence"]
    assert {"event_id": "at-start:2", "role": "tool_result", "quote": quote} in evidence
    source_result = registry.execute("memory_read", {"id": "at-start:2"}, context)
    assert source_result.ok, source_result.output
    source = json.loads(json.loads(source_result.output)["text"])
    assert source["content"] == quote
    assert source["turn_id"] == "at-start"
    assert source["session"] == "original-session"


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_time_only_range_still_excludes_future_turns_and_facts(memory, mode):
    now_stamp = NOW.timestamp()
    capture(memory, "at-now", "already recorded", now_stamp)
    capture(memory, "future-turn", "not recorded yet", now_stamp + .001)
    save_fact(memory, "past-fact", "available fact", now_stamp - .001)
    save_fact(memory, "future-fact", "unavailable fact", now_stamp + .001)
    result = memory.search(
        "  ", now=NOW, mode=mode,
        start_time=(NOW - timedelta(hours=1)).isoformat(),
        end_time=(NOW + timedelta(hours=1)).isoformat(),
    )
    assert {row["id"] for row in result["results"]} == {"at-now"}


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_today_is_a_keyword_and_does_not_infer_a_time_window(memory, mode):
    save_fact(memory, "old-today-keyword", "今天这个词是历史备注的一部分", START_STAMP - 30 * 86400)
    save_fact(memory, "recent-unrelated", "unrelated current note", NOW.timestamp() - 60)
    result = memory.search("今天", now=NOW, mode=mode)
    assert [row["id"] for row in result["results"]] == ["old-today-keyword"]
    assert result["trace"]["time_window"] is None


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_outside_high_score_hits_cannot_crowd_out_in_range_keyword_match(memory, mode):
    for index in range(40):
        save_fact(memory, f"outside-{index}", "needle", START_STAMP - index - 1)
    save_fact(memory, "earlier-unrelated", "an unrelated topic", START_STAMP)
    save_fact(memory, "in-range-match", "needle " + "padding " * 100, START_STAMP + 1)
    result = memory.search("needle", now=NOW, mode=mode, start_time=START, end_time=END, limit=1)
    # Eligibility is applied before BM25, and an unrelated in-range fact gets no score.
    assert [row["id"] for row in result["results"]] == ["in-range-match"]


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_time_only_search_keeps_local_workspace_scope(memory, tmp_path, monkeypatch, mode):
    other_root = tmp_path / "other-worktree"
    other_root.mkdir()
    # Two worktrees have one project identity and different local workspace IDs.
    monkeypatch.setattr(service_module, "project_identity", lambda _root: memory.project)
    other = MemoryService(other_root, memory.store.path, mode=mode)
    capture(memory, "own-turn", "visible original", START_STAMP)
    save_fact(memory, "own-fact", "visible fact", START_STAMP)
    capture(other, "foreign-turn", "private other worktree original", START_STAMP)
    save_fact(other, "foreign-fact", "private other worktree fact", START_STAMP)
    registry = ToolRegistry([memory_search_tool, memory_read_tool])
    memory.mode = mode
    context = ToolContext(cwd=str(memory.root), memory_service=memory)
    searched = registry.execute("memory_search", {"start_time": START, "end_time": END, "limit": 10}, context)
    assert searched.ok, searched.output
    assert {row["id"] for row in json.loads(searched.output)["results"]} == {"own-turn"}
    for identity in ("foreign-turn", "foreign-turn:0", "foreign-fact"):
        assert not registry.execute("memory_read", {"id": identity}, context).ok


def test_offline_agent_receives_clock_and_passes_explicit_range_to_real_memory(memory, monkeypatch):
    runtime_clock = "Current local time (default zone for relative dates): 2026-10-21T20:00:00+08:00"
    monkeypatch.setattr(agent_loop, "build_runtime_time_context", lambda: runtime_clock)
    bounds = {"start_time": START, "end_time": "2026-10-21T00:00:00+08:00"}
    original = "昨晚你让我修复上传线程停止后无法重启的问题。"
    capture(memory, "last-night-original", original, START_STAMP + 7200)
    capture(memory, "too-early", "窗口外旧内容：不要误报昨天上午的工作。", START_STAMP - 1)
    capture(memory, "at-midnight", "窗口外新内容：不要误报今天的工作。", START_STAMP + 21600)

    class ScriptedModel:
        # Scripted bounds test wiring; this does not evaluate a model's date reasoning.
        calls = 0

        def next(self, messages):
            self.calls += 1
            assert any(message["role"] == "system" and message["content"] == runtime_clock
                       for message in messages)
            if self.calls == 1:
                assert messages[-1]["content"] == "昨晚我叫你干什么？"
                return AgentStep(type="tool_calls", calls=[{
                    "id": "last-night-search", "toolName": "memory_search",
                    "input": {"query": "", **bounds},
                }])
            assert self.calls == 2
            result = messages[-1]
            assert result["role"] == "tool_result"
            assert result["toolName"] == "memory_search"
            assert result["toolUseId"] == "last-night-search"
            assert not result["isError"]
            response = json.loads(result["content"])
            assert [row["id"] for row in response["results"]] == ["last-night-original"]
            assert original in response["results"][0]["statement"]
            assert response["results"][0]["source_ids"] == ["last-night-original:0"]
            assert "窗口外" not in result["content"]
            return AgentStep(type="assistant", kind="final", content="昨晚你让我修复上传线程。")

    model = ScriptedModel()
    messages = agent_loop.run_agent_turn(
        model=model, tools=ToolRegistry([memory_search_tool]),
        messages=[{"role": "user", "content": "昨晚我叫你干什么？"}],
        cwd=str(memory.root), memory_service=memory,
    )
    assert model.calls == 2
    assert messages[-1] == {"role": "assistant", "content": "昨晚你让我修复上传线程。"}
    tool_call = next(message for message in messages if message["role"] == "assistant_tool_call")
    assert tool_call["input"] == {"query": "", **bounds}
