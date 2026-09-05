from cortexterm.tty_app import (
    _apply_tool_result_visual_state,
    _format_history,
    _handle_event,
    _handle_input,
    _handle_mouse_event,
    _handle_normal_mode_wheel,
    _install_transcript_mouse_zones,
    _mark_unfinished_tools,
    _save_transcript,
    MouseZone,
    ScreenState,
    TtyAppArgs,
    summarize_tool_input,
    summarize_tool_output,
)
from cortexterm.permissions import PermissionManager
from cortexterm.tui.input_parser import KeyEvent, MouseEvent, TextEvent, WheelEvent, parse_input_chunk
from cortexterm.tui.transcript import format_transcript_text
from cortexterm.tui.types import TranscriptEntry


def test_summarize_tool_output_prefers_first_meaningful_line() -> None:
    output = "\n\nFILE: README.md\nOFFSET: 0\nEND: 100"
    assert summarize_tool_output("read_file", output).startswith("FILE: README.md")


def test_summarize_tool_output_truncates_long_lines() -> None:
    output = "x" * 400
    summary = summarize_tool_output("run_command", output)
    assert len(summary) < 200
    assert summary.endswith("...")


def test_format_history_shows_recent_entries_with_numbers() -> None:
    rendered = _format_history(["/help", "build parser", "/cmd pytest -q"], limit=2)
    assert rendered == "2. build parser\n3. /cmd pytest -q"


def test_save_transcript_writes_plain_text(tmp_path) -> None:
    state_entries = [
        TranscriptEntry(id=1, kind="user", body="hello"),
        TranscriptEntry(id=2, kind="assistant", body="world"),
    ]
    permissions = PermissionManager(str(tmp_path), prompt=lambda request: {"decision": "allow_once"})

    path = _save_transcript(
        type("State", (), {"transcript": state_entries})(),
        str(tmp_path),
        permissions,
        "logs/session.txt",
    )

    assert path.endswith("logs\\session.txt") or path.endswith("logs/session.txt")
    assert (tmp_path / "logs" / "session.txt").read_text(encoding="utf-8") == "user\n  hello\n\n---\n\nassistant\n  world"


def test_format_transcript_text_uses_clean_separator() -> None:
    rendered = format_transcript_text(
        [
            TranscriptEntry(id=1, kind="user", body="one"),
            TranscriptEntry(id=2, kind="assistant", body="two"),
        ]
    )

    assert "\n\n---\n\n" in rendered


def test_summarize_tool_input_formats_patch_file() -> None:
    summary = summarize_tool_input(
        "patch_file",
        {"path": "demo.txt", "replacements": [{"search": "a", "replace": "b"}, {"search": "c", "replace": "d"}]},
    )

    assert summary == "patch_file path=demo.txt replacements=2"


def test_mark_unfinished_tools_marks_running_entries_as_errors() -> None:
    state = type(
        "State",
        (),
        {
            "transcript": [TranscriptEntry(id=1, kind="tool", body="running", toolName="run_command", status="running")],
            "recent_tools": [],
            "pending_tool_runs": {"run_command": [{"entry": "placeholder"}]},
            "active_tool": "run_command",
        },
    )()

    count = _mark_unfinished_tools(state)

    assert count == 1
    assert state.transcript[0].status == "error"
    assert "did not report a final result" in state.transcript[0].body
    assert state.recent_tools == [{"name": "run_command", "status": "error"}]
    assert state.pending_tool_runs == {}
    assert state.active_tool is None


def test_error_tool_entry_stays_expanded_for_visibility() -> None:
    entry = TranscriptEntry(id=1, kind="tool", body="boom", toolName="run_command", status="running")
    _apply_tool_result_visual_state(entry, "run_command", "boom", is_error=True)

    assert entry.status == "error"
    assert entry.collapsed is False
    assert entry.collapsedSummary is None


def test_success_tool_entry_collapses_to_summary() -> None:
    entry = TranscriptEntry(id=1, kind="tool", body="running", toolName="read_file", status="running")
    _apply_tool_result_visual_state(entry, "read_file", "FILE: README.md\nhello", is_error=False)

    assert entry.status == "success"
    assert entry.collapsed is True
    assert entry.collapsedSummary == "FILE: README.md"
    assert entry.collapsePhase == 3


def test_ctrl_r_enters_transcript_reading_mode() -> None:
    parsed = parse_input_chunk("\x12")

    assert parsed.events == [KeyEvent(name="r", ctrl=True, meta=False)]


def test_sgr_mouse_click_is_parsed_with_coordinates() -> None:
    parsed = parse_input_chunk("\x1b[<0;12;8m")

    assert parsed.events == [MouseEvent(button="left", action="up", x=12, y=8)]


def test_mouse_click_toggles_tool_entry_collapse() -> None:
    state = ScreenState(
        transcript=[
            TranscriptEntry(
                id=1,
                kind="tool",
                body="line 1\nline 2",
                toolName="read_file",
                status="success",
                collapsed=True,
                collapsedSummary="line 1",
            )
        ],
        mouse_zones=[MouseZone(y_start=5, y_end=7, entry_id=1, action="toggle_tool")],
    )
    renders = []

    assert _handle_mouse_event(
        state,
        MouseEvent(button="left", action="up", x=20, y=5),
        lambda: renders.append("render"),
    )
    assert state.transcript[0].collapsed is False
    assert state.transcript[0].transition == "opening"
    assert state.transcript[0].revealLines == 1
    assert renders == ["render"]

    assert _handle_mouse_event(
        state,
        MouseEvent(button="left", action="up", x=20, y=5),
        lambda: renders.append("render"),
    )
    assert state.transcript[0].transition == "closing"

    assert _handle_mouse_event(
        state,
        MouseEvent(button="left", action="up", x=20, y=5),
        lambda: renders.append("render"),
    )
    assert state.transcript[0].collapsed is False
    assert state.transcript[0].transition == "opening"


def test_tool_mouse_zone_targets_visible_tool_card(monkeypatch) -> None:
    state = ScreenState(
        transcript=[
            TranscriptEntry(
                id=1,
                kind="tool",
                body="line 1\nline 2\nline 3",
                toolName="read_file",
                status="success",
                collapsed=True,
                collapsedSummary="line 1",
            )
        ],
    )
    args = type("Args", (), {})()
    monkeypatch.setattr("cortexterm.tty_app._get_terminal_size", lambda: (80, 24))

    _install_transcript_mouse_zones(args, state, state.transcript, panel_start_row=1, body_lines=10)

    assert len(state.mouse_zones) == 1
    zone = state.mouse_zones[0]
    assert zone.y_end > zone.y_start + 1


def test_transcript_reading_mode_owns_keyboard_input(monkeypatch) -> None:
    state = ScreenState(
        transcript=[
            TranscriptEntry(id=1, kind="assistant", body="old\n" * 80),
            TranscriptEntry(id=2, kind="assistant", body="new"),
        ]
    )
    args = type("Args", (), {})()
    renders = []

    monkeypatch.setattr("cortexterm.tty_app._get_terminal_size", lambda: (80, 24))

    _handle_event(
        args,
        state,
        KeyEvent(name="r", ctrl=True, meta=False),
        lambda: renders.append("render"),
        type("Evt", (), {"set": lambda self: None})(),
        {},
    )

    assert state.transcript_read_mode is True

    _handle_event(
        args,
        state,
        TextEvent(text="x", ctrl=False, meta=False),
        lambda: renders.append("render"),
        type("Evt", (), {"set": lambda self: None})(),
        {},
    )
    assert state.input == ""

    _handle_event(
        args,
        state,
        KeyEvent(name="pageup", ctrl=False, meta=False),
        lambda: renders.append("render"),
        type("Evt", (), {"set": lambda self: None})(),
        {},
    )
    assert state.transcript_scroll_offset > 0


def test_normal_wheel_scrolls_by_small_smooth_step(monkeypatch) -> None:
    state = ScreenState(
        transcript=[
            TranscriptEntry(id=1, kind="assistant", body="old\n" * 120),
            TranscriptEntry(id=2, kind="assistant", body="new"),
        ]
    )
    args = type(
        "Args",
        (),
        {
            "cwd": ".",
            "model": None,
            "pending_approval": None,
        },
    )()
    renders = []

    monkeypatch.setattr("cortexterm.tty_app._get_terminal_size", lambda: (80, 30))
    monkeypatch.setattr("cortexterm.tty_app._get_chrome_overhead", lambda _args, _state: 8)

    assert _handle_normal_mode_wheel(
        args,
        state,
        WheelEvent(direction="up"),
        lambda: renders.append("render"),
    )

    assert state.transcript_scroll_offset == 3
    assert renders == ["render"]


def test_screen_state_starts_with_welcome_visible() -> None:
    state = ScreenState()

    assert state.show_welcome is True


def test_tui_agent_turn_receives_context_manager(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeTools:
        def list(self):
            return []

        def get_skills(self):
            return []

        def get_mcp_servers(self):
            return []

    class FakePermissions:
        def get_summary(self):
            return []

        def begin_turn(self):
            pass

        def end_turn(self):
            pass

    class FakeMemory:
        def get_relevant_context(self):
            return ""

    context_mgr = object()

    def fake_run_agent_turn(**kwargs):
        captured["context_manager"] = kwargs.get("context_manager")
        return kwargs["messages"] + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr("cortexterm.tty_app.run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr("cortexterm.tty_app.save_history_entries", lambda _history: None)

    args = TtyAppArgs(
        runtime={},
        tools=FakeTools(),
        model=object(),
        messages=[{"role": "system", "content": "old"}],
        cwd=str(tmp_path),
        permissions=FakePermissions(),
        memory_mgr=FakeMemory(),
        context_mgr=context_mgr,
    )
    state = ScreenState()

    assert _handle_input(args, state, lambda: None, "hello") is False
    state.agent_thread.join(timeout=2)

    assert captured["context_manager"] is context_mgr


def test_tui_renders_context_compaction_events(monkeypatch, tmp_path) -> None:
    class FakeTools:
        def list(self):
            return []

        def get_skills(self):
            return []

        def get_mcp_servers(self):
            return []

    class FakePermissions:
        def get_summary(self):
            return []

        def begin_turn(self):
            pass

        def end_turn(self):
            pass

    class FakeMemory:
        def get_relevant_context(self):
            return ""

    def fake_run_agent_turn(**kwargs):
        kwargs["on_context_event"](
            "compact_start",
            {
                "before_tokens": 190,
                "context_window": 200,
                "usage_percentage": 95,
                "messages_count": 20,
            },
        )
        kwargs["on_context_event"](
            "compact_done",
            {
                "before_tokens": 190,
                "after_tokens": 120,
                "context_window": 200,
                "usage_percentage": 60,
                "messages_removed": 8,
                "summary": "Context: ok",
            },
        )
        return kwargs["messages"] + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr("cortexterm.tty_app.run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr("cortexterm.tty_app.save_history_entries", lambda _history: None)

    args = TtyAppArgs(
        runtime={},
        tools=FakeTools(),
        model=object(),
        messages=[{"role": "system", "content": "old"}],
        cwd=str(tmp_path),
        permissions=FakePermissions(),
        memory_mgr=FakeMemory(),
        context_mgr=object(),
    )
    state = ScreenState()

    assert _handle_input(args, state, lambda: None, "hello") is False
    state.agent_thread.join(timeout=2)

    progress_bodies = [
        entry.body for entry in state.transcript if entry.kind == "progress"
    ]
    assert any("Context compaction started" in body for body in progress_bodies)
    assert any("Context compaction complete" in body for body in progress_bodies)
