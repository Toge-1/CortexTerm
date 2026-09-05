from __future__ import annotations

from pathlib import Path

import pytest

from cortexterm.memory import MemoryManager
from cortexterm.tooling import ToolContext
from cortexterm.tools import create_default_tool_registry
from cortexterm.tools.remember import remember_tool


class RecordingPermissions:
    def __init__(self) -> None:
        self.edit_calls: list[tuple[str, str]] = []

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        self.edit_calls.append((Path(target_path).name, diff_preview))


class DenyingPermissions:
    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        raise RuntimeError(f"denied {target_path}")


def test_remember_tool_saves_project_memory(tmp_path: Path) -> None:
    context = ToolContext(cwd=str(tmp_path), permissions=None)
    payload = remember_tool.validator(
        {
            "scope": "project",
            "category": "convention",
            "content": "Use pytest for CortexTerm regression tests.",
            "tags": ["tests", "cortexterm", "tests"],
        }
    )

    result = remember_tool.run(payload, context)

    assert result.ok is True
    assert "Memory saved:" in result.output

    manager = MemoryManager(project_root=tmp_path)
    entries = manager.memories[payload["scope"]].entries
    assert [entry.content for entry in entries] == [
        "Use pytest for CortexTerm regression tests."
    ]
    assert entries[0].tags == ["tests", "cortexterm"]


def test_remember_tool_skips_duplicate_memory(tmp_path: Path) -> None:
    context = ToolContext(cwd=str(tmp_path), permissions=None)
    payload = remember_tool.validator(
        {
            "scope": "project",
            "category": "command",
            "content": "Run python -m pytest -q before handing off changes.",
        }
    )

    first = remember_tool.run(payload, context)
    second = remember_tool.run(payload, context)

    assert first.ok is True
    assert second.ok is True
    assert "Memory skipped:" in second.output

    manager = MemoryManager(project_root=tmp_path)
    assert len(manager.memories[payload["scope"]].entries) == 1


def test_remember_tool_rejects_sensitive_content(tmp_path: Path) -> None:
    context = ToolContext(cwd=str(tmp_path), permissions=None)
    payload = remember_tool.validator(
        {
            "scope": "local",
            "category": "environment",
            "content": "ANTHROPIC_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        }
    )

    result = remember_tool.run(payload, context)

    assert result.ok is False
    assert result.output == "Memory rejected: sensitive content detected."


def test_remember_tool_requests_edit_permission_for_memory_files(tmp_path: Path) -> None:
    permissions = RecordingPermissions()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)
    payload = remember_tool.validator(
        {
            "scope": "project",
            "category": "decision",
            "content": "Project memory writes should pass through edit permission checks.",
        }
    )

    result = remember_tool.run(payload, context)

    assert result.ok is True
    assert [name for name, _preview in permissions.edit_calls] == [
        "memory.json",
        "MEMORY.md",
    ]
    assert "Project memory writes should pass through edit permission checks." in permissions.edit_calls[0][1]


def test_remember_tool_does_not_write_when_edit_permission_denied(tmp_path: Path) -> None:
    context = ToolContext(cwd=str(tmp_path), permissions=DenyingPermissions())
    payload = remember_tool.validator(
        {
            "scope": "project",
            "category": "decision",
            "content": "This memory should not be written.",
        }
    )

    with pytest.raises(RuntimeError):
        remember_tool.run(payload, context)

    assert not (tmp_path / ".cortexterm-memory" / "memory.json").exists()


def test_remember_tool_is_registered() -> None:
    registry = create_default_tool_registry(".")
    try:
        assert registry.find("remember") is remember_tool
    finally:
        registry.dispose()
