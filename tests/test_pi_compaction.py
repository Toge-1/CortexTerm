from __future__ import annotations

from cortexterm.context_manager import ContextManager, prepare_pi_compaction


def _summary(prompt: str) -> str:
    return "## Goal\nContinue the task.\n\n## Critical Context\n- Saved facts."


def test_pi_trigger_reserves_response_tokens() -> None:
    manager = ContextManager(
        context_window=1_000,
        reserve_tokens=200,
        keep_recent_tokens=100,
    )
    manager.messages = [{"role": "user", "content": "x" * 3_300}]

    assert manager.get_stats().total_tokens > 800
    assert manager.should_auto_compact()


def test_pi_compaction_keeps_recent_turn_and_uses_real_summary() -> None:
    manager = ContextManager(
        context_window=500,
        reserve_tokens=100,
        keep_recent_tokens=80,
    )
    manager.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "old request " * 50},
        {"role": "assistant", "content": "old answer " * 50},
        {"role": "user", "content": "recent request"},
        {"role": "assistant", "content": "recent answer " * 30},
    ]

    compacted = manager.compact_messages(_summary)

    summary_message = next(message for message in compacted if message.get("isCompactionSummary"))
    assert summary_message["content"].startswith("## Goal")
    assert any(message.get("content", "").startswith("recent answer") for message in compacted)
    assert manager.compaction_history[-1]["is_split_turn"] is True
    assert summary_message["compactedMessages"]
    assert manager.compaction_history[-1]["strategy"] == "pi"


def test_pi_cut_never_orphans_a_tool_result() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old" * 100},
        {
            "role": "assistant_tool_call",
            "toolUseId": "call-1",
            "toolName": "read_file",
            "input": {"path": "old.txt"},
        },
        {"role": "user", "content": "new"},
        {
            "role": "assistant_tool_call",
            "toolUseId": "call-2",
            "toolName": "read_file",
            "input": {"path": "new.txt"},
        },
        {
            "role": "tool_result",
            "toolUseId": "call-2",
            "toolName": "read_file",
            "content": "result" * 100,
            "isError": False,
        },
    ]

    preparation = prepare_pi_compaction(messages, keep_recent_tokens=40)

    assert preparation is not None
    kept_ids = {
        message.get("toolUseId")
        for message in preparation.kept_messages
        if message.get("role") in {"assistant_tool_call", "tool_result"}
    }
    assert kept_ids == {"call-2"}


def test_pi_cut_uses_next_allowed_event_after_tool_result() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {
            "role": "assistant_tool_call",
            "toolUseId": "call-1",
            "toolName": "read_file",
            "input": {"path": "large.txt"},
        },
        {
            "role": "tool_result",
            "toolUseId": "call-1",
            "toolName": "read_file",
            "content": "x" * 800,
            "isError": False,
        },
        {"role": "assistant_progress", "content": "processing the result"},
        {"role": "assistant", "content": "recent answer"},
    ]

    preparation = prepare_pi_compaction(messages, keep_recent_tokens=40)

    assert preparation is not None
    assert preparation.kept_messages[0]["role"] == "assistant_progress"


def test_pi_cut_rejects_unknown_event_types() -> None:
    messages = [
        {"role": "user", "content": "old" * 100},
        {"role": "future_metadata", "content": "x" * 400},
        {"role": "assistant", "content": "recent"},
    ]

    preparation = prepare_pi_compaction(messages, keep_recent_tokens=20)

    assert preparation is not None
    assert preparation.kept_messages[0]["role"] == "assistant"


def test_repeated_pi_compaction_updates_previous_summary() -> None:
    prompts: list[str] = []

    def summarize(prompt: str) -> str:
        prompts.append(prompt)
        return f"summary-{len(prompts)}"

    manager = ContextManager(
        context_window=300,
        reserve_tokens=80,
        keep_recent_tokens=40,
    )
    manager.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first " * 100},
        {"role": "assistant", "content": "answer " * 100},
        {"role": "user", "content": "recent"},
    ]
    manager.compact_messages(summarize)
    manager.messages.extend(
        [
            {"role": "assistant", "content": "more " * 300},
            {"role": "user", "content": "latest"},
        ]
    )

    manager.compact_messages(summarize)

    assert any(
        "<previous-summary>" in prompt and "summary-" in prompt
        for prompt in prompts[1:]
    )
    summaries = [message for message in manager.messages if message.get("isCompactionSummary")]
    assert len(summaries) == 1
    assert len(summaries[0]["compactedMessages"]) >= 2


def test_pi_compaction_tracks_file_operations_cumulatively() -> None:
    manager = ContextManager(
        context_window=250,
        reserve_tokens=80,
        keep_recent_tokens=20,
    )
    manager.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "work"},
        {
            "role": "assistant_tool_call",
            "toolUseId": "read-1",
            "toolName": "read_file",
            "input": {"path": "src/old.py"},
        },
        {
            "role": "tool_result",
            "toolUseId": "read-1",
            "toolName": "read_file",
            "content": "x" * 400,
            "isError": False,
        },
        {
            "role": "assistant_tool_call",
            "toolUseId": "edit-1",
            "toolName": "edit_file",
            "input": {"path": "src/changed.py", "search": "a", "replace": "b"},
        },
        {
            "role": "tool_result",
            "toolUseId": "edit-1",
            "toolName": "edit_file",
            "content": "done",
            "isError": False,
        },
        {"role": "assistant", "content": "finished " * 50},
    ]

    compacted = manager.compact_messages(_summary)

    summary = next(message for message in compacted if message.get("isCompactionSummary"))
    assert "src/old.py" in summary["content"]
    assert "src/changed.py" in summary["content"]
    assert manager.compaction_history[-1]["read_files"] == ["src/old.py"]
    assert manager.compaction_history[-1]["modified_files"] == ["src/changed.py"]
