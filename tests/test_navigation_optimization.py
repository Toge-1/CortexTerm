from pathlib import Path
from io import BytesIO
import json
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cortexterm.tooling import ToolContext
from cortexterm.tools.read_file import read_file_tool, MAX_READ_LIMIT
from cortexterm.tools.grep_files import grep_files_tool
from cortexterm.tui.types import TranscriptEntry
import cortexterm.tui.transcript as transcript


def read(tmp_path, **kwargs):
    return read_file_tool.run(read_file_tool.validator({"path": "sample.py", **kwargs}), ToolContext(cwd=str(tmp_path)))


def test_read_lines_inclusive_unicode_and_character_compatibility(tmp_path):
    (tmp_path / "sample.py").write_bytes("one\r\n中文\r\nthree\r\nfour\r\n".encode())
    result = read(tmp_path, start_line=2, end_line=3)
    assert "LINES: 2-3" in result.output
    assert result.output.endswith("2: 中文\n3: three")
    assert "TRUNCATED: no" in result.output
    assert read(tmp_path, offset=4, limit=2).output.endswith("中文")


@pytest.mark.parametrize("kwargs", [{"start_line": 0}, {"start_line": 1.5}, {"start_line": True},
    {"start_line": 3, "end_line": 2}, {"start_line": 1, "offset": 0}, {"end_line": 3, "limit": 12}])
def test_read_lines_rejects_ambiguous_or_invalid_ranges(kwargs):
    with pytest.raises(ValueError):
        read_file_tool.validator({"path": "file.py", **kwargs})


def test_read_lines_caps_output_and_exposes_exact_continuation(tmp_path):
    content = "prefix\n" + "x" * (MAX_READ_LIMIT + 30) + "\ntail\n"
    (tmp_path / "sample.py").write_text(content, encoding="utf-8")
    result = read(tmp_path, start_line=2, end_line=3)
    offset = len("prefix\n") + MAX_READ_LIMIT
    assert f"continue with offset {offset}" in result.output
    continuation = read(tmp_path, offset=offset, limit=100)
    assert continuation.output.endswith("x" * 30 + "\ntail\n")


def test_read_lines_caps_lines_handles_empty_and_eof(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("line\n" * 250, encoding="utf-8")
    result = read(tmp_path, start_line=1, end_line=250)
    assert "LINES: 1-200" in result.output
    assert "continue with start_line 201" in result.output
    assert "No lines" in read(tmp_path, start_line=251).output
    target.write_text("", encoding="utf-8")
    assert "TOTAL_LINES: 0" in read(tmp_path, start_line=1).output


def test_read_lines_still_enforces_workspace_access(tmp_path):
    with pytest.raises(PermissionError):
        read_file_tool.run({"path": "../outside.py", "start_line": 1}, ToolContext(cwd=str(tmp_path)))


def test_grep_single_file_returns_line_coordinates(tmp_path):
    (tmp_path / "sample.py").write_text("before\ndef target():\n    return 1\n", encoding="utf-8")
    result = grep_files_tool.run({"path": "sample.py", "pattern": "def target"}, ToolContext(cwd=str(tmp_path)))
    assert "sample.py:2:def target():" in result.output


def test_long_history_reuses_heights_without_growing_render_cache():
    entries = [TranscriptEntry(id=i, kind="assistant", body=f"entry {i}\n" + "x" * 150) for i in range(2000)]
    expected = transcript.render_transcript(entries, 0, 30, 80)
    with patch.object(transcript, "_render_transcript_entry", wraps=transcript._render_transcript_entry) as spy:
        assert transcript.render_transcript(list(entries), 0, 30, 80) == expected
        assert spy.call_count == 0
    assert len(transcript._screen_line_cache) <= transcript._CACHE_MAX_SIZE + 1
    entries[-1].body += "\nupdated 中文"
    assert "updated" in transcript.render_transcript(entries, 0, 30, 80)
    entries.clear()
    assert transcript.render_transcript(entries, 0, 30, 80) == ""
    assert transcript._layout_cache.states == []


def test_viewport_matches_full_materialization_after_mutations():
    entries = [TranscriptEntry(id=i, kind="user", body=("中文 long text " * (i % 10 + 1))) for i in range(40)]

    # Independent full-materialization reference, with the same repeated header
    # behavior when a viewport starts partway through a message.
    def reference(width, offset, size):
        rows, segments = [], []
        for i, entry in enumerate(entries):
            if i:
                rows.extend(transcript._SEPARATOR_LINES)
            start = len(rows)
            lines = []
            for line in transcript._render_transcript_entry(entry).split("\n"):
                lines.extend(transcript.wrap_panel_body_line(line, width))
            rows.extend(lines)
            segments.append((start, len(rows), lines[0]))
        maximum = max(0, len(rows) - size)
        offset = min(offset, maximum)
        end = len(rows) - offset
        start = max(0, end - (size - 1 if maximum else size))
        visible = rows[start:end]
        for left, right, header in segments:
            if left < start < right:
                visible.insert(0, header)
                break
        if maximum:
            theme = transcript.theme()
            visible.append(f"{theme.subtle}  {transcript.ICON_DIVIDER * 2} scroll {offset}/{maximum} (PgUp/PgDn or scroll){transcript.ICON_DIVIDER * 2}{theme.reset}")
        return "\n".join(visible)

    for mutation in range(4):
        for width in [40, 80, 120]:
            for offset in [0, 1, 17, 90, 100000]:
                assert transcript.render_transcript(entries, offset, 12, width) == reference(width, offset, 12)
        if mutation == 0:
            entries[10].body = "changed\n" * 40
        elif mutation == 1:
            entries.insert(0, TranscriptEntry(id=99, kind="user", body="inserted"))
        else:
            del entries[5:15]


def test_partial_entry_mouse_range_includes_repeated_header():
    entries = [TranscriptEntry(id=1, kind="tool", body="a\nb\nc\nd\ne", toolName="read_file", status="success")]
    rendered = transcript.render_transcript(entries, 0, 4, 80)
    ranges = transcript.get_transcript_visible_entry_ranges(entries, 0, 4, 80)
    assert ranges == [(1, 0, len(rendered.splitlines()) - 1)]


@pytest.mark.parametrize("chunk_size", [1, 5, 17])
def test_mcp_content_length_accepts_short_reads_and_consecutive_frames(chunk_size):
    from cortexterm.mcp import StdioMcpClient
    class ShortReads(BytesIO):
        def read(self, n=-1):
            return super().read(min(n, chunk_size))
    messages = [{"id": 1, "result": {"text": "中文"}}, {"id": 2, "result": {"value": 42}}]
    wire = b""
    for message in messages:
        data = json.dumps(message, ensure_ascii=False).encode()
        wire += f"Content-Length: {len(data)}\r\n\r\n".encode() + data
    client = StdioMcpClient("short-read", {}, ".")
    client.process = SimpleNamespace(stdout=ShortReads(wire), poll=lambda: 0)
    queues = [Queue(), Queue()]
    client._pending = {1: queues[0], 2: queues[1]}
    client._consume_stdout()
    assert [q.get_nowait() for q in queues] == messages
    assert not client._pending


def test_mcp_partial_frame_eof_reports_failure_to_waiter():
    from cortexterm.mcp import StdioMcpClient
    client = StdioMcpClient("truncated", {}, ".")
    client.process = SimpleNamespace(stdout=BytesIO(b'Content-Length: 100\r\n\r\n{"id":1'), poll=lambda: 23)
    pending = Queue()
    client._pending = {1: pending}
    client._consume_stdout()
    assert pending.get_nowait()["error"]["code"] == -1
    assert not client._pending


def test_scroll_and_render_use_the_same_width():
    from cortexterm.tui import render_session
    from cortexterm.tui.state import ScreenState
    entries = [TranscriptEntry(id=i, kind="user", body="x" * 400) for i in range(3)]
    state = ScreenState(transcript=entries)
    with patch.object(render_session, "get_transcript_panel_width", return_value=70), patch.object(render_session, "get_transcript_body_lines", return_value=10):
        assert render_session.get_max_transcript_scroll_offset(None, state) == transcript.get_transcript_max_scroll_offset(entries, 10, 70)
