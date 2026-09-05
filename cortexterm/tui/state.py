"""State objects for the full-screen TTY app.

These dataclasses intentionally contain no rendering or agent logic. They are
the shared vocabulary between event handlers, renderers, permission UI, and the
background agent runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from cortexterm.cost_tracker import CostTracker
from cortexterm.permissions import PermissionManager
from cortexterm.session import AutosaveManager, SessionData
from cortexterm.state import AppState, Store
from cortexterm.tooling import ToolRegistry
from cortexterm.tui.types import TranscriptEntry
from cortexterm.types import ChatMessage, ModelAdapter


@dataclass
class TtyAppArgs:
    runtime: dict | None
    tools: ToolRegistry
    model: ModelAdapter
    messages: list[ChatMessage]
    cwd: str
    permissions: PermissionManager
    memory_mgr: Any | None = None
    context_mgr: Any | None = None


@dataclass
class PendingApproval:
    request: dict[str, Any]
    resolve: Callable[[dict[str, Any]], None]
    details_expanded: bool = False
    details_scroll_offset: int = 0
    selected_choice_index: int = 0
    feedback_mode: bool = False
    feedback_input: str = ""


@dataclass
class AggregatedEditProgress:
    entry_id: int
    tool_name: str
    path: str
    total: int = 1
    completed: int = 0
    errors: int = 0
    last_output: str = ""


@dataclass
class MouseZone:
    y_start: int
    y_end: int
    entry_id: int
    action: str


@dataclass
class ScreenState:
    input: str = ""
    cursor_offset: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    transcript_scroll_offset: int = 0
    transcript_read_mode: bool = False
    selected_slash_index: int = 0
    status: str | None = None
    active_tool: str | None = None
    recent_tools: list[dict[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    history_index: int = 0
    history_draft: str = ""
    next_entry_id: int = 1
    pending_approval: PendingApproval | None = None
    is_busy: bool = False
    session: SessionData | None = None
    autosave: AutosaveManager | None = None
    app_state: Store[AppState] | None = None
    cost_tracker: CostTracker | None = None
    agent_thread: Any = None
    agent_result: dict | None = None
    agent_lock: Any = None
    tool_start_time: float | None = None
    mouse_zones: list[MouseZone] = field(default_factory=list)
    focused_entry_id: int | None = None
    show_welcome: bool = True
    manually_toggled_tools: set[int] = field(default_factory=set)
