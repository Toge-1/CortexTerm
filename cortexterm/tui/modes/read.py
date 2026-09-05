"""Transcript reading-mode event handling."""

from __future__ import annotations

from typing import Any, Callable

from cortexterm.tui.input_parser import KeyEvent, ParsedInputEvent, WheelEvent
from cortexterm.tui.state import ScreenState, TtyAppArgs


def handle_transcript_read_mode_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    *,
    scroll_by: Callable[[TtyAppArgs, ScreenState, int], bool],
    page_step: Callable[[TtyAppArgs, ScreenState], int],
    wheel_step: Callable[[TtyAppArgs, ScreenState], int],
    jump_to_edge: Callable[[TtyAppArgs, ScreenState, str], bool],
) -> None:
    """Handle keyboard/wheel events while transcript reading mode owns input."""
    if isinstance(event, KeyEvent):
        if event.name in ("escape",) or (event.ctrl and event.name == "r"):
            state.transcript_read_mode = False
            state.status = None
            state.transcript_scroll_offset = 0
            rerender()
            return

        if event.name == "pageup":
            if scroll_by(args, state, page_step(args, state)):
                rerender()
            return

        if event.name == "pagedown":
            if scroll_by(args, state, -page_step(args, state)):
                rerender()
            return

        if event.name == "home" or (event.ctrl and event.name == "a"):
            if jump_to_edge(args, state, "top"):
                rerender()
            return

        if event.name == "end" or (event.ctrl and event.name == "e"):
            if jump_to_edge(args, state, "bottom"):
                rerender()
            return

        if event.name == "up":
            if scroll_by(args, state, 1):
                rerender()
            return

        if event.name == "down":
            if scroll_by(args, state, -1):
                rerender()
            return

    if isinstance(event, WheelEvent):
        step = wheel_step(args, state)
        delta = step if event.direction == "up" else -step
        if scroll_by(args, state, delta):
            rerender()

