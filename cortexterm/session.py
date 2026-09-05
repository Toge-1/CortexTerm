"""Session persistence and resume module.

Provides session data structures, autosave mechanism, and resume capabilities
to allow CortexTerm to save and restore conversation state across restarts.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortexterm.config import CORTEXTERM_DIR


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSIONS_DIR = CORTEXTERM_DIR / "sessions"
AUTOSAVE_INTERVAL_SECONDS = 30  # Minimum seconds between autosaves


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SessionMetadata:
    """Lightweight metadata for session listing."""
    session_id: str
    created_at: float  # Unix timestamp
    updated_at: float  # Unix timestamp
    first_message: str = ""  # Truncated first user message
    last_message: str = ""   # Truncated last message
    message_count: int = 0
    workspace: str = ""      # Working directory when session started


@dataclass
class SessionData:
    """Complete session state that can be persisted and restored."""
    session_id: str
    created_at: float
    updated_at: float
    workspace: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript_entries: list[dict[str, Any]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    permissions_summary: dict[str, Any] = field(default_factory=dict)
    skills: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    metadata: SessionMetadata = field(default=None)

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = SessionMetadata(
                session_id=self.session_id,
                created_at=self.created_at,
                updated_at=self.updated_at,
                message_count=len(self.messages),
                workspace=self.workspace,
            )

    def update_metadata(self) -> None:
        """Refresh metadata from current state."""
        self.updated_at = time.time()
        self.metadata.updated_at = self.updated_at
        self.metadata.message_count = _meaningful_message_count(self.messages)
        self.metadata.first_message = _first_session_message(self.messages, self.transcript_entries)
        self.metadata.last_message = _last_session_message(self.messages, self.transcript_entries)

    def has_meaningful_content(self) -> bool:
        """Return True when this session contains actual user-visible work."""
        return _session_has_meaningful_content(self.messages, self.transcript_entries)


# ---------------------------------------------------------------------------
# Session file operations
# ---------------------------------------------------------------------------

def _session_file(session_id: str) -> Path:
    """Return path to a session JSON file."""
    return SESSIONS_DIR / f"{session_id}.json"


def _session_index_file() -> Path:
    """Return path to the session index file."""
    return CORTEXTERM_DIR / "sessions_index.json"


def _content_to_text(content: Any) -> str:
    """Convert model/transcript content into a short plain-text string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return str(text).strip() if text else ""
    return str(content).strip()


def _truncate_summary(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text[:limit]


def _message_text(message: dict[str, Any]) -> str:
    return _content_to_text(message.get("content"))


def _transcript_text(entry: dict[str, Any]) -> str:
    return _content_to_text(entry.get("body"))


def _is_meaningful_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if role == "system":
        return False
    return bool(_message_text(message))


def _is_meaningful_transcript_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return bool(_transcript_text(entry))


def _meaningful_message_count(messages: list[dict[str, Any]]) -> int:
    return sum(1 for message in messages if _is_meaningful_message(message))


def _session_has_meaningful_content(
    messages: list[dict[str, Any]],
    transcript_entries: list[dict[str, Any]],
) -> bool:
    return any(_is_meaningful_message(message) for message in messages) or any(
        _is_meaningful_transcript_entry(entry) for entry in transcript_entries
    )


def _first_session_message(
    messages: list[dict[str, Any]],
    transcript_entries: list[dict[str, Any]],
) -> str:
    for entry in transcript_entries:
        if isinstance(entry, dict) and entry.get("kind") == "user":
            text = _transcript_text(entry)
            if text:
                return _truncate_summary(text)
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            text = _message_text(message)
            if text:
                return _truncate_summary(text)
    for entry in transcript_entries:
        text = _transcript_text(entry)
        if text:
            return _truncate_summary(text)
    for message in messages:
        if _is_meaningful_message(message):
            return _truncate_summary(_message_text(message))
    return ""


def _last_session_message(
    messages: list[dict[str, Any]],
    transcript_entries: list[dict[str, Any]],
) -> str:
    for entry in reversed(transcript_entries):
        text = _transcript_text(entry)
        if text:
            return _truncate_summary(text)
    for message in reversed(messages):
        if _is_meaningful_message(message):
            return _truncate_summary(_message_text(message))
    return ""


def _metadata_from_session_file(session_path: Path) -> SessionMetadata | None:
    """Extract lightweight metadata from a full session JSON file."""
    try:
        if session_path.stat().st_size == 0:
            return None
        data = json.loads(session_path.read_text(encoding="utf-8"))
        metadata = data.get("metadata")
        session_id = str(data.get("session_id") or session_path.stem)
        created_at = float(data.get("created_at") or session_path.stat().st_mtime)
        updated_at = float(data.get("updated_at") or session_path.stat().st_mtime)
        messages = data.get("messages", []) if isinstance(data.get("messages", []), list) else []
        transcript_entries = (
            data.get("transcript_entries", [])
            if isinstance(data.get("transcript_entries", []), list)
            else []
        )
        if not _session_has_meaningful_content(messages, transcript_entries):
            return None
        if isinstance(metadata, dict):
            repaired = SessionMetadata(**metadata)
            repaired.first_message = _first_session_message(messages, transcript_entries)
            repaired.last_message = _last_session_message(messages, transcript_entries)
            repaired.message_count = _meaningful_message_count(messages)
            repaired.workspace = str(data.get("workspace", repaired.workspace))
            repaired.updated_at = updated_at
            repaired.created_at = created_at
            return repaired
        return SessionMetadata(
            session_id=session_id,
            created_at=created_at,
            updated_at=updated_at,
            first_message=_first_session_message(messages, transcript_entries),
            last_message=_last_session_message(messages, transcript_entries),
            message_count=_meaningful_message_count(messages),
            workspace=str(data.get("workspace", "")),
        )
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError):
        return None


def _load_session_index() -> dict[str, SessionMetadata]:
    """Load and repair the lightweight metadata index for all valid sessions."""
    index: dict[str, SessionMetadata] = {}
    try:
        for session_path in SESSIONS_DIR.glob("*.json"):
            metadata = _metadata_from_session_file(session_path)
            if metadata is None:
                continue
            index[metadata.session_id] = metadata
    except OSError:
        pass

    _save_session_index(index)
    return index


def _save_session_index(index: dict[str, SessionMetadata]) -> bool:
    """Save the session index."""
    try:
        CORTEXTERM_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        serializable = {
            sid: {
                "session_id": meta.session_id,
                "created_at": meta.created_at,
                "updated_at": meta.updated_at,
                "first_message": meta.first_message,
                "last_message": meta.last_message,
                "message_count": meta.message_count,
                "workspace": meta.workspace,
            }
            for sid, meta in index.items()
        }
        _atomic_write_text(
            _session_index_file(),
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n",
        )
        return True
    except OSError:
        return False


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a file via temp file + atomic replace to avoid 0-byte sessions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def save_session(session: SessionData) -> bool:
    """Persist a complete session to disk."""
    try:
        if not session.has_meaningful_content():
            return False
        session.update_metadata()
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Save full session data
        session_path = _session_file(session.session_id)
        serializable = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "workspace": session.workspace,
            "messages": session.messages,
            "transcript_entries": session.transcript_entries,
            "history": session.history,
            "permissions_summary": session.permissions_summary,
            "skills": session.skills,
            "mcp_servers": session.mcp_servers,
            "metadata": {
                "session_id": session.metadata.session_id,
                "created_at": session.metadata.created_at,
                "updated_at": session.metadata.updated_at,
                "first_message": session.metadata.first_message,
                "last_message": session.metadata.last_message,
                "message_count": session.metadata.message_count,
                "workspace": session.metadata.workspace,
            },
        }
        _atomic_write_text(
            session_path,
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n",
        )

        # Update index
        index = _load_session_index()
        index[session.session_id] = session.metadata
        return _save_session_index(index)
    except OSError:
        return False


def load_session(session_id: str) -> SessionData | None:
    """Load a session from disk. Returns None if not found."""
    session_path = _session_file(session_id)
    if not session_path.exists():
        return None

    try:
        if session_path.stat().st_size == 0:
            return None
        raw = session_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        messages = data.get("messages", [])
        transcript_entries = data.get("transcript_entries", [])
        if not isinstance(messages, list):
            messages = []
        if not isinstance(transcript_entries, list):
            transcript_entries = []
        if not _session_has_meaningful_content(messages, transcript_entries):
            return None
        metadata = SessionMetadata(**data.get("metadata", {}))
        session = SessionData(
            session_id=data["session_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            workspace=data["workspace"],
            messages=messages,
            transcript_entries=transcript_entries,
            history=data.get("history", []),
            permissions_summary=data.get("permissions_summary", {}),
            skills=data.get("skills", []),
            mcp_servers=data.get("mcp_servers", []),
            metadata=metadata,
        )
        session.update_metadata()
        return session
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def list_sessions() -> list[SessionMetadata]:
    """List all available sessions, newest first."""
    index = _load_session_index()
    sessions = list(index.values())
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """Delete a session from disk. Returns True if deleted."""
    session_path = _session_file(session_id)
    if not session_path.exists():
        return False

    try:
        session_path.unlink()
        index = _load_session_index()
        index.pop(session_id, None)
        _save_session_index(index)
        return True
    except OSError:
        return False


def cleanup_old_sessions(max_sessions: int = 50) -> int:
    """Remove oldest sessions beyond max_sessions limit. Returns count deleted."""
    sessions = list_sessions()
    if len(sessions) <= max_sessions:
        return 0

    to_delete = sessions[max_sessions:]
    deleted = 0
    for meta in to_delete:
        if delete_session(meta.session_id):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Session creation helpers
# ---------------------------------------------------------------------------

def create_new_session(workspace: str) -> SessionData:
    """Create a new empty session."""
    now = time.time()
    session_id = uuid.uuid4().hex[:12]
    return SessionData(
        session_id=session_id,
        created_at=now,
        updated_at=now,
        workspace=workspace,
    )


def get_latest_session(workspace: str | None = None) -> SessionData | None:
    """Get the most recent session, optionally filtered by workspace."""
    sessions = list_sessions()
    for meta in sessions:
        if workspace is None or meta.workspace == workspace:
            return load_session(meta.session_id)
    return None


# ---------------------------------------------------------------------------
# Autosave manager
# ---------------------------------------------------------------------------

class AutosaveManager:
    """Manages automatic session saving with rate limiting."""

    def __init__(self, session: SessionData, interval: int = AUTOSAVE_INTERVAL_SECONDS):
        self.session = session
        self.interval = interval
        self._last_save_time = time.time()  # Initialize to current time
        self._dirty = False

    def mark_dirty(self) -> None:
        """Mark session as needing save."""
        self._dirty = True

    def should_save(self) -> bool:
        """Check if autosave should trigger."""
        if not self._dirty:
            return False
        elapsed = time.time() - self._last_save_time
        return elapsed >= self.interval

    def save_if_needed(self) -> bool:
        """Save if dirty and interval elapsed. Returns True if saved."""
        if self.should_save():
            if save_session(self.session):
                self._last_save_time = time.time()
                self._dirty = False
                return True
            return False
        return False

    def force_save(self) -> bool:
        """Force immediate save regardless of interval."""
        if save_session(self.session):
            self._last_save_time = time.time()
            self._dirty = False
            return True
        return False


# ---------------------------------------------------------------------------
# Session formatting for display
# ---------------------------------------------------------------------------

def format_session_list(sessions: list[SessionMetadata]) -> str:
    """Format sessions as a human-readable list."""
    if not sessions:
        return "No saved sessions found."

    lines = ["Saved sessions:", ""]
    for i, meta in enumerate(sessions, 1):
        created = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(meta.created_at),
        )
        workspace = meta.workspace or "unknown"
        first_msg = meta.first_message or "(empty)"
        count = meta.message_count

        lines.append(
            f"  {i}. [{meta.session_id[:8]}] {created} - {workspace}"
        )
        lines.append(f"     Messages: {count} | First: {first_msg}")
        lines.append("")

    lines.append(f"Total: {len(sessions)} session(s)")
    return "\n".join(lines)


def format_session_resume(session: SessionData) -> str:
    """Format session info for resume confirmation."""
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(session.created_at),
    )
    updated = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(session.updated_at),
    )
    return (
        f"Resuming session {session.session_id[:8]}\n"
        f"  Created: {created}\n"
        f"  Updated: {updated}\n"
        f"  Messages: {len(session.messages)}\n"
        f"  Workspace: {session.workspace}"
    )
