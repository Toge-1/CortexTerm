"""Display-only helpers for tool cards in the TTY transcript."""

from __future__ import annotations

from typing import Any


def truncate_for_display(text: str, max_len: int = 180) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Return a compact, user-facing summary of a tool call input."""
    if isinstance(tool_input, str):
        return truncate_for_display(" ".join(tool_input.split()).strip())

    if isinstance(tool_input, dict):
        path = str(tool_input.get("path", "")).strip()
        path_part = f" path={path}" if path else ""

        if tool_name == "patch_file":
            replacements = tool_input.get("replacements")
            count = len(replacements) if isinstance(replacements, list) else 0
            return f"patch_file{path_part} replacements={count}"
        if tool_name == "edit_file":
            return f"edit_file{path_part}"
        if tool_name == "read_file":
            extras: list[str] = []
            if tool_input.get("offset") is not None:
                extras.append(f"offset={tool_input['offset']}")
            if tool_input.get("limit") is not None:
                extras.append(f"limit={tool_input['limit']}")
            return f"read_file{path_part}{' ' + ' '.join(extras) if extras else ''}"
        if tool_name == "run_command":
            cmd = str(tool_input.get("command", "")).strip()
            return f"run_command{' ' + truncate_for_display(cmd, 120) if cmd else ''}"
        if path:
            return f"{tool_name}{path_part}"

    try:
        return truncate_for_display(str(tool_input))
    except Exception:
        return truncate_for_display(repr(tool_input))


def is_file_edit_tool(tool_name: str) -> bool:
    return tool_name in ("edit_file", "patch_file", "modify_file", "write_file")


def extract_path_from_tool_input(tool_input: Any) -> str | None:
    if isinstance(tool_input, dict):
        value = tool_input.get("path") or tool_input.get("file_path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def summarize_collapsed_tool_body(output: str) -> str:
    line = next(
        (line.strip() for line in output.split("\n") if line.strip()),
        "output collapsed",
    )
    return line[:140] + "..." if len(line) > 140 else line


def summarize_tool_output(tool_name: str, output: str) -> str:
    """Summarize tool output for collapsed display."""
    return summarize_collapsed_tool_body(output)

