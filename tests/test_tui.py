from cortexterm.tui import (
    get_transcript_max_scroll_offset,
    render_banner,
    render_input_prompt,
    render_panel,
    render_permission_prompt,
    render_transcript,
)
from cortexterm.tui.chrome import strip_ansi
from cortexterm.tui.input import get_input_cursor_cell
from cortexterm.tui.types import TranscriptEntry


def test_render_panel_contains_title() -> None:
    rendered = render_panel("Demo", "body")
    assert "Demo" in rendered
    assert "body" in rendered


def test_render_panel_can_clamp_body_height() -> None:
    rendered = render_panel("Demo", "one\ntwo\nthree", min_body_lines=2, max_body_lines=2)

    assert "one" in rendered
    assert "two" in rendered
    assert "three" not in rendered


def test_render_panel_is_frameless_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CORTEXTERM_PANEL_FRAMES", raising=False)

    rendered = render_panel("Demo", "body")

    assert "Demo" in rendered
    assert "body" in rendered
    assert "|" not in rendered
    assert not rendered.startswith("+")


def test_render_panel_frames_are_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("CORTEXTERM_PANEL_FRAMES", "1")

    rendered = render_panel("Demo", "body")

    assert "|" in rendered or "│" in rendered


def test_render_banner_includes_model() -> None:
    rendered = render_banner(
        {"model": "claude-test", "baseUrl": "https://api.anthropic.com"},
        "/tmp/demo",
        ["cwd: /tmp/demo"],
        {"transcriptCount": 1, "messageCount": 2, "skillCount": 3, "mcpCount": 4},
    )
    assert "claude-test" in rendered
    assert "api.anthropic.com" in rendered


def test_render_input_prompt_wraps_multiline_input() -> None:
    text = "alpha\nbeta"
    rendered = strip_ansi(render_input_prompt(text, len(text), compact=True))

    assert "user > alpha" in rendered
    assert "\n        beta" in rendered
    assert "cortexterm>" not in rendered


def test_input_cursor_cell_tracks_ascii_input(monkeypatch) -> None:
    monkeypatch.setattr("cortexterm.tui.input._cached_terminal_size", lambda: (80, 24))

    row, col = get_input_cursor_cell("hello", 5)

    assert row == 0
    assert col == len(" user > ") + len("hello") + 1


def test_input_cursor_cell_tracks_wrapped_cjk_input(monkeypatch) -> None:
    monkeypatch.setattr("cortexterm.tui.input._cached_terminal_size", lambda: (20, 24))

    row, col = get_input_cursor_cell("中" * 9, 9)

    assert row == 1
    assert col == len(" user > ") + 2 + 1


def test_render_input_prompt_windows_long_input_near_cursor(monkeypatch) -> None:
    monkeypatch.setattr("cortexterm.tui.input._cached_terminal_size", lambda: (24, 24))
    text = "one\ntwo\nthree\nfour\nfive"

    rendered = strip_ansi(render_input_prompt(text, len(text), compact=True, max_lines=3))

    assert "... 2 earlier lines" in rendered
    assert "three" in rendered
    assert "five" in rendered
    assert "one" not in rendered


def test_render_transcript_shows_tool_entry() -> None:
    transcript = [
        TranscriptEntry(id=1, kind="user", body="hi"),
        TranscriptEntry(id=2, kind="tool", body="done", toolName="read_file", status="success"),
    ]
    rendered = render_transcript(transcript, scroll_offset=0)
    assert "read_file" in rendered
    assert "ok" in rendered


def test_render_transcript_uses_user_label() -> None:
    rendered = render_transcript(
        [TranscriptEntry(id=1, kind="user", body="hello")],
        scroll_offset=0,
        window_size=4,
        width=80,
    )

    assert "user" in rendered
    assert "you" not in rendered


def test_render_transcript_preserves_label_when_window_starts_inside_entry() -> None:
    rendered = render_transcript(
        [TranscriptEntry(id=1, kind="user", body="one\ntwo\nthree\nfour\nfive")],
        scroll_offset=0,
        window_size=4,
        width=80,
    )

    assert "user" in rendered
    assert "five" in rendered


def test_render_transcript_shows_intermediate_collapse_phase() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsePhase=1,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "run_command" in rendered
    assert "collapsing" in rendered


def test_render_transcript_shows_collapsed_summary_when_fully_collapsed() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsed=True,
            collapsedSummary="short summary",
            collapsePhase=3,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "run_command" in rendered
    assert "short summary" in rendered
    assert "full output here" not in rendered


def test_transcript_scroll_uses_wrapped_terminal_rows() -> None:
    transcript = [
        TranscriptEntry(id=1, kind="assistant", body="a" * 36 + "b" * 36 + "c" * 36 + "d" * 36)
    ]

    max_offset = get_transcript_max_scroll_offset(transcript, window_size=4, width=40)
    rendered = render_transcript(transcript, scroll_offset=0, window_size=4, width=40)

    # The two-space transcript indentation causes this 144-character line to
    # occupy five panel rows at width 40 (inner width 36). Together with its
    # label that is six terminal rows, not two logical newline-delimited rows.
    assert max_offset == 2
    assert "d" * 34 in rendered
    assert "scroll 0/2" in rendered
    assert "assistant" in rendered


def test_transcript_scroll_accounts_for_cjk_display_width() -> None:
    transcript = [TranscriptEntry(id=1, kind="assistant", body="\u4e2d" * 72)]

    max_offset = get_transcript_max_scroll_offset(transcript, window_size=4, width=40)
    rendered = render_transcript(transcript, scroll_offset=0, window_size=4, width=40)

    # Each CJK character is two terminal columns; the 36-column panel body
    # therefore needs five rows for seventy-two characters after indentation,
    # plus the label.
    assert max_offset == 2
    assert rendered.count("\u4e2d") == 37


def test_render_permission_prompt_lists_choices() -> None:
    rendered = render_permission_prompt(
        {
            "summary": "Need approval",
            "details": ["target: demo.txt"],
            "choices": [{"key": "1", "label": "allow once"}],
        }
    )
    assert "Need approval" in rendered
    assert "allow once" in rendered
