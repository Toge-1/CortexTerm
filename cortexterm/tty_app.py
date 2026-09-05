"""CortexTerm TTY application.

This file is the compatibility entrypoint for CortexTerm's full-screen terminal
UI.  It is now deliberately small enough to read as a composition layer:
session setup, terminal raw-mode lifecycle, the main input/render loop, and a
few compatibility exports used by tests and external callers.

When changing this module, keep these boundaries in mind:

1. Model context (`args.messages`) is not the same thing as the visible
   transcript.  The transcript may be collapsed or summarized for the user, but
   model messages must preserve tool calls/results for the next model request.
2. The main thread owns terminal input and rendering.  Slow agent work runs in a
   background thread so keyboard, mouse, approval prompts, and redraws stay
   responsive.
3. Rendering should stay display-focused.  It should not run agent logic, mutate
   permissions, or perform tool execution.
4. Event handlers should route based on mode: normal input, permission approval,
   transcript reading, or feedback.
5. Runtime actions, background agent turns, event loop mechanics, and session
   persistence live under ``cortexterm.tui`` modules.

See ``docs/tui-architecture.md`` for the current flow and the intended split.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable

from cortexterm.agent_loop import run_agent_turn
from cortexterm.history import save_history_entries
from cortexterm.permissions import PermissionManager
from cortexterm.tooling import ToolRegistry
from cortexterm.tui.chrome import (
    _cached_terminal_size,
)
from cortexterm.tui.event_loop import run_terminal_event_loop
from cortexterm.tui.input_parser import (
    KeyEvent,
    MouseEvent,
    ParsedInputEvent,
    TextEvent,
    WheelEvent,
)
from cortexterm.tui.modes.approval import handle_pending_approval_event as _handle_pending_approval_event
from cortexterm.tui.modes.normal import (
    NormalModeActions,
    handle_normal_mode_event as _handle_normal_mode_event_impl,
    handle_normal_mode_wheel as _handle_normal_mode_wheel_impl,
)
from cortexterm.tui.modes.read import handle_transcript_read_mode_event as _handle_transcript_read_mode_event
from cortexterm.tui.rendering import ThrottledRenderer as _ThrottledRenderer
from cortexterm.tui.render_session import (
    get_chrome_overhead as _get_chrome_overhead,
    get_transcript_body_lines as _get_transcript_body_lines,
    install_transcript_mouse_zones as _install_transcript_mouse_zones,
    jump_transcript_to_edge as _jump_transcript_to_edge,
    render_screen as _render_screen,
    scroll_transcript_by as _scroll_transcript_by,
    toggle_transcript_read_mode as _toggle_transcript_read_mode,
    transcript_page_step as _transcript_page_step,
)
from cortexterm.tui.state import (
    MouseZone,
    PendingApproval,
    ScreenState,
    TtyAppArgs,
)
from cortexterm.tui.runtime import (
    execute_tool_shortcut as _execute_tool_shortcut,
    handle_input as _handle_input_impl,
)
from cortexterm.tui.session_lifecycle import (
    bootstrap_screen_state,
    save_final_session,
)
from cortexterm.tui.tool_cards import (
    finalize_dangling_running_tools as _finalize_dangling_running_tools,
    get_running_tool_entries as _get_running_tool_entries,
    handle_mouse_event as _handle_mouse_event,
)
from cortexterm.tui.transcript_ops import (
    apply_tool_result_visual_state as _apply_tool_result_visual_state,
    format_history as _format_history,
    mark_unfinished_tools as _mark_unfinished_tools,
    save_transcript as _save_transcript,
    summarize_tool_input,
    summarize_tool_output,
)
from cortexterm.tui.screen import (
    enter_alternate_screen,
    exit_alternate_screen,
    hide_cursor,
    show_cursor,
)
from cortexterm.types import ChatMessage, ModelAdapter


def _configure_tty_stdio() -> None:
    """Make direct TTY entrypoint calls robust on Windows consoles.

    ``main.py`` also configures stdio, but tests and external callers can call
    ``run_tty_app`` directly.  Rendering must not crash just because stdout is
    still using a legacy code page such as GBK.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Terminal size — use unified cache from chrome module
# ---------------------------------------------------------------------------

# Alias to the single canonical implementation in chrome.py
_get_terminal_size = _cached_terminal_size


def _transcript_wheel_step(args: TtyAppArgs, state: ScreenState) -> int:
    """Compatibility wrapper for tests that monkeypatch tty_app layout helpers."""
    _, rows = _get_terminal_size()
    rows = max(24, rows)
    body_lines = max(6, rows - (_get_chrome_overhead(args, state) + 4))
    return max(1, min(4, body_lines // 6))


# ---------------------------------------------------------------------------
# Throttled renderer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Runtime input actions
# ---------------------------------------------------------------------------


def _handle_input(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    submitted_raw_input: str | None = None,
) -> bool:
    """Compatibility wrapper that keeps tty_app.run_agent_turn monkeypatchable."""
    return _handle_input_impl(
        args,
        state,
        rerender,
        submitted_raw_input,
        run_agent_turn_func=run_agent_turn,
        save_history_func=save_history_entries,
    )


# ---------------------------------------------------------------------------
# Main event-driven TTY app
# ---------------------------------------------------------------------------


def run_tty_app(
    *,
    runtime: dict | None,
    tools: ToolRegistry,
    model: ModelAdapter,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager,
    memory_mgr: Any | None = None,
    context_mgr: Any | None = None,
    resume_session: str | None = None,
    list_sessions_only: bool = False,
) -> list[ChatMessage]:
    """Event-driven full-screen TTY application, ported from the TypeScript version.
    
    Args:
        resume_session: Session ID to resume, or "latest" for most recent
        list_sessions_only: If True, print session list and exit
    """
    _configure_tty_stdio()

    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=model,
        messages=messages,
        cwd=cwd,
        permissions=permissions,
        memory_mgr=memory_mgr,
        context_mgr=context_mgr,
    )

    state = bootstrap_screen_state(
        args,
        runtime=runtime,
        cwd=cwd,
        tools=tools,
        permissions=permissions,
        resume_session=resume_session,
        list_sessions_only=list_sessions_only,
    )
    if state is None:
        return messages

    # Wire up permission prompt handler
    approval_event = threading.Event()
    approval_result: dict[str, Any] = {}

    def _permission_prompt_handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal approval_result
        state.pending_approval = PendingApproval(
            request=request,
            resolve=lambda r: None,
        )
        # Signal the main thread's throttled renderer to show the approval UI.
        # Do NOT call _render_screen() here — we're on the agent thread and
        # writing to stdout concurrently with the main thread would corrupt
        # the terminal display.  request() only sets a pending flag; the main
        # event loop's next flush() will do the actual render safely.
        rerender()
        approval_event.clear()
        approval_event.wait()
        result = approval_result.copy()
        state.pending_approval = None
        return result

    permissions.prompt = _permission_prompt_handler

    # Throttled renderer: coalesces rapid rerender() calls to reduce flickering
    throttled = _ThrottledRenderer(lambda: _render_screen(args, state), min_interval=0.016)

    def rerender() -> None:
        throttled.request()

    enter_alternate_screen()
    hide_cursor()

    # On Unix, listen for SIGWINCH so terminal resizes are picked up
    # immediately rather than waiting for the 0.5s cache TTL.
    # signal.signal() can only be called from the main thread.
    _prev_sigwinch = None
    if (
        sys.platform != "win32"
        and threading.current_thread() is threading.main_thread()
    ):
        import signal as _signal

        from cortexterm.tui.chrome import invalidate_terminal_size_cache

        def _on_sigwinch(_signum: int, _frame: Any) -> None:
            invalidate_terminal_size_cache()
            throttled.request()

        try:
            _prev_sigwinch = _signal.signal(_signal.SIGWINCH, _on_sigwinch)
        except (OSError, ValueError):
            # Couldn't set signal handler (e.g. not main thread despite check)
            _prev_sigwinch = None

    try:
        _render_screen(args, state)
        run_terminal_event_loop(
            args=args,
            state=state,
            renderer=throttled,
            rerender=rerender,
            approval_event=approval_event,
            approval_result=approval_result,
            handle_event=_handle_event,
        )

    finally:
        # Restore previous SIGWINCH handler on Unix
        if _prev_sigwinch is not None and sys.platform != "win32":
            import signal as _signal

            _signal.signal(_signal.SIGWINCH, _prev_sigwinch)

        show_cursor()
        exit_alternate_screen()
        save_final_session(args, state, permissions=permissions, tools=tools)

    return args.messages


def _handle_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Process a single parsed input event.
    
    Routes the event to the appropriate handler based on current state:
    - Ctrl+C: Exit immediately
    - Pending approval: Handle permission dialog input
    - Normal mode: Handle input, navigation, and commands
    
    Args:
        args: Application arguments (tools, model, permissions)
        state: Current screen state
        event: Parsed input event from terminal
        rerender: Function to trigger screen redraw
        approval_event: Threading event for approval synchronization
        approval_result: Dict to store approval decision
    """
    # ---------- Ctrl+C → exit ----------
    # \x03 is parsed as KeyEvent(name='c', ctrl=True) by parse_input_chunk
    # (CTRL_CHAR_TO_NAME maps \x03 → 'c', produces KeyEvent not TextEvent)
    if isinstance(event, KeyEvent) and event.ctrl and event.name == "c":
        raise SystemExit(0)
    if isinstance(event, TextEvent) and event.ctrl and event.text == "c":
        raise SystemExit(0)

    # ---------- Pending approval mode ----------
    # Capture locally to avoid TOCTOU — the agent thread may clear
    # state.pending_approval between our check and the handler's use.
    pending = state.pending_approval
    if pending is not None:
        _handle_pending_approval_event(state, pending, event, rerender, approval_event, approval_result)
        return

    if isinstance(event, MouseEvent) and _handle_mouse_event(state, event, rerender):
        return

    if state.transcript_read_mode:
        _handle_transcript_read_mode_event(
            args,
            state,
            event,
            rerender,
            scroll_by=_scroll_transcript_by,
            page_step=_transcript_page_step,
            wheel_step=_transcript_wheel_step,
            jump_to_edge=_jump_transcript_to_edge,
        )
        return

    # ---------- Normal mode ----------
    _handle_normal_mode_event_impl(args, state, event, rerender, _normal_mode_actions())


# ---------------------------------------------------------------------------
# Normal mode event handlers
# ---------------------------------------------------------------------------


def _normal_mode_actions() -> NormalModeActions:
    return NormalModeActions(
        submit_input=_handle_input,
        toggle_read_mode=_toggle_transcript_read_mode,
        scroll_by=_scroll_transcript_by,
        page_step=_transcript_page_step,
        wheel_step=_transcript_wheel_step,
        jump_to_edge=_jump_transcript_to_edge,
    )


def _handle_normal_mode_wheel(
    args: TtyAppArgs,
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    """Backward-compatible wrapper for tests and external callers."""
    return _handle_normal_mode_wheel_impl(args, state, event, rerender, _normal_mode_actions())


# ---------------------------------------------------------------------------
# Public API / backward-compatible exports for tests
# ---------------------------------------------------------------------------


