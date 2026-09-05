"""Tool-card state transitions used by background agent turns."""

from __future__ import annotations

import time
from typing import Any, Callable

from cortexterm.tui.state import AggregatedEditProgress, ScreenState
from cortexterm.tui.tool_cards import (
    collapse_tool_entry,
    push_transcript_entry,
    schedule_tool_auto_collapse,
    update_tool_entry,
)
from cortexterm.tui.tool_display import (
    extract_path_from_tool_input,
    is_file_edit_tool,
    summarize_collapsed_tool_body,
    summarize_tool_input,
)


def handle_tool_start(
    state: ScreenState,
    pending_tool_entries: dict[str, list[int]],
    aggregated_edit_by_key: dict[str, AggregatedEditProgress],
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress],
    tool_name: str,
    tool_input: Any,
) -> None:
    state.status = f"Running {tool_name}..."
    state.active_tool = tool_name
    state.tool_start_time = time.monotonic()

    target_path = extract_path_from_tool_input(tool_input)
    can_aggregate = is_file_edit_tool(tool_name) and target_path is not None

    if can_aggregate:
        entry_id = _start_or_extend_aggregated_edit(
            state,
            aggregated_edit_by_key,
            aggregated_edit_by_entry_id,
            tool_name,
            target_path,
            tool_input,
        )
    else:
        entry_id = push_transcript_entry(
            state,
            kind="tool",
            toolName=tool_name,
            status="running",
            body=summarize_tool_input(tool_name, tool_input),
        )

    pending_tool_entries[tool_name].append(entry_id)


def handle_tool_result(
    state: ScreenState,
    pending_tool_entries: dict[str, list[int]],
    aggregated_edit_by_key: dict[str, AggregatedEditProgress],
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress],
    tool_name: str,
    output: str,
    is_error: bool,
    rerender: Callable[[], None],
) -> None:
    pending = pending_tool_entries.get(tool_name, [])
    entry_id = pending.pop(0) if pending else None
    if entry_id is not None:
        aggregated = aggregated_edit_by_entry_id.get(entry_id)
        if aggregated and aggregated.tool_name == tool_name:
            _finish_aggregated_tool_result(
                state,
                aggregated_edit_by_key,
                aggregated_edit_by_entry_id,
                aggregated,
                entry_id,
                output,
                is_error,
            )
        else:
            _finish_single_tool_result(
                state,
                entry_id,
                tool_name,
                output,
                is_error,
                rerender,
            )

    state.active_tool = None
    remaining = sum(len(v) for v in pending_tool_entries.values())
    state.status = f"{remaining} tool(s) still running..." if remaining > 0 else None


def _start_or_extend_aggregated_edit(
    state: ScreenState,
    aggregated_edit_by_key: dict[str, AggregatedEditProgress],
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress],
    tool_name: str,
    target_path: str,
    tool_input: Any,
) -> int:
    key = f"{tool_name}:{target_path}"
    existing = aggregated_edit_by_key.get(key)
    if existing:
        existing.total += 1
        existing.last_output = summarize_tool_input(tool_name, tool_input)
        update_tool_entry(
            state,
            existing.entry_id,
            "error" if existing.errors > 0 else "running",
            (
                f"Aggregated {tool_name} for {target_path}\n"
                f"Completed: {existing.completed}/{existing.total}"
            ),
        )
        return existing.entry_id

    entry_id = push_transcript_entry(
        state,
        kind="tool",
        toolName=tool_name,
        status="running",
        body=summarize_tool_input(tool_name, tool_input),
    )
    progress = AggregatedEditProgress(
        entry_id=entry_id,
        tool_name=tool_name,
        path=target_path,
        total=1,
        completed=0,
        errors=0,
        last_output=summarize_tool_input(tool_name, tool_input),
    )
    aggregated_edit_by_key[key] = progress
    aggregated_edit_by_entry_id[entry_id] = progress
    return entry_id


def _finish_aggregated_tool_result(
    state: ScreenState,
    aggregated_edit_by_key: dict[str, AggregatedEditProgress],
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress],
    aggregated: AggregatedEditProgress,
    entry_id: int,
    output: str,
    is_error: bool,
) -> None:
    aggregated.completed += 1
    if is_error:
        aggregated.errors += 1
    aggregated.last_output = output
    done = aggregated.completed >= aggregated.total
    if done:
        state.recent_tools.append(
            {
                "name": f"{aggregated.tool_name} x{aggregated.total}",
                "status": "error" if aggregated.errors > 0 else "success",
            }
        )
    body = (
        "\n".join(
            [
                f"Aggregated {aggregated.tool_name} for {aggregated.path}",
                f"Operations: {aggregated.total}, errors: {aggregated.errors}",
                f"Last result: {aggregated.last_output}",
            ]
        )
        if done
        else (
            f"Aggregated {aggregated.tool_name} for {aggregated.path}\n"
            f"Completed: {aggregated.completed}/{aggregated.total}"
        )
    )
    update_tool_entry(
        state,
        entry_id,
        "error" if aggregated.errors > 0 else ("success" if done else "running"),
        body,
    )
    if done:
        collapse_tool_entry(state, entry_id, summarize_collapsed_tool_body(body))
        aggregated_edit_by_entry_id.pop(entry_id, None)
        aggregated_edit_by_key.pop(f"{aggregated.tool_name}:{aggregated.path}", None)


def _finish_single_tool_result(
    state: ScreenState,
    entry_id: int,
    tool_name: str,
    output: str,
    is_error: bool,
    rerender: Callable[[], None],
) -> None:
    state.recent_tools.append(
        {
            "name": tool_name,
            "status": "error" if is_error else "success",
        }
    )

    display_output = _display_output_with_error_hint(output, is_error)
    update_tool_entry(
        state,
        entry_id,
        "error" if is_error else "success",
        display_output,
    )
    schedule_tool_auto_collapse(state, entry_id, display_output, rerender)


def _display_output_with_error_hint(output: str, is_error: bool) -> str:
    if not is_error:
        return output

    suggestions: list[str] = []
    output_lower = output.lower()
    if "not found" in output_lower or "no such file" in output_lower:
        suggestions.append("Hint: file not found - use /ls to list files")
    elif "permission" in output_lower or "denied" in output_lower:
        suggestions.append("Hint: permission denied - check file access rights")
    elif "syntax" in output_lower or "error" in output_lower:
        suggestions.append("Hint: error occurred - review output and fix issues")

    if suggestions:
        return f"ERROR: {output}\n\n" + "\n".join(suggestions)
    return f"ERROR: {output}"
