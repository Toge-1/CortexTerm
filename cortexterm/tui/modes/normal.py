"""Normal prompt-input mode for the TTY app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cortexterm.tui.input_parser import KeyEvent, ParsedInputEvent, TextEvent, WheelEvent
from cortexterm.tui.navigation import get_visible_commands, history_down, history_up
from cortexterm.tui.state import ScreenState, TtyAppArgs


@dataclass(frozen=True)
class NormalModeActions:
    submit_input: Callable[[TtyAppArgs, ScreenState, Callable[[], None], str], bool]
    toggle_read_mode: Callable[[TtyAppArgs, ScreenState], bool]
    scroll_by: Callable[[TtyAppArgs, ScreenState, int], bool]
    page_step: Callable[[TtyAppArgs, ScreenState], int]
    wheel_step: Callable[[TtyAppArgs, ScreenState], int]
    jump_to_edge: Callable[[TtyAppArgs, ScreenState, str], bool]


def handle_normal_mode_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    actions: NormalModeActions,
) -> None:
    """Handle input events when no modal state owns the terminal."""
    visible_commands = get_visible_commands(state.input)

    if isinstance(event, KeyEvent):
        if _handle_normal_mode_key(args, state, event, visible_commands, rerender, actions):
            return
    elif isinstance(event, TextEvent):
        if _handle_normal_mode_text(args, state, event, visible_commands, rerender, actions):
            return
    elif isinstance(event, WheelEvent):
        if handle_normal_mode_wheel(args, state, event, rerender, actions):
            return


def _handle_normal_mode_key(
    args: TtyAppArgs,
    state: ScreenState,
    event: KeyEvent,
    visible_commands: list,
    rerender: Callable[[], None],
    actions: NormalModeActions,
) -> bool:
    if event.name == "return":
        _handle_normal_mode_return(args, state, visible_commands, rerender, actions)
        return True

    if event.ctrl and event.name == "r":
        actions.toggle_read_mode(args, state)
        rerender()
        return True

    if event.name == "tab" and visible_commands:
        _handle_normal_mode_tab(state, visible_commands, rerender)
        return True

    if _handle_normal_mode_navigation(state, event, rerender):
        return True

    if event.name == "pageup" and actions.scroll_by(args, state, actions.page_step(args, state)):
        rerender()
        return True

    if event.name == "pagedown" and actions.scroll_by(args, state, -actions.page_step(args, state)):
        rerender()
        return True

    if event.name == "up" and event.meta:
        if actions.scroll_by(args, state, actions.page_step(args, state)):
            rerender()
        return True

    if event.name == "down" and event.meta:
        if actions.scroll_by(args, state, -actions.page_step(args, state)):
            rerender()
        return True

    if event.name == "up":
        _handle_up_arrow(state, visible_commands, rerender)
        return True

    if event.name == "down":
        _handle_down_arrow(state, visible_commands, rerender)
        return True

    return False


def _handle_normal_mode_return(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
    actions: NormalModeActions,
) -> None:
    if visible_commands and 0 <= state.selected_slash_index < len(visible_commands):
        selected = visible_commands[state.selected_slash_index]
        usage = getattr(selected, "usage", str(selected))
        if state.input.strip() != usage:
            state.input = usage
            state.cursor_offset = len(state.input)
            state.selected_slash_index = 0
            rerender()
            return

    submitted = state.input
    state.input = ""
    state.cursor_offset = 0
    state.selected_slash_index = 0
    if submitted.strip():
        state.show_welcome = False
    if actions.submit_input(args, state, rerender, submitted):
        raise SystemExit(0)
    rerender()


def _handle_normal_mode_tab(
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    selected = visible_commands[min(state.selected_slash_index, len(visible_commands) - 1)]
    usage = getattr(selected, "usage", str(selected))
    state.input = usage + " "
    state.cursor_offset = len(state.input)
    state.selected_slash_index = 0
    rerender()


def _handle_normal_mode_navigation(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
) -> bool:
    if event.name == "backspace" and state.cursor_offset > 0:
        state.input = state.input[: state.cursor_offset - 1] + state.input[state.cursor_offset :]
        state.cursor_offset -= 1
        state.selected_slash_index = 0
        rerender()
        return True

    if event.name == "delete" and state.cursor_offset < len(state.input):
        state.input = state.input[: state.cursor_offset] + state.input[state.cursor_offset + 1 :]
        state.selected_slash_index = 0
        rerender()
        return True

    if event.name == "home":
        state.cursor_offset = 0
        rerender()
        return True

    if event.name == "end":
        state.cursor_offset = len(state.input)
        rerender()
        return True

    if event.name == "left":
        state.cursor_offset = max(0, state.cursor_offset - 1)
        rerender()
        return True

    if event.name == "right":
        state.cursor_offset = min(len(state.input), state.cursor_offset + 1)
        rerender()
        return True

    if event.name == "escape":
        state.input = ""
        state.cursor_offset = 0
        state.selected_slash_index = 0
        rerender()
        return True

    return False


def _handle_up_arrow(
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index - 1 + len(visible_commands)) % len(visible_commands)
        rerender()
    elif history_up(state):
        rerender()


def _handle_down_arrow(
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index + 1) % len(visible_commands)
        rerender()
    elif history_down(state):
        rerender()


def _handle_normal_mode_text(
    args: TtyAppArgs,
    state: ScreenState,
    event: TextEvent,
    visible_commands: list,
    rerender: Callable[[], None],
    actions: NormalModeActions,
) -> bool:
    if event.ctrl:
        if event.text == "u":
            state.input = ""
            state.cursor_offset = 0
            state.selected_slash_index = 0
            rerender()
            return True

        if event.text == "a":
            if not state.input:
                if actions.jump_to_edge(args, state, "top"):
                    rerender()
                return True
            state.cursor_offset = 0
            rerender()
            return True

        if event.text == "e":
            if not state.input:
                if actions.jump_to_edge(args, state, "bottom"):
                    rerender()
                return True
            state.cursor_offset = len(state.input)
            rerender()
            return True

        if event.text == "p":
            if history_up(state):
                rerender()
            return True

        if event.text == "n":
            if history_down(state):
                rerender()
            return True

        return False

    if not event.ctrl and event.text:
        state.input = state.input[: state.cursor_offset] + event.text + state.input[state.cursor_offset :]
        state.cursor_offset += len(event.text)
        state.selected_slash_index = 0
        state.history_index = len(state.history)
        rerender()
        return True

    return False


def handle_normal_mode_wheel(
    args: TtyAppArgs,
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
    actions: NormalModeActions,
) -> bool:
    step = actions.wheel_step(args, state)
    delta = step if event.direction == "up" else -step
    if actions.scroll_by(args, state, delta):
        rerender()
        return True
    return False

