"""Session ownership tests, including actual competing OS processes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from cortexterm import session as sessions
from cortexterm import tty_app
from cortexterm.session_lock import SessionInUseError
from cortexterm.tooling import ToolRegistry
from cortexterm.tui import session_lifecycle as lifecycle
from cortexterm.tui.state import TtyAppArgs


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CORTEXTERM_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(lifecycle, "load_history_entries", lambda: [])
    return tmp_path


def saved_session(store):
    session = sessions.create_new_session(str(store.resolve()))
    session.messages = [{"role": "user", "content": "original"}]
    assert sessions.save_session(session)
    return session


class Permissions:
    prompt = None

    def get_summary(self):
        return []


def app_args(store):
    return TtyAppArgs(
        runtime={}, tools=ToolRegistry([]), model=object(),
        messages=[{"role": "system", "content": "initial"}],
        cwd=str(store), permissions=Permissions(),
    )


def bootstrap(args, resume):
    return lifecycle.bootstrap_screen_state(
        args, runtime=args.runtime, cwd=args.cwd, tools=args.tools,
        permissions=args.permissions, resume_session=resume, list_sessions_only=False,
    )


_OWNER_PROGRAM = """
import sys
from pathlib import Path
from cortexterm import session as sessions
sessions.CORTEXTERM_DIR = Path(sys.argv[1])
sessions.SESSIONS_DIR = sessions.CORTEXTERM_DIR / 'sessions'
lock = sessions.acquire_session_lock(sys.argv[2])
try:
    current = sessions.load_session(sys.argv[2])
    print('ready', flush=True)
    command = sys.stdin.readline().strip()
    if command == 'save':
        current.messages.append({'role': 'assistant', 'content': 'owner update'})
        assert sessions.save_session(current, session_lock=lock)
finally:
    lock.release()
"""


@contextmanager
def owner_process(store, session_id):
    # Pipes + CREATE_NO_WINDOW keep these test-only workers off the desktop.
    process = subprocess.Popen(
        [sys.executable, "-c", _OWNER_PROGRAM, str(store), session_id],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        assert ready.get(timeout=10).strip() == "ready"
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("resume_latest", [False, True], ids=["explicit-id", "latest"])
def test_other_process_cannot_resume_overwrite_or_delete_active_session(store, capsys, resume_latest):
    original = saved_session(store)
    stale = sessions.load_session(original.session_id)
    stale.messages.append({"role": "user", "content": "must not overwrite owner"})
    before = sessions._session_file(original.session_id).read_bytes()

    with owner_process(store, original.session_id) as owner:
        args = app_args(store)
        state = bootstrap(args, "latest" if resume_latest else original.session_id)
        assert state is None
        assert "already in use" in capsys.readouterr().out
        assert args.messages == [{"role": "system", "content": "initial"}]
        assert not sessions.save_session(stale)
        assert not sessions.delete_session(original.session_id)
        assert sessions.cleanup_old_sessions(max_sessions=0) == 0
        assert sessions._session_file(original.session_id).read_bytes() == before
        assert sessions.load_session(original.session_id).messages == original.messages

        # Other sessions remain usable while the original session is locked.
        other_args = app_args(store)
        other = bootstrap(other_args, None)
        assert other is not None and other.session.session_id != original.session_id
        other.session_lock.release()

        owner.communicate(input="save\n", timeout=10)
        assert owner.returncode == 0

    resumed_args = app_args(store)
    resumed = bootstrap(resumed_args, original.session_id)
    try:
        assert resumed is not None
        assert resumed_args.messages[-1]["content"] == "owner update"
    finally:
        if resumed is not None:
            resumed.session_lock.release()


def test_force_killed_owner_does_not_leave_stale_ownership(store):
    original = saved_session(store)
    with owner_process(store, original.session_id) as owner:
        with pytest.raises(SessionInUseError):
            sessions.acquire_session_lock(original.session_id)
        owner.kill()
        owner.wait(timeout=10)

    lock = sessions.acquire_session_lock(original.session_id)
    try:
        assert lock.lock_path.exists()  # File existence is not ownership.
        assert lock.holds(sessions._session_file(original.session_id))
        assert sessions.load_session(original.session_id).messages == original.messages
    finally:
        lock.release()


def test_autosave_uses_owner_lock_and_rejects_wrong_or_released_locks(store):
    session = saved_session(store)
    lock = sessions.acquire_session_lock(session.session_id)
    try:
        manager = sessions.AutosaveManager(session, interval=0, session_lock=lock)
        session.messages.append({"role": "assistant", "content": "saved under owner lock"})
        manager.mark_dirty()
        assert manager.save_if_needed()
        assert sessions.load_session(session.session_id).messages == session.messages
        other = sessions.create_new_session(str(store))
        other.messages = [{"role": "user", "content": "other"}]
        assert not sessions.save_session(other, session_lock=lock)
    finally:
        lock.release()
    assert not manager.force_save()  # Do not silently reacquire after ownership ended.


def test_failed_restore_releases_lock(store, monkeypatch):
    session = saved_session(store)

    def fail(*args):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(lifecycle, "_restore_session", fail)
    with pytest.raises(RuntimeError, match="restore failed"):
        bootstrap(app_args(store), session.session_id)
    lock = sessions.acquire_session_lock(session.session_id)
    lock.release()


@pytest.mark.parametrize("invalid", ["missing", "corrupt", "mismatched-id"])
def test_invalid_session_releases_ownership(store, invalid):
    session = saved_session(store)
    path = sessions._session_file(session.session_id)
    if invalid == "missing":
        path.unlink()
    elif invalid == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["session_id"] = "some-other-session"
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert bootstrap(app_args(store), session.session_id) is None
    lock = sessions.acquire_session_lock(session.session_id)
    lock.release()


@pytest.mark.parametrize("failure_stage", [None, "setup", "loop", "save"])
def test_tty_keeps_lock_through_final_save_and_releases_on_all_exits(store, monkeypatch, failure_stage):
    session = saved_session(store)
    for name in ("_configure_tty_stdio", "enter_alternate_screen", "hide_cursor", "show_cursor", "exit_alternate_screen"):
        monkeypatch.setattr(tty_app, name, lambda: None)
    monkeypatch.setattr(tty_app, "_render_screen", lambda *args: None)

    def fail():
        raise RuntimeError("injected failure")

    def run_loop(**kwargs):
        with pytest.raises(SessionInUseError):
            sessions.acquire_session_lock(session.session_id)
        kwargs["args"].messages.append({"role": "assistant", "content": "final snapshot"})
        if failure_stage == "loop":
            fail()

    saved = []
    real_save = lifecycle.save_final_session

    def final_save(*args, **kwargs):
        with pytest.raises(SessionInUseError):
            sessions.acquire_session_lock(session.session_id)
        if failure_stage == "save":
            fail()
        real_save(*args, **kwargs)
        saved.append(True)

    monkeypatch.setattr(tty_app, "run_terminal_event_loop", run_loop)
    monkeypatch.setattr(tty_app, "save_final_session", final_save)
    if failure_stage == "setup":
        monkeypatch.setattr(tty_app, "enter_alternate_screen", fail)
    args = app_args(store)

    def run_app():
        return tty_app.run_tty_app(
            runtime={}, tools=args.tools, model=args.model, messages=args.messages,
            cwd=str(store), permissions=args.permissions, resume_session=session.session_id,
        )

    if failure_stage is None:
        run_app()
    else:
        with pytest.raises(RuntimeError, match="injected failure"):
            run_app()
    if failure_stage in (None, "loop"):
        assert saved
        assert sessions.load_session(session.session_id).messages[-1]["content"] == "final snapshot"
    lock = sessions.acquire_session_lock(session.session_id)
    lock.release()
