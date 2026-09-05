from __future__ import annotations

from .chrome import (
    RESET, DIM, BOLD, ITALIC, HIGHLIGHT_BG,
    BRIGHT_GREEN, SUBTLE,
    ICON_PROMPT, ICON_DOT,
    _cached_terminal_size,
    char_display_width,
    string_display_width,
)
from .theme import theme


def _wrap_input_lines(text: str, cursor_offset: int, width: int) -> list[tuple[str, int, int]]:
    """Wrap editable input into terminal-display-width rows."""
    if width <= 0:
        return [("", 0, 0)]

    lines: list[tuple[str, int, int]] = []
    current = ""
    current_start = 0
    current_width = 0

    for index, char in enumerate(text):
        if char == "\n":
            lines.append((current, current_start, index))
            current = ""
            current_start = index + 1
            current_width = 0
            continue

        char_width = char_display_width(char)
        if current and current_width + char_width > width:
            lines.append((current, current_start, index))
            current = ""
            current_start = index
            current_width = 0

        current += char
        current_width += char_width

    lines.append((current, current_start, len(text)))
    return lines or [("", 0, 0)]


def _find_cursor_line_index(
    lines: list[tuple[str, int, int]],
    cursor_offset: int,
) -> int:
    for line_index, (_, start, end) in enumerate(lines):
        if start <= cursor_offset <= end:
            return line_index
    return max(0, len(lines) - 1)


def _window_input_lines(
    lines: list[tuple[str, int, int]],
    cursor_offset: int,
    max_lines: int | None,
) -> tuple[list[tuple[str, int, int]], int, int]:
    if not max_lines or max_lines <= 0 or len(lines) <= max_lines:
        return lines, 0, len(lines)

    cursor_line = _find_cursor_line_index(lines, cursor_offset)
    window_start = max(0, min(cursor_line - max_lines + 1, len(lines) - max_lines))
    return lines[window_start:window_start + max_lines], window_start, len(lines)


def _render_editable_line(
    text: str,
    start: int,
    end: int,
    cursor_offset: int,
    prefix: str,
    continuation_prefix: str,
) -> str:
    offset = max(0, min(cursor_offset, end))
    active = start <= offset <= end
    line_prefix = prefix if start == 0 else continuation_prefix

    if active:
        relative = offset - start
        before = text[:relative]
        current = text[relative] if relative < len(text) else " "
        after = text[relative + 1 :] if relative < len(text) else ""
        return f"{line_prefix}{before}{HIGHLIGHT_BG}{BRIGHT_GREEN}{current}{RESET}{after}"

    return f"{line_prefix}{text}"


def render_input_prompt(
    current_input: str,
    cursor_offset: int,
    compact: bool = False,
    max_lines: int | None = None,
) -> str:
    """Render the input prompt line.

    The editable area wraps instead of truncating pasted or multi-line input.

    When compact=True (small terminal), the hint bar is hidden to save lines.
    """
    t = theme()
    cols, _ = _cached_terminal_size()
    prefix = f" {t.user}{BOLD}user >{RESET} "
    continuation_prefix = " " * string_display_width(" user > ")
    edit_width = max(16, cols - string_display_width(" user > ") - 2)

    placeholder = (
        "" if current_input
        else f"{ITALIC} Type a message or /help for commands{RESET}"
    )

    if current_input:
        wrapped_lines = _wrap_input_lines(current_input, cursor_offset, edit_width)
        visible_lines, window_start, total_lines = _window_input_lines(
            wrapped_lines,
            cursor_offset,
            max_lines,
        )
        input_lines = [
            _render_editable_line(
                line_text,
                start,
                end,
                cursor_offset,
                prefix,
                continuation_prefix,
            )
            for line_text, start, end in visible_lines
        ]
        if window_start:
            input_lines.insert(
                0,
                f"{continuation_prefix}{SUBTLE}... {window_start} earlier line"
                f"{'s' if window_start != 1 else ''}{RESET}",
            )
        hidden_after = total_lines - window_start - len(visible_lines)
        if hidden_after:
            input_lines.append(
                f"{continuation_prefix}{SUBTLE}... {hidden_after} more line"
                f"{'s' if hidden_after != 1 else ''}{RESET}"
            )
    else:
        input_lines = [
            f"{prefix}{HIGHLIGHT_BG}{BRIGHT_GREEN} {RESET}{DIM}{placeholder}{RESET}"
        ]

    if compact:
        return "\n".join(input_lines)

    # Hint bar
    key_enter = f"{t.subtle}[{RESET}{DIM}Enter{RESET}{t.subtle}]{RESET} {t.subtle}send{RESET}"
    key_help = f"{t.subtle}[{RESET}{DIM}/help{RESET}{t.subtle}]{RESET} {t.subtle}cmds{RESET}"
    key_esc = f"{t.subtle}[{RESET}{DIM}Esc{RESET}{t.subtle}]{RESET} {t.subtle}clear{RESET}"
    key_exit = f"{t.subtle}[{RESET}{DIM}^C{RESET}{t.subtle}]{RESET} {t.subtle}exit{RESET}"

    line1 = f"  {key_enter}  {key_help}  {key_esc}  {key_exit}"
    line2 = ""

    return "\n".join([line1, line2, *input_lines])


def get_input_cursor_cell(
    current_input: str,
    cursor_offset: int,
    max_lines: int | None = None,
) -> tuple[int, int]:
    """Return the rendered input cursor cell as ``(row_offset, one_based_col)``.

    The TUI draws its own highlighted cursor, but Windows IME composition
    windows follow the terminal's real cursor position.  Rendering code uses
    this helper after drawing the full screen to move the hidden real cursor
    back onto the editable input area, so Chinese/Japanese/Korean preedit text
    appears beside ``user >`` instead of at the footer.
    """
    cols, _ = _cached_terminal_size()
    cursor_offset = max(0, min(cursor_offset, len(current_input)))
    prefix_width = string_display_width(" user > ")
    edit_width = max(16, cols - prefix_width - 2)

    if not current_input:
        return 0, min(cols, prefix_width + 1)

    wrapped_lines = _wrap_input_lines(current_input, cursor_offset, edit_width)
    visible_lines, window_start, _ = _window_input_lines(
        wrapped_lines,
        cursor_offset,
        max_lines,
    )
    marker_before = 1 if window_start else 0

    for visible_row, (line_text, start, end) in enumerate(visible_lines):
        if start <= cursor_offset <= end:
            relative = max(0, cursor_offset - start)
            before_cursor = line_text[:relative]
            col = prefix_width + string_display_width(before_cursor) + 1
            return marker_before + visible_row, min(cols, max(1, col))

    return 0, min(cols, prefix_width + 1)
