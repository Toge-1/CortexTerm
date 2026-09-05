from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from cortexterm.memory import MemoryManager, MemoryScope
from cortexterm.tooling import ToolDefinition, ToolResult


ALLOWED_SCOPES = {"user", "project", "local"}
ALLOWED_CATEGORIES = {
    "preference",
    "convention",
    "architecture",
    "command",
    "environment",
    "decision",
    "bugfix",
    "general",
}
MAX_CONTENT_CHARS = 1200
MAX_TAGS = 10
MAX_TAG_CHARS = 40
DUPLICATE_THRESHOLD = 0.88

SENSITIVE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def _contains_sensitive_content(content: str) -> bool:
    return any(pattern.search(content) for pattern in SENSITIVE_PATTERNS)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = str(raw_tag).strip().replace(" ", "-")
        if not tag:
            continue
        tag = tag[:MAX_TAG_CHARS]
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
        if len(normalized) >= MAX_TAGS:
            break
    return normalized


def _validate(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        raise ValueError("input must be an object")

    scope = str(input_data.get("scope", "project")).strip().lower()
    if scope not in ALLOWED_SCOPES:
        raise ValueError("scope must be user, project, or local")

    category = str(input_data.get("category", "general")).strip().lower()
    if category not in ALLOWED_CATEGORIES:
        category = "general"

    content = str(input_data.get("content", "")).strip()
    if not content:
        raise ValueError("content is required")
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS].rstrip()

    tags = _normalize_tags(input_data.get("tags", []))

    return {
        "scope": scope,
        "category": category,
        "content": content,
        "tags": tags,
    }


def _find_duplicate(manager: MemoryManager, scope: MemoryScope, content: str):
    for entry in manager.memories[scope].entries:
        if _similarity(content, entry.content) >= DUPLICATE_THRESHOLD:
            return entry
    return None


def _memory_file_targets(manager: MemoryManager, scope: MemoryScope) -> list[Path]:
    scope_path = manager._get_scope_path(scope)
    return [scope_path / "memory.json", scope_path / "MEMORY.md"]


def _ensure_memory_write_allowed(manager: MemoryManager, scope: MemoryScope, input_data: dict, context) -> None:
    if context.permissions is None:
        return

    diff_preview = (
        "remember tool will update long-term memory\n"
        f"scope: {scope.value}\n"
        f"category: {input_data['category']}\n"
        f"content: {input_data['content']}\n"
    )
    for target in _memory_file_targets(manager, scope):
        context.permissions.ensure_edit(str(target), diff_preview)


def _run(input_data: dict, context) -> ToolResult:
    content = input_data["content"]
    if _contains_sensitive_content(content):
        return ToolResult(ok=False, output="Memory rejected: sensitive content detected.")

    scope = MemoryScope(input_data["scope"])
    manager = MemoryManager(project_root=context.cwd)
    duplicate = _find_duplicate(manager, scope, content)
    if duplicate is not None:
        return ToolResult(
            ok=True,
            output=f"Memory skipped: similar entry already exists ({duplicate.id}).",
        )

    _ensure_memory_write_allowed(manager, scope, input_data, context)

    entry = manager.add_entry(
        scope=scope,
        category=input_data["category"],
        content=content,
        tags=input_data["tags"],
    )
    return ToolResult(
        ok=True,
        output=f"Memory saved: {entry.id} ({entry.scope.value}/{entry.category})",
    )


remember_tool = ToolDefinition(
    name="remember",
    description=(
        "Save durable long-term memory for future sessions. Use this automatically for stable "
        "user preferences, project conventions, architecture decisions, setup or test commands, "
        "recurring bug fixes, and local environment constraints. Do not store secrets, temporary "
        "task progress, one-off outputs, guesses, large logs, or sensitive content."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["user", "project", "local"],
                "description": "user for cross-project preferences, project for repo conventions, local for machine-specific notes",
            },
            "category": {
                "type": "string",
                "enum": [
                    "preference",
                    "convention",
                    "architecture",
                    "command",
                    "environment",
                    "decision",
                    "bugfix",
                    "general",
                ],
            },
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["scope", "category", "content"],
    },
    validator=_validate,
    run=_run,
)
