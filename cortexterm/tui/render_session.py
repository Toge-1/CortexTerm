"""Session screen rendering and transcript layout for the TTY app."""

from __future__ import annotations

import sys
from typing import Any

from cortexterm.background_tasks import list_background_tasks
from cortexterm.tui.chrome import (
    _cached_terminal_size,
    render_banner,
    render_footer_bar,
    render_panel,
    render_permission_prompt,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
    SUBTLE,
    RESET,
    use_panel_frames,
)
from cortexterm.tui.input import get_input_cursor_cell, render_input_prompt
from cortexterm.tui.navigation import get_visible_commands
from cortexterm.tui.render_home import render_home_screen
from cortexterm.tui.state import MouseZone, ScreenState, TtyAppArgs
from cortexterm.tui.transcript import (
    get_transcript_visible_entry_ranges,
    get_transcript_max_scroll_offset,
    render_transcript,
)
from cortexterm.tui.types import TranscriptEntry

_get_terminal_size = _cached_terminal_size


def get_session_stats(args: TtyAppArgs, state: ScreenState) -> dict[str, int]:
    """Get current session statistics.
    
    Returns a dict with transcript, message, skill, and MCP server counts.
    """
    return {
        "transcriptCount": len(state.transcript),
        "messageCount": len(getattr(args, "messages", [])),
        "skillCount": len(args.tools.get_skills()) if hasattr(args, "tools") else 0,
        "mcpCount": len(args.tools.get_mcp_servers()) if hasattr(args, "tools") else 0,
    }


def get_contextual_help(state: ScreenState, args: TtyAppArgs) -> str | None:
    """根据当前状态提供上下文相关的帮助提示（纯文本，不含 emoji）。"""
    if not state.is_busy and not state.pending_approval:
        return None  # 保持状态栏简洁

    if state.is_busy and state.active_tool:
        return f"Running {state.active_tool}... (Ctrl+C to cancel)"

    if state.pending_approval:
        return "Approval required. Use arrow keys and Enter to choose."

    return None


# ---------------------------------------------------------------------------
# Scroll / history / slash
# ---------------------------------------------------------------------------


_FOOTER_LINES = 1
_PROMPT_MAX_EDIT_LINES = 4
_chrome_overhead_cache: dict[str, tuple[tuple, int]] = {}


def count_rendered_lines(s: str) -> int:
    """Count screen lines in a rendered string."""
    return s.count("\n") + 1


def get_chrome_overhead(args: TtyAppArgs, state: ScreenState) -> int:
    """Measure header + prompt + footer overhead for transcript sizing."""
    compact = is_compact_terminal()
    gaps = 2 if compact else 4
    cache_key = (
        getattr(args, "cwd", ""),
        getattr(args, "model", None),
        state.input,
        bool(state.pending_approval),
        compact,
        _cached_terminal_size(),
    )
    cached = _chrome_overhead_cache.get("key")
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    if not all(hasattr(args, name) for name in ("runtime", "permissions", "tools")):
        overhead = _FOOTER_LINES + gaps + 4
        _chrome_overhead_cache["key"] = (cache_key, overhead)
        return overhead

    header_lines = count_rendered_lines(render_header_panel(args, state))
    prompt_lines = count_rendered_lines(render_prompt_panel(state))
    overhead = header_lines + prompt_lines + _FOOTER_LINES + gaps
    _chrome_overhead_cache["key"] = (cache_key, overhead)
    return overhead


def get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    _, rows = _get_terminal_size()
    rows = max(24, rows)
    transcript_frame = 4
    chrome_overhead = get_chrome_overhead(args, state) + transcript_frame
    return max(6, rows - chrome_overhead)


def get_transcript_panel_width() -> int:
    cols, _ = _get_terminal_size()
    return max(20, cols - 4)


def get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(
        state.transcript,
        get_transcript_body_lines(args, state),
    )


def scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    max_offset = get_max_transcript_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def transcript_page_step(args: TtyAppArgs, state: ScreenState) -> int:
    return max(3, get_transcript_body_lines(args, state) - 2)


def transcript_wheel_step(args: TtyAppArgs, state: ScreenState) -> int:
    return max(1, min(4, get_transcript_body_lines(args, state) // 6))


def jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    next_offset = get_max_transcript_scroll_offset(args, state) if target == "top" else 0
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def toggle_transcript_read_mode(args: TtyAppArgs, state: ScreenState) -> bool:
    state.transcript_read_mode = not state.transcript_read_mode
    if state.transcript_read_mode:
        state.status = "Reading transcript. PgUp/PgDn or wheel to scroll, Ctrl+R to exit."
    else:
        state.status = None
        state.transcript_scroll_offset = 0
    return True


# ---------------------------------------------------------------------------
# Rendering — cached header & footer
# ---------------------------------------------------------------------------

# Banner cache: the banner rarely changes (only when cwd, model, or stats change).
_banner_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


_COMPACT_ROWS_THRESHOLD = 35  # Use compact UI when terminal rows < this value


def is_compact_terminal() -> bool:
    """Return True when the terminal is too short for the full UI chrome."""
    _, rows = _get_terminal_size()
    return rows < _COMPACT_ROWS_THRESHOLD


def render_header_panel(args: TtyAppArgs, state: ScreenState) -> str:
    """Render the top banner panel with model info, cwd, and session stats.
    
    The result is cached to avoid re-rendering when stats haven't changed.
    Uses compact single-line mode when the terminal has fewer than
    _COMPACT_ROWS_THRESHOLD rows so that the transcript area has more space.
    """
    stats = get_session_stats(args, state)
    compact = is_compact_terminal()
    cache_key = (
        args.cwd,
        id(args.runtime),
        stats.get("transcriptCount"),
        stats.get("messageCount"),
        stats.get("skillCount"),
        stats.get("mcpCount"),
        _cached_terminal_size(),
        compact,
    )
    cached = _banner_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_banner(
        args.runtime,
        args.cwd,
        args.permissions.get_summary(),
        stats,
        compact=compact,
    )
    _banner_cache["key"] = (cache_key, result)
    return result


# Footer cache: only changes with status, tool/skill state, background tasks
_footer_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


def render_footer_cached(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]],
) -> str:
    """Render the bottom status bar with caching to reduce flicker.
    
    Shows current operation status, tool/skill availability, and background tasks.
    """
    cache_key = (
        status,
        tools_enabled,
        skills_enabled,
        len(background_tasks),
        _cached_terminal_size(),
    )
    cached = _footer_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_footer_bar(status, tools_enabled, skills_enabled, background_tasks)
    _footer_cache["key"] = (cache_key, result)
    return result


def render_prompt_panel(state: ScreenState) -> str:
    compact = is_compact_terminal()
    commands = get_visible_commands(state.input)
    prompt_body = render_input_prompt(
        state.input,
        state.cursor_offset,
        compact=compact,
        max_lines=_PROMPT_MAX_EDIT_LINES,
    )
    if commands:
        prompt_body += "\n" + render_slash_menu(
            commands,
            min(state.selected_slash_index, len(commands) - 1),
        )
    return render_panel("prompt", prompt_body)


def render_session_prompt(state: ScreenState) -> str:
    commands = get_visible_commands(state.input)
    prompt_body = render_input_prompt(
        state.input,
        state.cursor_offset,
        compact=True,
        max_lines=_PROMPT_MAX_EDIT_LINES,
    )
    if commands:
        prompt_body += "\n" + render_slash_menu(
            commands,
            min(state.selected_slash_index, len(commands) - 1),
        )
    return prompt_body


def next_render_row(buf: list[str]) -> int:
    """Return the 1-based terminal row where the next appended block starts."""
    return 1 + sum(part.count("\n") for part in buf)


def transcript_panel_body_start_offset() -> int:
    """Rows inside a rendered panel before the transcript body starts."""
    return 3 if use_panel_frames() else 1


def install_transcript_mouse_zones(
    args: TtyAppArgs,
    state: ScreenState,
    transcript_snapshot: list[TranscriptEntry],
    panel_start_row: int,
    body_lines: int,
) -> None:
    body_start = panel_start_row + transcript_panel_body_start_offset()
    entries_by_id = {entry.id: entry for entry in transcript_snapshot}
    ranges = get_transcript_visible_entry_ranges(
        transcript_snapshot,
        state.transcript_scroll_offset,
        body_lines,
        get_transcript_panel_width(),
        active_entry_id=state.focused_entry_id,
    )
    state.mouse_zones = [
        MouseZone(
            y_start=body_start + start,
            y_end=body_start + end,
            entry_id=entry_id,
            action="toggle_tool",
        )
        for entry_id, start, end in ranges
        if entries_by_id.get(entry_id) is not None and entries_by_id[entry_id].kind == "tool"
    ]


def render_screen(args: TtyAppArgs, state: ScreenState) -> None:
    background_tasks = list_background_tasks()
    compact = is_compact_terminal()
    sep = "\n" if compact else "\n\n"

    # Build the entire frame into a buffer, then write once
    buf: list[str] = []
    # CSI H + CSI J  (cursor home + erase to end) – avoids full clear flicker
    buf.append("\x1b[H\x1b[J")
    state.mouse_zones = []

    if state.show_welcome and not state.pending_approval and not state.transcript_read_mode:
        buf.append(render_home_screen(args, state))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        return

    if state.transcript_read_mode and not state.pending_approval:
        transcript_snapshot = list(state.transcript)
        body_lines = get_transcript_body_lines(args, state)
        if transcript_snapshot:
            transcript_body = render_transcript(
                transcript_snapshot,
                state.transcript_scroll_offset,
                body_lines,
                get_transcript_panel_width(),
                active_entry_id=state.focused_entry_id,
            )
        else:
            transcript_body = f"{render_status_line(None)}\n\nNo transcript yet."
        panel_start_row = next_render_row(buf)
        install_transcript_mouse_zones(args, state, transcript_snapshot, panel_start_row, body_lines)
        buf.append(
            render_panel(
                "conversation - reading mode",
                transcript_body,
                right_title=f"{len(transcript_snapshot)} events",
                min_body_lines=body_lines,
                max_body_lines=body_lines,
            )
        )
        buf.append(
            f"\n{SUBTLE}Reading mode: PgUp/PgDn or wheel scroll - Home/Ctrl+A top - End/Ctrl+E bottom - Ctrl+R/Esc exit{RESET}"
        )
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        return

    has_skills = len(args.tools.get_skills()) > 0

    if state.pending_approval:
        # Permission approval overlay
        buf.append(
            render_permission_prompt(
                state.pending_approval.request,
                expanded=state.pending_approval.details_expanded,
                scroll_offset=state.pending_approval.details_scroll_offset,
                selected_choice_index=state.pending_approval.selected_choice_index,
                feedback_mode=state.pending_approval.feedback_mode,
                feedback_input=state.pending_approval.feedback_input,
            )
        )
        buf.append(sep)
        buf.append(
            render_panel(
                "activity",
                render_tool_panel(state.active_tool, state.recent_tools, background_tasks),
            )
        )
        buf.append(sep)
        buf.append(render_footer_cached(state.status, True, has_skills, background_tasks))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        return

    # Transcript — snapshot the list to avoid IndexError from concurrent
    # agent-thread appends (CPython GIL makes list.append atomic but
    # iteration + append can still race on length vs slot access).
    transcript_snapshot = list(state.transcript)
    body_lines = get_transcript_body_lines(args, state)
    if transcript_snapshot:
        transcript_body = render_transcript(
            transcript_snapshot,
            state.transcript_scroll_offset,
            body_lines,
            get_transcript_panel_width(),
            active_entry_id=state.focused_entry_id,
        )
    else:
        transcript_body = f"{render_status_line(None)}\n\nNo transcript yet."
    panel_start_row = next_render_row(buf)
    install_transcript_mouse_zones(args, state, transcript_snapshot, panel_start_row, body_lines)
    buf.append(
        render_panel(
            "conversation",
            transcript_body,
            right_title=f"{len(transcript_snapshot)} events",
            min_body_lines=body_lines,
            max_body_lines=body_lines,
        )
    )
    buf.append(sep)

    # Prompt
    #
    # Keep the terminal's real cursor on the editable prompt.  The visual
    # cursor is drawn by render_input_prompt(), but Windows IME composition
    # follows the hidden real cursor; if we leave it after the footer, pinyin
    # preedit text appears in the bottom-right status area.
    prompt_start_row = next_render_row(buf)
    prompt_cursor_row_offset, prompt_cursor_col = get_input_cursor_cell(
        state.input,
        state.cursor_offset,
        max_lines=_PROMPT_MAX_EDIT_LINES,
    )
    buf.append(render_session_prompt(state))
    buf.append("\n")

    # Footer (cached)
    buf.append(render_footer_cached(state.status, True, has_skills, background_tasks))

    # Contextual hint (only when busy or awaiting approval — no idle spam)
    contextual_help = get_contextual_help(state, args)
    if contextual_help:
        buf.append(f"\n{SUBTLE}{contextual_help}{RESET}")

    buf.append(f"\x1b[{prompt_start_row + prompt_cursor_row_offset};{prompt_cursor_col}H")

    sys.stdout.write("".join(buf))
    sys.stdout.flush()


