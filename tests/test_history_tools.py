from copy import deepcopy
import json

import pytest

from cortexterm.agent_loop import run_agent_turn
from cortexterm.anthropic_adapter import _to_anthropic_messages
from cortexterm.context_manager import ContextManager
from cortexterm.conversation_history import get_archived_messages
from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.tools import create_default_tool_registry
from cortexterm.tools.history import history_read_tool, history_search_tool
from cortexterm.types import AgentStep


def registry():
    return ToolRegistry([history_search_tool, history_read_tool])


def execute(name, args, archive):
    result = registry().execute(name, args, ToolContext(cwd=".", archived_messages=archive))
    assert result.ok, result.output
    return json.loads(result.output)


def test_search_returns_stable_ids_and_matching_snippets_with_pagination():
    archive = [
        {"role": "user", "content": "unrelated"},
        {"role": "tool_result", "toolName": "run_command", "toolUseId": "A", "content": "x" * 900 + "中文错误 needle" + "z" * 900},
        {"role": "assistant", "content": "another NEEDLE"},
    ]
    before = deepcopy(archive)
    first = execute("history_search", {"query": "needle", "limit": 1}, archive)
    assert first["total_matches"] == 2
    hit = first["results"][0]
    assert hit["message_id"] == "archive:2"
    assert "中文错误 needle" in hit["snippet"]
    assert hit["truncated"]
    assert archive[1]["content"][hit["snippet_offset"]:].startswith(hit["snippet"])
    second = execute("history_search", {"query": "needle", "offset": first["next_offset"]}, archive)
    assert second["results"][0]["message_id"] == "archive:3"
    assert second["next_offset"] is None
    assert archive == before


def test_search_can_browse_or_filter_tool_input_and_role():
    archive = [
        {"role": "assistant_tool_call", "toolName": "read_file", "toolUseId": "A", "input": {"path": "src/关键.py"}},
        {"role": "tool_result", "toolName": "read_file", "toolUseId": "A", "content": "file text"},
    ]
    assert execute("history_search", {}, archive)["total_matches"] == 2
    result = execute("history_search", {"query": "关键", "role": "assistant_tool_call", "tool_name": "read_file"}, archive)
    assert result["results"][0]["message_id"] == "archive:1"
    assert execute("history_search", {"query": "read_file"}, archive)["total_matches"] == 2
    assert execute("history_search", {"query": "not present"}, archive)["results"] == []


def test_unicode_case_matching_keeps_original_character_offsets():
    # U+0130 expands to two code points under str.lower(). Search offsets must
    # refer to the original message, so the returned snippet still includes it.
    text = "\u0130" * 800 + "target[1]" + "x" * 800
    archive = [{"role": "assistant", "content": text}]
    result = execute("history_search", {"query": "TARGET[1]"}, archive)
    hit = result["results"][0]
    assert "target[1]" in hit["snippet"]
    assert hit["snippet_offset"] == 700
    assert text[hit["snippet_offset"]:hit["snippet_offset"] + 400] == hit["snippet"]


def test_read_pages_reconstruct_original_text_and_link_tool_pair():
    content = "第一行\n" + "数据\n" * 5000 + "exact final error"
    archive = [
        {"role": "assistant_tool_call", "toolUseId": "A", "toolName": "read_file", "input": {"path": "log"}},
        {"role": "tool_result", "toolUseId": "A", "toolName": "read_file", "isError": True, "content": content},
    ]
    offset = 0
    chunks = []
    while offset is not None:
        result = execute("history_read", {"message_id": "archive:2", "offset": offset}, archive)
        assert len(result["text"]) <= 4000
        assert result["isError"]
        assert result["related_message_ids"] == ["archive:1"]
        chunks.append(result["text"])
        offset = result["next_offset"]
    assert "".join(chunks) == content
    call = execute("history_read", {"message_id": "archive:1"}, archive)
    assert json.loads(call["text"]) == {"path": "log"}
    assert call["text_format"] == "json"
    end = execute("history_read", {"message_id": "archive:2", "offset": len(content)}, archive)
    assert end["text"] == "" and end["next_offset"] is None


@pytest.mark.parametrize("name,args", [
    ("history_read", {"message_id": "../../other-session.json"}),
    ("history_read", {"message_id": "archive:0"}),
    ("history_read", {"message_id": "archive:1", "limit": 8001}),
    ("history_read", {"message_id": "archive:1", "offset": -1}),
    ("history_read", {"message_id": "archive:1", "offset": 999}),
    ("history_read", {"message_id": "archive:9"}),
    ("history_search", {"limit": 21}),
    ("history_search", {"offset": True}),
    ("history_search", {"query": "x" * 513}),
    ("history_search", {"role": "system"}),
])
def test_invalid_inputs_are_tool_errors(name, args):
    result = registry().execute(name, args, ToolContext(cwd=".", archived_messages=[{"role": "user", "content": "short"}]))
    assert not result.ok


def test_archive_scope_is_current_context_only_and_empty_is_normal():
    tools = registry()
    first = tools.execute("history_search", {"query": "secret"}, ToolContext(cwd=".", archived_messages=[{"role": "user", "content": "secret"}]))
    second = tools.execute("history_search", {"query": "secret"}, ToolContext(cwd="."))
    assert json.loads(first.output)["total_matches"] == 1
    assert second.ok and json.loads(second.output)["archive_count"] == 0
    assert not tools.execute("history_read", {"message_id": "archive:1"}, ToolContext(cwd=".")).ok


def test_ids_survive_repeated_compaction_and_real_session_save_resume(tmp_path, monkeypatch):
    from cortexterm import session as sessions

    monkeypatch.setattr(sessions, "CORTEXTERM_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    manager = ContextManager(context_window=500, reserve_tokens=100, keep_recent_tokens=20)
    manager.messages = [
        {"role": "user", "content": "first original " * 200},
        {"role": "assistant", "content": "old result " * 200},
        {"role": "user", "content": "current task " * 8},
    ]
    manager.compact_messages(lambda prompt: "checkpoint")
    first_archive = deepcopy(get_archived_messages(manager.messages))
    manager.messages.extend([
        {"role": "assistant", "content": "new result " * 400},
        {"role": "user", "content": "next task " * 10},
    ])
    manager.compact_messages(lambda prompt: "updated checkpoint")
    session = sessions.create_new_session(workspace=str(tmp_path))
    session.messages = manager.messages
    assert sessions.save_session(session)
    resumed = sessions.load_session(session.session_id)
    assert resumed is not None
    archive = get_archived_messages(resumed.messages)
    assert archive[:len(first_archive)] == first_archive
    assert len(archive) > len(first_archive)
    result = execute("history_read", {"message_id": "archive:1", "limit": 8000}, archive)
    assert result["text"] == first_archive[0]["content"]


def test_default_registry_exposes_both_history_tools(tmp_path):
    tools = create_default_tool_registry(str(tmp_path), runtime={"mcpServers": {}})
    try:
        assert tools.find("history_search") is not None
        assert tools.find("history_read") is not None
    finally:
        tools.dispose()


def test_agent_can_retrieve_current_turn_archive_without_inserting_old_tool_calls():
    old_result = "first part " * 1000 + "needle: exact failure"
    initial = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "inspect old error"},
        {"role": "assistant_tool_call", "toolUseId": "old", "toolName": "run_command", "input": {"command": "test"}},
        {"role": "tool_result", "toolUseId": "old", "toolName": "run_command", "content": old_result},
        {"role": "assistant", "content": "old investigation done"},
        {"role": "user", "content": "what was the exact failure? " * 3},
    ]

    class Model:
        calls = 0
        def summarize(self, prompt, *, max_tokens):
            return "An older command failed; consult the archive for details."

        def next(self, messages):
            self.calls += 1
            # Only the *new* retrieval tools may appear as live tool calls.
            _, converted = _to_anthropic_messages(messages)
            assert not any(block.get("id") == "old" for m in converted for block in m["content"])
            if self.calls == 1:
                return AgentStep(type="tool_calls", calls=[{"id": "search", "toolName": "history_search", "input": {"query": "needle"}}])
            if self.calls == 2:
                hit = json.loads(messages[-1]["content"])["results"][0]
                return AgentStep(type="tool_calls", calls=[{"id": "read", "toolName": "history_read", "input": {"message_id": hit["message_id"], "offset": hit["snippet_offset"], "limit": 400}}])
            result = json.loads(messages[-1]["content"])
            assert "needle: exact failure" in result["text"]
            return AgentStep(type="assistant", content="Recovered the exact failure.")

    manager = ContextManager(context_window=2000, reserve_tokens=200, keep_recent_tokens=20)
    result = run_agent_turn(model=Model(), tools=registry(), messages=initial, cwd=".", context_manager=manager)
    assert result[-1]["content"] == "Recovered the exact failure."
    assert any(m.get("isCompactionSummary") for m in result)
