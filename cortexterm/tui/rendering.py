"""Rendering coordination helpers for the TTY app."""

from __future__ import annotations

import threading
import time
from typing import Callable


class ThrottledRenderer:
    """Coalesce rapid rerender requests into bounded terminal redraws.

    The actual render function only runs from ``flush()`` or ``force()``.
    Background threads may safely call ``request()`` without writing to stdout.
    """

    __slots__ = ("_render_fn", "_min_interval", "_pending", "_last_render_time", "_lock")

    def __init__(self, render_fn: Callable[[], None], min_interval: float = 0.033) -> None:
        self._render_fn = render_fn
        self._min_interval = min_interval
        self._pending = False
        self._last_render_time: float = 0.0
        self._lock = threading.Lock()

    def request(self) -> None:
        with self._lock:
            self._pending = True

    def flush(self) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._pending:
                return
            elapsed = now - self._last_render_time
            if elapsed < self._min_interval:
                return
            self._pending = False
            self._last_render_time = now
        self._render_fn()

    def force(self) -> None:
        with self._lock:
            self._pending = False
            self._last_render_time = time.monotonic()
        self._render_fn()

