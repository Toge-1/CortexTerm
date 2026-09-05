"""Transcript tool-card state and interactions.

This module owns the display-side lifecycle of tool entries in the transcript:
creation, status updates, collapse/expand animations, mouse hit handling, and
unfinished-tool cleanup.  It deliberately does not execute tools and does not
know about model messages.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from cortexterm.tui.input_parser import MouseEvent
from cortexterm.tui.state import ScreenState
from cortexterm.tui.tool_display import summarize_collapsed_tool_body
from cortexterm.tui.types import TranscriptEntry


def push_transcript_entry(state: ScreenState, **kwargs: Any) -> int:
    """Create and append a new transcript entry."""
    entry_id = state.next_entry_id
    state.next_entry_id += 1
    state.transcript.append(TranscriptEntry(id=entry_id, **kwargs))
    return entry_id


def mark_running_tools_as_error(state: ScreenState, message: str) -> None:
    """Mark all currently running tools as failed with the given message."""
    for entry in state.transcript:
        if entry.kind == "tool" and entry.status == "running":
            entry.status = "error"
            entry.body = message
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            state.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
    if any(e.kind == "tool" and e.status == "error" for e in state.transcript):
        state.active_tool = None


def update_tool_entry(
    state: ScreenState,
    entry_id: int,
    status: str,
    body: str,
) -> None:
    """Update a tool entry's status and output body."""
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool":
            entry.status = status
            entry.body = body
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            entry.revealLines = None
            entry.transition = None
            entry.animationToken += 1
            return


def collapse_tool_entry(state: ScreenState, entry_id: int, summary: str) -> None:
    """Collapse a completed tool entry to a summary line."""
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool" and entry.status != "running":
            entry.collapsePhase = None
            entry.collapsed = True
            entry.collapsedSummary = summary
            entry.revealLines = None
            entry.transition = None
            entry.animationToken += 1
            return


def _tool_preview_line_count(entry: TranscriptEntry) -> int:
    raw_count = len(entry.body.splitlines()) if entry.body else 1
    if entry.toolName == "read_file":
        return min(raw_count, 20)
    return min(raw_count, 36)


def _animate_tool_open(
    state: ScreenState,
    entry_id: int,
    token: int,
    rerender: Callable[[], None],
) -> None:
    def _run() -> None:
        for lines in (1, 3, 7, 14, 28, 36):
            time.sleep(0.016)
            changed = False
            for entry in state.transcript:
                if (
                    entry.id == entry_id
                    and entry.kind == "tool"
                    and entry.transition == "opening"
                    and entry.animationToken == token
                ):
                    entry.revealLines = min(lines, _tool_preview_line_count(entry))
                    changed = True
                    break
            if changed:
                rerender()
            else:
                return

        for entry in state.transcript:
            if (
                entry.id == entry_id
                and entry.kind == "tool"
                and entry.transition == "opening"
                and entry.animationToken == token
            ):
                entry.revealLines = None
                entry.transition = None
                break
        rerender()

    threading.Thread(target=_run, daemon=True).start()


def _animate_tool_close(
    state: ScreenState,
    entry_id: int,
    token: int,
    rerender: Callable[[], None],
) -> None:
    def _run() -> None:
        time.sleep(0.045)
        for entry in state.transcript:
            if (
                entry.id == entry_id
                and entry.kind == "tool"
                and entry.transition == "closing"
                and entry.animationToken == token
            ):
                entry.collapsed = True
                entry.collapsePhase = None
                entry.collapsedSummary = entry.collapsedSummary or summarize_collapsed_tool_body(entry.body)
                entry.revealLines = None
                entry.transition = None
                break
        rerender()

    threading.Thread(target=_run, daemon=True).start()


def toggle_tool_entry(state: ScreenState, entry_id: int, rerender: Callable[[], None]) -> bool:
    """Toggle a completed tool entry between collapsed summary and full output."""
    for entry in state.transcript:
        if entry.id != entry_id or entry.kind != "tool":
            continue
        if entry.status == "running":
            return False

        state.manually_toggled_tools.add(entry_id)
        entry.animationToken += 1
        token = entry.animationToken

        should_open = (
            entry.collapsed
            or entry.collapsePhase is not None
            or entry.transition == "closing"
        )
        if should_open:
            entry.collapsed = False
            entry.collapsePhase = None
            entry.transition = "opening"
            entry.revealLines = 1
            _animate_tool_open(state, entry_id, token, rerender)
            return True

        entry.transition = "closing"
        entry.revealLines = 1
        entry.collapsedSummary = entry.collapsedSummary or summarize_collapsed_tool_body(entry.body)
        _animate_tool_close(state, entry_id, token, rerender)
        return True
    return False


def handle_mouse_event(state: ScreenState, event: MouseEvent, rerender: Callable[[], None]) -> bool:
    """Handle mouse clicks against the latest rendered hit-test zones."""
    if event.button != "left" or event.action != "up":
        return False

    for zone in state.mouse_zones:
        if zone.y_start <= event.y < zone.y_end and zone.action == "toggle_tool":
            if toggle_tool_entry(state, zone.entry_id, rerender):
                state.focused_entry_id = zone.entry_id
                rerender()
                return True
            return False
    return False


def get_running_tool_entries(state: ScreenState) -> list[TranscriptEntry]:
    """Get all transcript entries that are still in 'running' status."""
    return [entry for entry in state.transcript if entry.kind == "tool" and entry.status == "running"]


def finalize_dangling_running_tools(state: ScreenState) -> None:
    """Mark running tools as errors when a turn ends unexpectedly."""
    running = get_running_tool_entries(state)
    if running:
        error_message = (
            f"{running[0].body}\n\n"
            "ERROR: Tool did not report a final result before the turn ended. "
            "This usually means the command kept running in the background "
            "or the tool lifecycle got out of sync."
        )
        mark_running_tools_as_error(state, error_message)
        state.status = f"Previous turn ended with {len(running)} unfinished tool call(s)."


def schedule_tool_auto_collapse(
    state: ScreenState,
    entry_id: int,
    output: str,
    rerender: Callable[[], None],
) -> None:
    """Collapse tool output with a brief delay to reduce transcript noise."""
    summary = summarize_collapsed_tool_body(output)

    def _do_collapse() -> None:
        time.sleep(0.25)
        if entry_id in state.manually_toggled_tools:
            return
        collapse_tool_entry(state, entry_id, summary)
        rerender()

    threading.Thread(target=_do_collapse, daemon=True).start()
