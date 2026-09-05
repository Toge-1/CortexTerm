"""Terminal raw-mode and low-level input helpers for the TTY app."""

from __future__ import annotations

import os
import sys
from typing import Any


_WIN_SCANCODE_TO_ANSI: dict[int, str] = {
    72: "\x1b[A",
    80: "\x1b[B",
    77: "\x1b[C",
    75: "\x1b[D",
    71: "\x1b[H",
    79: "\x1b[F",
    73: "\x1b[5~",
    81: "\x1b[6~",
    83: "\x1b[3~",
    82: "\x1b[2~",
    152: "\x1b[1;3A",
    160: "\x1b[1;3B",
    157: "\x1b[1;3C",
    155: "\x1b[1;3D",
    141: "\x1b[1;5A",
    145: "\x1b[1;5B",
    116: "\x1b[1;5C",
    115: "\x1b[1;5D",
}


def win_read_one_key() -> str:
    """Read one logical key from Windows msvcrt.

    Special keys are translated into ANSI escape sequences so the common input
    parser can handle Windows and POSIX terminals through the same event model.
    Returns an empty string if no key is available.
    """
    import msvcrt

    if not msvcrt.kbhit():
        return ""

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            scan = ord(msvcrt.getwch())
        else:
            return "\x1b"
        return _WIN_SCANCODE_TO_ANSI.get(scan, "")
    return ch


def read_raw_char() -> str:
    """Read a single character from stdin in raw mode, cross-platform."""
    if sys.platform == "win32":
        return win_read_one_key()

    import select

    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], 0.05)
    if ready:
        data = os.read(fd, 4096)
        return data.decode("utf-8", errors="replace") if data else ""
    return ""


def read_raw_chunk() -> str:
    """Read all available raw chars as a single chunk."""
    if sys.platform == "win32":
        result = ""
        while True:
            ch = win_read_one_key()
            if not ch:
                break
            result += ch
        return result

    import select

    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], 0.05)
    if not ready:
        return ""
    data = os.read(fd, 4096)
    if not data:
        return ""
    while True:
        ready2, _, _ = select.select([fd], [], [], 0)
        if not ready2:
            break
        more = os.read(fd, 4096)
        if not more:
            break
        data += more
    return data.decode("utf-8", errors="replace")


class RawModeContext:
    """Context manager for raw terminal mode."""

    def __init__(self) -> None:
        self._old_settings: Any = None
        self._old_cp: int | None = None

    def __enter__(self) -> RawModeContext:
        if sys.platform == "win32":
            from cortexterm.tui.screen import _enable_windows_vt_processing

            _enable_windows_vt_processing()
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                self._old_cp = kernel32.GetConsoleOutputCP()
                kernel32.SetConsoleOutputCP(65001)
            except Exception:
                pass
        else:
            import termios

            fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[0] &= ~(
                termios.BRKINT
                | termios.ICRNL
                | termios.INPCK
                | termios.ISTRIP
                | termios.IXON
            )
            new[2] &= ~(termios.CSIZE | termios.PARENB)
            new[2] |= termios.CS8
            new[3] &= ~(
                termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG
            )
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        return self

    def __exit__(self, *_: Any) -> None:
        if sys.platform == "win32":
            if self._old_cp is not None:
                try:
                    import ctypes

                    ctypes.windll.kernel32.SetConsoleOutputCP(self._old_cp)  # type: ignore[attr-defined]
                except Exception:
                    pass
        elif self._old_settings is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
