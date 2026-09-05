"""Home/start screen rendering for the CortexTerm TTY app."""

from __future__ import annotations

from cortexterm.tui.chrome import (
    _cached_terminal_size,
    char_display_width,
    string_display_width,
    truncate_plain,
)
from cortexterm.tui.state import ScreenState, TtyAppArgs
from cortexterm.tui.theme import theme

_get_terminal_size = _cached_terminal_size


def _center_ansi_line(line: str, width: int) -> str:
    pad = max(0, (width - string_display_width(line)) // 2)
    return (" " * pad) + line


def _fit_ansi_line(line: str, width: int) -> str:
    display = string_display_width(line)
    if display >= width:
        return truncate_plain(line, width)
    return line + (" " * (width - display))


def _home_input_card_width(state: ScreenState, cols: int) -> int:
    """Size the welcome prompt card by need, not by the whole terminal width."""
    max_width = max(42, min(92, cols - 12))
    base_width = max(42, min(64, cols - 24))
    if not state.input:
        return min(max_width, base_width)

    longest_input_line = max(
        (string_display_width(line) for line in state.input.splitlines()),
        default=0,
    )
    desired = longest_input_line + 6
    return min(max_width, max(base_width, desired))


def _wrap_home_input_lines(
    text: str,
    cursor_offset: int,
    width: int,
    max_lines: int = 5,
) -> tuple[list[tuple[str, int, int]], int, int]:
    """Wrap home-screen input by terminal display width and keep cursor visible."""
    if width <= 0:
        return [("", 0, 0)], 0, 1

    offset = max(0, min(cursor_offset, len(text)))
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

    cursor_line = 0
    for line_index, (_, start, end) in enumerate(lines):
        if start <= offset <= end:
            cursor_line = line_index
            break

    if len(lines) <= max_lines:
        return lines, 0, len(lines)

    window_start = max(0, min(cursor_line - max_lines + 1, len(lines) - max_lines))
    window_end = window_start + max_lines
    return lines[window_start:window_end], window_start, len(lines)


def _render_home_input_text_line(
    text: str,
    start: int,
    end: int,
    cursor_offset: int,
    width: int,
    color: str,
) -> str:
    t = theme()
    offset = max(0, min(cursor_offset, end))
    if start <= offset <= end:
        relative = offset - start
        before = text[:relative]
        current = text[relative] if relative < len(text) else " "
        after = text[relative + 1 :] if relative < len(text) else ""
        rendered = (
            f"{color}{before}"
            f"{t.reverse}{current}{t.reset}{t.highlight_bg}"
            f"{color}{after}{t.reset}{t.highlight_bg}"
        )
    else:
        rendered = f"{color}{text}{t.reset}{t.highlight_bg}"

    return _fit_ansi_line(f"  {rendered}", width)


def _render_home_input_card(args: TtyAppArgs | None, state: ScreenState) -> str:
    t = theme()
    cols, _ = _get_terminal_size()
    card_width = _home_input_card_width(state, cols)
    indent = " " * max(0, (cols - card_width - 2) // 2)

    placeholder = 'Ask anything... "What should I change in this repo?"'
    input_width = max(20, card_width - 4)
    prompt_color = t.assistant if state.input else t.subtle
    prompt_lines: list[str] = []

    if state.input:
        wrapped_lines, window_start, total_lines = _wrap_home_input_lines(
            state.input,
            state.cursor_offset,
            input_width,
            max_lines=5,
        )
        if window_start:
            prompt_lines.append(
                _fit_ansi_line(
                    f"  {t.subtle}… {window_start} earlier line"
                    f"{'s' if window_start != 1 else ''}{t.reset}{t.highlight_bg}",
                    card_width,
                )
            )
        for line_text, start, end in wrapped_lines:
            prompt_lines.append(
                _render_home_input_text_line(
                    line_text,
                    start,
                    end,
                    state.cursor_offset,
                    card_width,
                    prompt_color,
                )
            )
        hidden_after = total_lines - window_start - len(wrapped_lines)
        if hidden_after:
            prompt_lines.append(
                _fit_ansi_line(
                    f"  {t.subtle}… {hidden_after} more line"
                    f"{'s' if hidden_after != 1 else ''}{t.reset}{t.highlight_bg}",
                    card_width,
                )
            )
    else:
        prompt_lines.append(
            _fit_ansi_line(
                f"  {prompt_color}{placeholder}{t.reset}{t.highlight_bg}",
                card_width,
            )
        )

    model = args.runtime.get("model", "model") if args and args.runtime else "model"
    model = model.replace("deepseek-", "deepseek ")
    meta = (
        f"  {t.expandable}MiniClaudeCode{t.reset}{t.highlight_bg}"
        f" {t.subtle}·{t.reset}{t.highlight_bg} "
        f"{t.assistant}{model}{t.reset}{t.highlight_bg}"
        f" {t.subtle}·{t.reset}{t.highlight_bg} "
        f"{t.progress}{t.bold}ready{t.reset}{t.highlight_bg}"
    )
    meta_line = _fit_ansi_line(meta, card_width)
    blank_line = _fit_ansi_line("", card_width)

    card_lines = [blank_line, *prompt_lines, blank_line, meta_line]
    return "\n".join(
        f"{indent}{t.progress}▌{t.reset}{t.highlight_bg}{line}{t.reset}"
        for line in card_lines
    )


def _render_welcome_body(args: TtyAppArgs, state: ScreenState) -> str:
    t = theme()
    cols, _ = _get_terminal_size()
    logo_lines = [
        f"{t.assistant}{t.bold}█▀▄▀█ █ █▄ █ █{t.reset}   {t.progress}{t.bold}█▀▀ █   ▄▀█ █ █ █▀▄ █▀▀{t.reset}   {t.expandable}{t.bold}█▀▀ █▀█ █▀▄ █▀▀{t.reset}",
        f"{t.assistant}{t.bold}█ ▀ █ █ █ ▀█ █{t.reset}   {t.progress}{t.bold}█▄▄ █▄▄ █▀█ █▄█ █▄▀ ██▄{t.reset}   {t.expandable}{t.bold}█▄▄ █▄█ █▄▀ ██▄{t.reset}",
    ]
    return "\n".join(
        [
            _center_ansi_line(logo_lines[0], cols),
            _center_ansi_line(logo_lines[1], cols),
            "",
            _center_ansi_line(f"{t.subtle}local agent · code, tests, edits, review{t.reset}", cols),
            "",
        ]
    )


def render_home_screen(args: TtyAppArgs, state: ScreenState) -> str:
    t = theme()
    cols, rows = _get_terminal_size()
    body_parts = [
        _render_welcome_body(args, state),
        _render_home_input_card(args, state),
        "",
        _center_ansi_line(
            f"{t.assistant}{t.bold}tab{t.reset} {t.subtle}history{t.reset}   "
            f"{t.assistant}{t.bold}/help{t.reset} {t.subtle}commands{t.reset}   "
            f"{t.assistant}{t.bold}enter{t.reset} {t.subtle}send{t.reset}",
            cols,
        ),
        "",
        _center_ansi_line(
            f"{t.progress}● Tip{t.reset} {t.subtle}Tell me what outcome you want; tools stay collapsed until useful.{t.reset}",
            cols,
        ),
    ]
    body = "\n".join(body_parts)
    body_height = body.count("\n") + 1
    top_pad = "\n" * max(1, min(8, (rows - body_height) // 3))
    return top_pad + body


