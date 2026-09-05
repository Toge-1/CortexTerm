"""Terminal event loop for the full-screen TTY app."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable

from cortexterm.tui.input_parser import ParsedInputEvent, parse_input_chunk
from cortexterm.tui.state import ScreenState, TtyAppArgs
from cortexterm.tui.terminal import RawModeContext, win_read_one_key


AUTOSAVE_CHECK_INTERVAL = 100  # iterations; roughly every 2-5s depending on idle polling.


def run_terminal_event_loop(
    *,
    args: TtyAppArgs,
    state: ScreenState,
    renderer: Any,
    rerender: Callable[[], None],
    approval_event: Any,
    approval_result: dict[str, Any],
    handle_event: Callable[
        [TtyAppArgs, ScreenState, ParsedInputEvent, Callable[[], None], Any, dict[str, Any]],
        None,
    ],
) -> None:
    """Run the raw terminal input loop until the user exits."""
    input_remainder = ""
    should_exit = False
    autosave_counter = 0

    with RawModeContext():
        while not should_exit:
            autosave_counter = _autosave_tick(state, autosave_counter)
            _harvest_completed_agent_turn(args, state)

            chunk, eof = _read_input_chunk(renderer)
            if eof:
                break
            if not chunk:
                continue

            parsed = parse_input_chunk(input_remainder + chunk)
            input_remainder = parsed.rest

            for event in parsed.events:
                try:
                    handle_event(args, state, event, rerender, approval_event, approval_result)
                    if state.input == "/exit":
                        raise SystemExit(0)
                except SystemExit:
                    should_exit = True
                    break
                except Exception as exc:
                    logging.debug("Event handling error: %s", exc, exc_info=True)

            renderer.flush()


def _autosave_tick(state: ScreenState, counter: int) -> int:
    counter += 1
    if state.autosave and counter >= AUTOSAVE_CHECK_INTERVAL:
        state.autosave.save_if_needed()
        return 0
    return counter


def _harvest_completed_agent_turn(args: TtyAppArgs, state: ScreenState) -> None:
    agent_result_data = state.agent_result
    lock = getattr(state, "agent_lock", None)
    if agent_result_data is None or lock is None or not agent_result_data.get("done"):
        return

    with lock:
        if agent_result_data.get("messages"):
            args.messages = agent_result_data["messages"]
        agent_result_data["done"] = False


def _read_input_chunk(renderer: Any) -> tuple[str, bool]:
    if sys.platform == "win32":
        return _read_windows_input_chunk(renderer), False
    return _read_posix_input_chunk(renderer)


def _read_windows_input_chunk(renderer: Any) -> str:
    import msvcrt

    if not msvcrt.kbhit():
        renderer.flush()
        time.sleep(0.05)
        return ""

    chunk = ""
    while True:
        ch = win_read_one_key()
        if not ch:
            break
        chunk += ch
    return chunk


def _read_posix_input_chunk(renderer: Any) -> tuple[str, bool]:
    import select

    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], 0.05)
    if not ready:
        renderer.flush()
        return "", False

    raw = os.read(fd, 4096)
    if not raw:
        return "", True

    while True:
        ready2, _, _ = select.select([fd], [], [], 0)
        if not ready2:
            break
        more = os.read(fd, 4096)
        if not more:
            break
        raw += more

    return raw.decode("utf-8", errors="replace"), False
