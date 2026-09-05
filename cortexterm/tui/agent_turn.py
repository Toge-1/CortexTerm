"""Background agent-turn orchestration for the TTY app."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from cortexterm.agent_loop import run_agent_turn
from cortexterm.prompt import build_system_prompt
from cortexterm.state import set_busy
from cortexterm.tui.agent_tool_events import handle_tool_result, handle_tool_start
from cortexterm.tui.state import AggregatedEditProgress, ScreenState, TtyAppArgs
from cortexterm.tui.tool_cards import push_transcript_entry


def start_agent_turn(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    input_text: str,
    run_agent_turn_func: Callable[..., Any] | None = None,
) -> None:
    """Append user input and run the model/tool loop on a background thread."""
    push_transcript_entry(state, kind="user", body=input_text)
    state.transcript_scroll_offset = 0
    state.status = "Thinking..."
    state.is_busy = True

    if state.app_state:
        state.app_state.set_state(set_busy())

    rerender()

    pending_tool_entries: dict[str, list[int]] = defaultdict(list)
    aggregated_edit_by_key: dict[str, AggregatedEditProgress] = {}
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress] = {}

    _refresh_system_prompt(args)
    args.messages.append({"role": "user", "content": input_text})

    def on_assistant_message(content: str) -> None:
        push_transcript_entry(state, kind="assistant", body=content)
        rerender()

    def on_progress_message(content: str) -> None:
        push_transcript_entry(state, kind="progress", body=content)
        rerender()

    def on_context_event(event: str, payload: dict[str, Any]) -> None:
        _append_context_event(state, event, payload)
        rerender()

    def on_tool_start(tool_name: str, tool_input: Any) -> None:
        handle_tool_start(
            state,
            pending_tool_entries,
            aggregated_edit_by_key,
            aggregated_edit_by_entry_id,
            tool_name,
            tool_input,
        )
        rerender()

    def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
        handle_tool_result(
            state,
            pending_tool_entries,
            aggregated_edit_by_key,
            aggregated_edit_by_entry_id,
            tool_name,
            output,
            is_error,
            rerender,
        )
        rerender()

    args.permissions.begin_turn()

    agent_result: dict[str, Any] = {"messages": None}
    agent_thread_lock = threading.Lock()

    def run_agent_background() -> None:
        try:
            turn_runner = run_agent_turn_func or run_agent_turn
            next_messages = turn_runner(
                model=args.model,
                tools=args.tools,
                messages=list(args.messages),
                cwd=args.cwd,
                permissions=args.permissions,
                context_manager=args.context_mgr,
                on_tool_start=on_tool_start,
                on_tool_result=on_tool_result,
                on_assistant_message=on_assistant_message,
                on_progress_message=on_progress_message,
                on_context_event=on_context_event,
            )
            with agent_thread_lock:
                agent_result["messages"] = next_messages
        except Exception as exc:
            with agent_thread_lock:
                agent_result["error"] = exc
        finally:
            args.permissions.end_turn()
            with agent_thread_lock:
                agent_result["done"] = True
            state.is_busy = False
            state.active_tool = None
            state.status = None
            rerender()

    agent_thread = threading.Thread(target=run_agent_background, daemon=True)
    agent_thread.start()
    state.agent_thread = agent_thread
    # The main loop may see ``agent_result`` immediately, so install the lock first.
    state.agent_lock = agent_thread_lock
    state.agent_result = agent_result


def _refresh_system_prompt(args: TtyAppArgs) -> None:
    args.messages[0] = {
        "role": "system",
        "content": build_system_prompt(
            args.cwd,
            args.permissions.get_summary(),
            {
                "skills": args.tools.get_skills(),
                "mcpServers": args.tools.get_mcp_servers(),
                "memory_context": (
                    args.memory_mgr.get_relevant_context() if args.memory_mgr else ""
                ),
            },
        ),
    }


def _append_context_event(
    state: ScreenState,
    event: str,
    payload: dict[str, Any],
) -> None:
    if event == "compact_start":
        state.status = "Compressing context..."
        before = int(payload.get("before_tokens") or 0)
        window = int(payload.get("context_window") or 0)
        usage = float(payload.get("usage_percentage") or 0)
        body = (
            f"Context compaction started. Current context is {usage:.0f}% "
            f"({before:,}/{window:,} tokens)."
            if window
            else "Context compaction started."
        )
        push_transcript_entry(state, kind="progress", body=body)
        return

    if event == "compact_done":
        state.status = "Context compressed."
        before = int(payload.get("before_tokens") or 0)
        after = int(payload.get("after_tokens") or 0)
        usage = float(payload.get("usage_percentage") or 0)
        removed = payload.get("messages_removed")
        parts = [
            f"Context compaction complete. {before:,} -> {after:,} tokens",
            f"now {usage:.0f}%",
        ]
        if removed is not None:
            parts.append(f"{removed} messages removed")
        summary = payload.get("summary")
        if summary:
            parts.append(str(summary))
        push_transcript_entry(state, kind="progress", body="; ".join(parts) + ".")
        return

    state.status = f"Context event: {event}"
    push_transcript_entry(state, kind="progress", body=f"Context event: {event}")
