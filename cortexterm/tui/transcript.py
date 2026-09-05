from __future__ import annotations

from .chrome import (
    _cached_terminal_size,
    RESET, DIM, BOLD,
    ICON_ARROW, ICON_DIVIDER, ICON_DOT,
    wrap_panel_body_line,
)
from .markdown import render_markdownish
from .theme import theme
from .types import TranscriptEntry

# Pre-build the separator string once (immutable)
_SEPARATOR = f"  {DIM}{ICON_DOT} {ICON_DIVIDER * 3} {ICON_DOT}{RESET}"
_SEPARATOR_LINES = ["", _SEPARATOR, ""]
_SEPARATOR_LINE_COUNT = 3

# Tool output preview limits (match Rust TOOL_PREVIEW_LINES / TOOL_PREVIEW_CHARS)
_TOOL_PREVIEW_LINES = 6
_TOOL_PREVIEW_CHARS = 180


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent all lines in a block of text."""
    return "\n".join(prefix + line for line in text.split("\n"))


def preview_tool_body(tool_name: str, body: str) -> str:
    """Truncate tool output based on tool name and content size."""
    max_chars = 1000 if tool_name == "read_file" else 1800
    max_lines = 20 if tool_name == "read_file" else 36

    lines = body.split("\n")
    limited_lines = lines[:max_lines] if len(lines) > max_lines else lines
    limited = "\n".join(limited_lines)

    if len(limited) > max_chars:
        limited = limited[:max_chars] + "..."

    if limited != body:
        return f"{limited}\n{DIM}... output truncated in transcript{RESET}"

    return limited


def _tool_card(header: str, body: str) -> str:
    t = theme()
    body_lines = body.splitlines() if body else [""]
    lines = [f"{t.subtle}╭─{t.reset} {header}"]
    lines.extend(f"{t.subtle}│{t.reset} {line}" for line in body_lines)
    lines.append(f"{t.subtle}╰─{t.reset}")
    return "\n".join(lines)


def _render_transcript_entry(entry: TranscriptEntry, active: bool = False) -> str:
    """Render a single TranscriptEntry with Morandi theme colors.

    Tool entries follow the Rust [展开]/[收起] toggle pattern:
    - expanded=False → show preview lines + [展开] label
    - expanded=True  → show full output + [收起] label
    The ``collapsed`` / ``collapsePhase`` fields drive the display:
      collapsed=True  → entry was auto-collapsed after completion
      collapsePhase   → animation step (kept for compat, treated as collapsed)
    """
    t = theme()

    if entry.kind == "user":
        label = f"{t.user}{t.bold}{ICON_ARROW} user{t.reset}"
        return f"{label}\n{_indent_block(entry.body)}"

    if entry.kind == "assistant":
        label = f"{t.assistant}{t.bold}{ICON_ARROW} assistant{t.reset}"
        return f"{label}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "progress":
        label = f"{t.progress}{t.bold}{ICON_ARROW} progress{t.reset}"
        return f"{label}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "tool":
        # Status indicator
        if entry.status == "running":
            status_label = f"{t.tool}{ICON_DOT} running{t.reset}"
        elif entry.status == "success":
            status_label = f"{t.assistant}ok{t.reset}"
        else:
            status_label = f"{t.tool_error}err{t.reset}"

        tool_name_display = f"{t.tool}{t.bold}{entry.toolName}{t.reset}"

        # Determine expand/collapse toggle text
        body_lines = entry.body.split("\n") if entry.body else []
        total_lines = len(body_lines)
        collapsible_by_lines = total_lines > _TOOL_PREVIEW_LINES
        collapsible_by_chars = any(
            len(ln) > _TOOL_PREVIEW_CHARS
            for ln in body_lines[:_TOOL_PREVIEW_LINES]
        )
        can_toggle = entry.status != "running" and bool(entry.body)

        is_collapsed = entry.collapsed or entry.collapsePhase is not None
        target_is_collapsed = is_collapsed or entry.transition == "closing"

        if can_toggle:
            verb = "collapse" if not target_is_collapsed else "expand"
            toggle_text = (
                f"  {t.expandable}{t.bold}[click to {verb}]{t.reset}"
            )
        else:
            toggle_text = ""

        if entry.transition == "opening":
            marker = "▾"
            active_text = f" {t.progress}{t.bold}opening...{t.reset}"
        elif entry.transition == "closing":
            marker = "▴"
            active_text = f" {t.progress}{t.bold}closing...{t.reset}"
        elif is_collapsed:
            marker = "▸"
            active_text = f" {t.expandable}{t.bold}active{t.reset}" if active else ""
        else:
            marker = "▾"
            active_text = f" {t.expandable}{t.bold}active{t.reset}" if active else ""
        label = (
            f"{t.tool}{t.bold}{marker} tool{t.reset} {tool_name_display}"
            f" {status_label}{active_text}{toggle_text}"
        )

        if entry.status == "running":
            body = entry.body
        elif is_collapsed:
            if entry.collapsePhase is not None and not entry.collapsed:
                summary = entry.collapsedSummary or "collapsing..."
            else:
                summary = entry.collapsedSummary or "output collapsed"
            body = f"{t.subtle}{t.italic}{summary}{t.reset}"
        else:
            # Show preview (matches Rust's collapsed_preview_len = TOOL_PREVIEW_LINES)
            if collapsible_by_lines:
                preview = "\n".join(body_lines[:_TOOL_PREVIEW_LINES])
                hidden = total_lines - _TOOL_PREVIEW_LINES
                body = (
                    preview_tool_body(entry.toolName or "", render_markdownish(preview))
                    + f"\n{t.subtle}  ... {hidden} more lines{t.reset}"
                )
            else:
                body = preview_tool_body(
                    entry.toolName or "", render_markdownish(entry.body)
                )

            if entry.revealLines is not None:
                rendered_lines = body.splitlines()
                reveal = max(1, min(entry.revealLines, len(rendered_lines)))
                hidden = len(rendered_lines) - reveal
                body = "\n".join(rendered_lines[:reveal])
                if hidden > 0:
                    body += f"\n{t.subtle}  ... revealing {hidden} more lines{t.reset}"

        return _tool_card(label, body)

    return ""


def get_transcript_window_size(window_size: int | None = None) -> int:
    if window_size is not None:
        return max(4, window_size)
    _, rows = _cached_terminal_size()
    return max(8, rows - 15)


# ---------------------------------------------------------------------------
# Per-entry rendering cache
# ---------------------------------------------------------------------------

_entry_cache: dict[int, tuple[tuple, list[str]]] = {}
_CACHE_MAX_SIZE = 500

# The transcript viewport must use the same physical, terminal-width-aware
# lines as its panel.  Counting only source newlines means a long CJK or ANSI
# styled line occupies one viewport row but several terminal rows, which makes
# the feed overflow and renders its scroll offset incorrect.
_screen_line_cache: dict[tuple[int, int, bool], tuple[tuple, list[str]]] = {}


def _entry_render_state(entry: TranscriptEntry, active: bool = False) -> tuple:
    return (
        entry.kind,
        entry.body,
        entry.status,
        entry.collapsed,
        entry.collapsePhase,
        entry.collapsedSummary,
        entry.revealLines,
        entry.transition,
        entry.toolName,
        active,
    )


def _get_entry_lines(entry: TranscriptEntry, active: bool = False) -> list[str]:
    state = _entry_render_state(entry, active)

    entry_id = hash((id(entry), active))
    cached = _entry_cache.get(entry_id)
    if cached is not None and cached[0] == state:
        return cached[1]

    lines = _render_transcript_entry(entry, active=active).split("\n")

    if len(_entry_cache) > _CACHE_MAX_SIZE:
        keys = list(_entry_cache.keys())
        for k in keys[: len(keys) // 2]:
            del _entry_cache[k]

    _entry_cache[entry_id] = (state, lines)
    return lines


def _get_entry_screen_lines(entry: TranscriptEntry, width: int, active: bool = False) -> list[str]:
    """Return the physical terminal rows an entry occupies inside a panel."""
    state = _entry_render_state(entry, active)
    cache_key = (id(entry), width, active)
    cached = _screen_line_cache.get(cache_key)
    if cached is not None and cached[0] == state:
        return cached[1]

    lines: list[str] = []
    for line in _get_entry_lines(entry, active=active):
        lines.extend(wrap_panel_body_line(line, width))

    if len(_screen_line_cache) > _CACHE_MAX_SIZE:
        keys = list(_screen_line_cache.keys())
        for key in keys[: len(keys) // 2]:
            del _screen_line_cache[key]

    _screen_line_cache[cache_key] = (state, lines)
    return lines


# ---------------------------------------------------------------------------
# Per-entry line count cache
# ---------------------------------------------------------------------------

_line_count_cache: dict[int, tuple[tuple, int]] = {}


def _get_entry_line_count(entry: TranscriptEntry) -> int:
    state = _entry_render_state(entry)
    entry_id = id(entry)

    cached_lc = _line_count_cache.get(entry_id)
    if cached_lc is not None and cached_lc[0] == state:
        return cached_lc[1]

    cached_full = _entry_cache.get(entry_id)
    if cached_full is not None and cached_full[0] == state:
        count = len(cached_full[1])
        _line_count_cache[entry_id] = (state, count)
        return count

    lines = _get_entry_lines(entry)
    count = len(lines)
    _line_count_cache[entry_id] = (state, count)
    return count


# ---------------------------------------------------------------------------
# Windowed transcript rendering — O(visible)
# ---------------------------------------------------------------------------

def _resolve_panel_width(width: int | None) -> int:
    if width is not None:
        return max(40, width)
    terminal_width, _ = _cached_terminal_size()
    return max(40, terminal_width)


def _compute_total_lines(
    entries: list[TranscriptEntry],
    width: int | None = None,
    active_entry_id: int | None = None,
) -> int:
    if not entries:
        return 0
    panel_width = _resolve_panel_width(width)
    total = 0
    for i, entry in enumerate(entries):
        if i > 0:
            total += _SEPARATOR_LINE_COUNT
        total += len(_get_entry_screen_lines(entry, panel_width, active=entry.id == active_entry_id))
    return total


def _render_visible_window(
    entries: list[TranscriptEntry],
    start_line: int,
    end_line: int,
    width: int | None = None,
    active_entry_id: int | None = None,
) -> list[str]:
    if not entries:
        return []

    panel_width = _resolve_panel_width(width)
    result: list[str] = []
    current_line = 0

    for i, entry in enumerate(entries):
        if i > 0:
            sep_start = current_line
            sep_end = current_line + _SEPARATOR_LINE_COUNT
            if sep_start < end_line and sep_end > start_line:
                vis_start = max(0, start_line - sep_start)
                vis_end = min(_SEPARATOR_LINE_COUNT, end_line - sep_start)
                result.extend(_SEPARATOR_LINES[vis_start:vis_end])
            current_line = sep_end
            if current_line >= end_line:
                break

        lines = _get_entry_screen_lines(entry, panel_width, active=entry.id == active_entry_id)
        entry_line_count = len(lines)
        entry_start = current_line
        entry_end = current_line + entry_line_count

        if entry_start < end_line and entry_end > start_line:
            vis_start = max(0, start_line - entry_start)
            vis_end = min(entry_line_count, end_line - entry_start)
            if vis_start > 0 and lines:
                result.append(lines[0])
            result.extend(lines[vis_start:vis_end])

        current_line = entry_end
        if current_line >= end_line:
            break

    return result


def _visible_entry_line_ranges(
    entries: list[TranscriptEntry],
    start_line: int,
    end_line: int,
    width: int | None = None,
    active_entry_id: int | None = None,
) -> list[tuple[int, int, int]]:
    """Return visible row ranges for transcript entries.

    Ranges are relative to the rendered transcript body, zero-based, and use
    an exclusive end row: ``(entry_id, start_row, end_row)``. Separator and
    scroll-hint rows are intentionally omitted so mouse hit testing only lands
    on actual messages/tools.
    """
    if not entries:
        return []

    panel_width = _resolve_panel_width(width)
    result: list[tuple[int, int, int]] = []
    current_line = 0
    rendered_row = 0

    for i, entry in enumerate(entries):
        if i > 0:
            sep_start = current_line
            sep_end = current_line + _SEPARATOR_LINE_COUNT
            if sep_start < end_line and sep_end > start_line:
                rendered_row += min(_SEPARATOR_LINE_COUNT, end_line - sep_start) - max(0, start_line - sep_start)
            current_line = sep_end
            if current_line >= end_line:
                break

        entry_line_count = len(_get_entry_screen_lines(entry, panel_width, active=entry.id == active_entry_id))
        entry_start = current_line
        entry_end = current_line + entry_line_count

        if entry_start < end_line and entry_end > start_line:
            vis_start = max(0, start_line - entry_start)
            vis_end = min(entry_line_count, end_line - entry_start)
            start_row = rendered_row
            rendered_row += vis_end - vis_start
            result.append((entry.id, start_row, rendered_row))

        current_line = entry_end
        if current_line >= end_line:
            break

    return result


def get_transcript_visible_entry_ranges(
    entries: list[TranscriptEntry],
    scroll_offset: int,
    window_size: int | None = None,
    width: int | None = None,
    active_entry_id: int | None = None,
) -> list[tuple[int, int, int]]:
    """Return visible transcript entry ranges for mouse hit testing."""
    if not entries:
        return []

    panel_width = _resolve_panel_width(width)
    total_lines = _compute_total_lines(entries, panel_width, active_entry_id=active_entry_id)
    ws = get_transcript_window_size(window_size)
    max_offset = max(0, total_lines - ws)
    offset = max(0, min(scroll_offset, max_offset))

    if max_offset == 0:
        end = total_lines
        start = max(0, end - ws)
        return _visible_entry_line_ranges(entries, start, end, panel_width, active_entry_id=active_entry_id)

    content_ws = max(1, ws - 1)
    end = total_lines - offset
    start = max(0, end - content_ws)
    return _visible_entry_line_ranges(entries, start, end, panel_width, active_entry_id=active_entry_id)


def get_transcript_max_scroll_offset(
    entries: list[TranscriptEntry],
    window_size: int | None = None,
    width: int | None = None,
    active_entry_id: int | None = None,
) -> int:
    if not entries:
        return 0
    total = _compute_total_lines(entries, width, active_entry_id=active_entry_id)
    ws = get_transcript_window_size(window_size)
    return max(0, total - ws)


def render_transcript(
    entries: list[TranscriptEntry],
    scroll_offset: int,
    window_size: int | None = None,
    width: int | None = None,
    active_entry_id: int | None = None,
) -> str:
    """Render a windowed view of the transcript. O(visible)."""
    t = theme()
    if not entries:
        return ""

    panel_width = _resolve_panel_width(width)
    total_lines = _compute_total_lines(entries, panel_width, active_entry_id=active_entry_id)
    ws = get_transcript_window_size(window_size)
    max_offset = max(0, total_lines - ws)
    offset = max(0, min(scroll_offset, max_offset))

    if max_offset == 0:
        end = total_lines
        start = max(0, end - ws)
        visible_lines = _render_visible_window(entries, start, end, panel_width, active_entry_id=active_entry_id)
        return "\n".join(visible_lines)

    # Always reserve one row for navigation when the transcript overflows. At
    # the latest position this is the only cue that earlier turns are available.
    content_ws = max(1, ws - 1)
    end = total_lines - offset
    start = max(0, end - content_ws)
    visible_lines = _render_visible_window(entries, start, end, panel_width, active_entry_id=active_entry_id)
    body = "\n".join(visible_lines)

    return (
        f"{body}\n"
        f"{t.subtle}  {ICON_DIVIDER * 2} scroll {offset}/{max_offset} "
        f"(PgUp/PgDn or scroll){ICON_DIVIDER * 2}{t.reset}"
    )


# ---------------------------------------------------------------------------
# Legacy full-render API (backward compat)
# ---------------------------------------------------------------------------

def _render_transcript_lines(entries: list[TranscriptEntry]) -> list[str]:
    """Render all entries into lines with separators. Kept for backward compat."""
    all_lines: list[str] = []
    for i, entry in enumerate(entries):
        if i > 0:
            all_lines.extend(_SEPARATOR_LINES)
        all_lines.extend(_get_entry_lines(entry))
    return all_lines


def format_transcript_text(entries: list[TranscriptEntry]) -> str:
    """Format transcript entries as plain text (no ANSI) for file saving."""
    parts = []
    for entry in entries:
        label = "user" if entry.kind == "user" else entry.kind
        if entry.kind == "tool":
            status_text = f" ({entry.status})" if entry.status else ""
            label = f"{entry.toolName or 'tool'}{status_text}"
        indented = "\n".join("  " + line for line in entry.body.splitlines())
        parts.append(f"{label}\n{indented}")
    return "\n\n---\n\n".join(parts)
