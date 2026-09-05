"""Runtime actions triggered by the TTY input layer.

This module owns the non-rendering side effects that happen after the user
submits input in the terminal UI:

- built-in slash commands such as ``/tools`` and ``/debug``;
- local tool shortcuts;
- starting an agent turn in a background thread;
- turning agent callbacks into transcript/tool-card updates.

The full-screen event loop remains in ``tty_app.py`` because it owns terminal
raw mode and process lifecycle.  Keeping these turn actions here makes the
entrypoint easier to scan and keeps rendering/input-mode modules display-only.
"""

from __future__ import annotations

from typing import Any, Callable

from cortexterm.cli_commands import find_matching_slash_commands, try_handle_local_command
from cortexterm.history import save_history_entries
from cortexterm.local_tool_shortcuts import parse_local_tool_shortcut
from cortexterm.tooling import ToolContext
from cortexterm.tui.agent_turn import start_agent_turn
from cortexterm.tui.chrome import _cached_terminal_size
from cortexterm.tui.render_session import (
    get_chrome_overhead,
    get_transcript_body_lines,
    is_compact_terminal,
)
from cortexterm.tui.state import ScreenState, TtyAppArgs
from cortexterm.tui.tool_cards import (
    collapse_tool_entry,
    finalize_dangling_running_tools,
    get_running_tool_entries,
    push_transcript_entry,
    update_tool_entry,
)
from cortexterm.tui.tool_display import (
    summarize_collapsed_tool_body,
    summarize_tool_input,
)
from cortexterm.tui.transcript import _compute_total_lines, get_transcript_max_scroll_offset


def execute_tool_shortcut(
    args: TtyAppArgs,
    state: ScreenState,
    tool_name: str,
    tool_input: Any,
    rerender: Callable[[], None],
) -> None:
    """Execute a local tool shortcut directly from the TTY command line."""
    state.is_busy = True
    state.status = f"Running {tool_name}..."
    state.active_tool = tool_name
    entry_id = push_transcript_entry(
        state,
        kind="tool",
        toolName=tool_name,
        status="running",
        body=summarize_tool_input(tool_name, tool_input),
    )
    rerender()

    try:
        result = args.tools.execute(
            tool_name,
            tool_input,
            context=ToolContext(cwd=args.cwd, permissions=args.permissions),
        )
        state.recent_tools.append(
            {
                "name": tool_name,
                "status": "success" if result.ok else "error",
            }
        )
        output = result.output if result.ok else f"ERROR: {result.output}"
        update_tool_entry(state, entry_id, "success" if result.ok else "error", output)
        collapse_tool_entry(state, entry_id, summarize_collapsed_tool_body(output))
    finally:
        state.is_busy = False
        state.active_tool = None
        finalize_dangling_running_tools(state)
        if not get_running_tool_entries(state):
            state.status = None


def handle_input(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    submitted_raw_input: str | None = None,
    run_agent_turn_func: Callable[..., Any] | None = None,
    save_history_func: Callable[[list[str]], None] | None = None,
) -> bool:
    """Handle a submitted prompt line.  Returns True when the user requested exit."""
    if state.is_busy:
        state.status = (
            f"Running {state.active_tool}..."
            if state.active_tool
            else "Current turn is still running..."
        )
        return False

    input_text = (submitted_raw_input if submitted_raw_input is not None else state.input).strip()
    if not input_text:
        return False
    if input_text == "/exit":
        return True

    if not state.history or state.history[-1] != input_text:
        state.history.append(input_text)
        history_saver = save_history_func or save_history_entries
        history_saver(state.history)
    state.history_index = len(state.history)
    state.history_draft = ""

    if state.autosave:
        state.autosave.mark_dirty()

    if input_text == "/tools":
        push_transcript_entry(
            state,
            kind="assistant",
            body="\n".join(f"{t.name}: {t.description}" for t in args.tools.list()),
        )
        return False

    if input_text == "/debug":
        _append_scroll_debug(args, state)
        return False

    local_result = try_handle_local_command(input_text, tools=args.tools)
    if local_result is not None:
        push_transcript_entry(state, kind="assistant", body=local_result)
        return False

    shortcut = parse_local_tool_shortcut(input_text)
    if shortcut:
        execute_tool_shortcut(
            args,
            state,
            shortcut["toolName"],
            shortcut["input"],
            rerender,
        )
        return False

    if input_text.startswith("/"):
        matches = find_matching_slash_commands(input_text)
        push_transcript_entry(
            state,
            kind="assistant",
            body=(
                f"Unknown command. Did you mean:\n{chr(10).join(matches)}"
                if matches
                else "Unknown command. Type /help to see available commands."
            ),
        )
        return False

    start_agent_turn(args, state, rerender, input_text, run_agent_turn_func)
    return False


def _append_scroll_debug(args: TtyAppArgs, state: ScreenState) -> None:
    cols, rows = _cached_terminal_size()
    compact = is_compact_terminal()
    body_lines = get_transcript_body_lines(args, state)
    total_lines = _compute_total_lines(state.transcript, cols)
    max_scroll = get_transcript_max_scroll_offset(state.transcript, body_lines, cols)
    chrome = get_chrome_overhead(args, state)
    lines = [
        "=== Scroll Debug ===",
        f"Terminal: {cols}x{rows}  compact={compact}",
        f"Chrome overhead: {chrome} lines",
        "Transcript frame: 4 lines",
        f"Body window: {body_lines} lines",
        f"Transcript total: {total_lines} lines",
        f"Scroll offset: {state.transcript_scroll_offset}/{max_scroll}",
        "Mouse tracking: ESC[?1000h ESC[?1003h ESC[?1006h",
        "",
        "Try scrolling now. If scroll_offset changes, mouse events work.",
        "Use PageUp/PageDown or Ctrl+A/E as keyboard alternatives.",
    ]
    push_transcript_entry(state, kind="assistant", body="\n".join(lines))
