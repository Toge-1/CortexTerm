"""Permission approval and rejection-feedback input modes."""

from __future__ import annotations

import threading
from typing import Any, Callable

from cortexterm.tui.input_parser import KeyEvent, ParsedInputEvent, TextEvent, WheelEvent
from cortexterm.tui.navigation import (
    move_pending_approval_selection,
    scroll_pending_approval_by,
    toggle_pending_approval_expand,
)
from cortexterm.tui.state import ScreenState


def handle_pending_approval_event(
    state: ScreenState,
    pending: Any,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Handle input events while a permission approval is pending.

    ``pending`` is captured by the caller to avoid TOCTOU races with the agent
    thread, which may clear ``state.pending_approval`` after approval resolves.
    """
    if pending.feedback_mode:
        handle_feedback_mode_event(state, event, rerender, approval_event, approval_result)
        return

    if isinstance(event, KeyEvent):
        if _handle_pending_approval_key(state, event, rerender, approval_event, approval_result):
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        if _handle_pending_approval_text(state, event, rerender, approval_event, approval_result):
            return

    if isinstance(event, WheelEvent):
        if _handle_pending_approval_wheel(state, event, rerender):
            return


def _handle_pending_approval_key(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    pending = state.pending_approval

    if event.name == "escape":
        approval_result.clear()
        approval_result["decision"] = "deny_once"
        approval_event.set()
        rerender()
        return True

    if event.name == "return":
        _confirm_pending_choice(state, rerender, approval_event, approval_result)
        return True

    if event.name == "up" and move_pending_approval_selection(state, -1):
        rerender()
        return True

    if event.name == "down" and move_pending_approval_selection(state, 1):
        rerender()
        return True

    if event.name == "pageup" and scroll_pending_approval_by(state, -5):
        rerender()
        return True

    if event.name == "pagedown" and scroll_pending_approval_by(state, 5):
        rerender()
        return True

    choices = pending.request.get("choices", []) if pending else []
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True

    return False


def _handle_pending_approval_text(
    state: ScreenState,
    event: TextEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    pending = state.pending_approval

    if event.text == "v" and toggle_pending_approval_expand(state):
        rerender()
        return True

    choices = pending.request.get("choices", []) if pending else []
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True

    return False


def _handle_pending_approval_wheel(
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    delta = 3 if event.direction == "up" else -3
    if scroll_pending_approval_by(state, delta):
        rerender()
        return True
    return False


def _confirm_pending_choice(
    state: ScreenState,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    pending = state.pending_approval
    choices = pending.request.get("choices", []) if pending else []

    if pending and choices and 0 <= pending.selected_choice_index < len(choices):
        choice = choices[pending.selected_choice_index]
        _select_pending_choice(state, choice, rerender, approval_event, approval_result)
    else:
        approval_result.clear()
        approval_result["decision"] = "allow_once"
        approval_event.set()
        rerender()


def _select_pending_choice(
    state: ScreenState,
    choice: dict,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    pending = state.pending_approval
    decision = choice.get("decision", "allow_once")

    if pending and decision == "deny_with_feedback":
        pending.feedback_mode = True
        pending.feedback_input = ""
        rerender()
        return

    approval_result.clear()
    approval_result["decision"] = decision
    approval_event.set()
    rerender()


def handle_feedback_mode_event(
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Handle events when collecting rejection guidance text."""
    pending = state.pending_approval
    if not pending:
        return

    if isinstance(event, KeyEvent):
        if event.name == "escape":
            pending.feedback_mode = False
            pending.feedback_input = ""
            rerender()
            return
        if event.name == "return":
            approval_result.clear()
            approval_result["decision"] = "deny_with_feedback"
            approval_result["feedback"] = pending.feedback_input
            approval_event.set()
            rerender()
            return
        if event.name == "backspace":
            if pending.feedback_input:
                pending.feedback_input = pending.feedback_input[:-1]
                rerender()
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        pending.feedback_input += event.text
        rerender()

