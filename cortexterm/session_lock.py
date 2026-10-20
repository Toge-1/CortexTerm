"""Process-level ownership of a local session, using only the standard library."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO


class SessionInUseError(OSError):
    """Another owner already holds this session's lock."""


class SessionLock:
    """Hold a stable sidecar file lock while a session is being edited.

    The JSON itself is atomically replaced on save, so locking that file would
    not protect subsequent replacements on Unix. Never unlink the sidecar:
    doing so could let different processes lock different underlying files.
    An abandoned .lock file is harmless; ownership lives in the OS, not in its
    existence or a recorded PID. Closing the handle/process releases ownership.
    """

    def __init__(self, session_path: Path):
        self.session_path = session_path.resolve()
        self.lock_path = self.session_path.with_name(f".{self.session_path.name}.lock")
        self._handle: BinaryIO | None = None
        self._owner_pid: int | None = None

    def acquire(self) -> SessionLock:
        if self._handle is not None:
            raise RuntimeError("This session lock has already been acquired")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                # Windows permits byte-range locks beyond EOF, including an
                # empty lock file. Nonblocking acquisition fails immediately.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle = handle
            self._owner_pid = os.getpid()
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise SessionInUseError(
                    f"Session '{self.session_path.stem}' is already in use by another "
                    "CortexTerm instance. Close that instance before resuming, "
                    "or start without --resume to create a new session."
                ) from error
            raise
        except BaseException:
            handle.close()
            raise
        return self

    def holds(self, session_path: Path) -> bool:
        return (
            self._handle is not None
            and not self._handle.closed
            and self._owner_pid == os.getpid()
            and self.session_path == session_path.resolve()
        )

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            # A forked child must not explicitly unlock its parent's lease.
            if self._owner_pid == os.getpid():
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._owner_pid = None

    def __enter__(self) -> SessionLock:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()
