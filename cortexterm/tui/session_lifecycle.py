"""Session bootstrap and final-save helpers for the TTY app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cortexterm.cost_tracker import CostTracker
from cortexterm.history import load_history_entries
from cortexterm.permissions import PermissionManager
from cortexterm.session import (
    AutosaveManager,
    SessionData,
    acquire_session_lock,
    create_new_session,
    format_session_list,
    format_session_resume,
    list_sessions,
    load_session,
    save_session,
)
from cortexterm.session_lock import SessionInUseError, SessionLock
from cortexterm.state import create_app_store
from cortexterm.tooling import ToolRegistry
from cortexterm.tui.state import ScreenState, TtyAppArgs
from cortexterm.tui.types import TranscriptEntry


def bootstrap_screen_state(
    args: TtyAppArgs,
    *,
    runtime: dict | None,
    cwd: str,
    tools: ToolRegistry,
    permissions: PermissionManager,
    resume_session: str | None,
    list_sessions_only: bool,
) -> ScreenState | None:
    """Create the initial ``ScreenState`` or return None when startup exits early."""
    if list_sessions_only:
        sessions = list_sessions()
        print(format_session_list(sessions))
        return None

    workspace = str(Path(cwd).resolve())
    try:
        selected = _select_session(workspace, resume_session)
    except SessionInUseError as error:
        print(str(error))
        return None
    except OSError as error:
        print(f"Unable to acquire the session lock: {error}")
        return None
    if selected is None:
        return None
    session, session_lock = selected

    try:
        app_state_store = create_app_store(
            {
                "session_id": session.session_id,
                "workspace": cwd,
                "model": runtime.get("model", "unknown") if runtime else "unknown",
            }
        )

        state = ScreenState(
            history=load_history_entries(),
            session=session,
            session_lock=session_lock,
            autosave=AutosaveManager(session, session_lock=session_lock),
            app_state=app_state_store,
            cost_tracker=CostTracker(),
        )
        state.history_index = len(state.history)

        _restore_session(args, state)
        return state
    except BaseException:
        session_lock.release()
        raise


def save_final_session(
    args: TtyAppArgs,
    state: ScreenState,
    *,
    permissions: PermissionManager,
    tools: ToolRegistry,
) -> None:
    """Persist the final session snapshot after the TTY exits."""
    if not state.session:
        return

    state.session.messages = list(args.messages)
    state.session.transcript_entries = [
        {
            "id": entry.id,
            "kind": entry.kind,
            "toolName": entry.toolName,
            "status": entry.status,
            "body": entry.body,
            "collapsed": entry.collapsed,
            "collapsedSummary": entry.collapsedSummary,
            "collapsePhase": entry.collapsePhase,
        }
        for entry in state.transcript
    ]
    state.session.history = state.history
    state.session.permissions_summary = permissions.get_summary()
    state.session.skills = tools.get_skills()
    state.session.mcp_servers = tools.get_mcp_servers()

    if not state.session.has_meaningful_content():
        print("\nSession not saved: empty session.")
        return

    saved = (
        state.autosave.force_save() if state.autosave
        else save_session(state.session, session_lock=state.session_lock)
    )
    if saved:
        print(f"\nSession saved: {state.session.session_id[:8]}")
    else:
        print("\nSession was not saved: unable to write session files.")


def _select_session(
    workspace: str, resume_session: str | None,
) -> tuple[SessionData, SessionLock] | None:
    """Select an ID, then acquire ownership BEFORE loading editable state."""
    selected_id = resume_session
    latest = None
    if resume_session in (None, "latest"):
        latest = next((meta for meta in list_sessions() if meta.workspace == workspace), None)
    if resume_session == "latest":
        selected_id = latest.session_id if latest else None
        if latest is None:
            print("No previous session found for this workspace.")
    elif not resume_session and latest:
        print(f"Previous session found: {latest.session_id[:8]}")
        print("Use --resume to continue, or starting fresh session.")

    if selected_id:
        session_lock = acquire_session_lock(selected_id)
        try:
            session = load_session(selected_id)
            if session is None:
                print(f"Session '{selected_id}' not found or invalid.")
                session_lock.release()
                return None
            # A hand-edited/corrupt payload must not switch the save target to
            # another session for which this process does not hold a lock.
            if session.session_id != selected_id:
                print(f"Session '{selected_id}' has a mismatched ID; not resumed.")
                session_lock.release()
                return None
            print(format_session_resume(session))
            return session, session_lock
        except BaseException:
            session_lock.release()
            raise

    session = create_new_session(workspace=workspace)
    return session, acquire_session_lock(session.session_id)


def _restore_session(args: TtyAppArgs, state: ScreenState) -> None:
    session = state.session
    if not session:
        return

    if session.messages:
        args.messages.clear()
        args.messages.extend(session.messages)

    for entry_data in session.transcript_entries:
        state.transcript.append(TranscriptEntry(**entry_data))

    if state.transcript:
        state.next_entry_id = max(entry.id for entry in state.transcript) + 1

    if session.has_meaningful_content():
        state.show_welcome = False
        state.transcript_scroll_offset = 0

    print(
        f"Restored {len(session.messages)} messages, "
        f"{len(state.transcript)} transcript entries."
    )
