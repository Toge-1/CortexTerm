import json

import pytest

from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.tools.code_nav import find_symbols_tool
from cortexterm.tools.grep_files import grep_files_tool
from cortexterm.tools.read_file import read_file_tool


SOURCE = """import os

@register(
    "first",
)
class Worker:
    @trace
    async def run(
        self,
        value,
    ):
        def helper(item):
            return item + 1

        return helper(value)

def helper(value):
    return value * 2

FLAG = True
"""


@pytest.fixture
def navigation(tmp_path):
    (tmp_path / "sample.py").write_text(SOURCE, encoding="utf-8")
    registry = ToolRegistry([grep_files_tool, find_symbols_tool, read_file_tool])
    return registry, ToolContext(cwd=str(tmp_path))


def find(navigation, **kwargs):
    registry, context = navigation
    return registry.execute("find_symbols", {"path": "sample.py", **kwargs}, context)


def read_args(result):
    assert result.ok, result.output
    return json.loads(result.output.split("read_file arguments: ", 1)[1].splitlines()[0])


def test_symbol_outline_includes_read_boundaries_and_qualified_names(navigation):
    result = find(navigation)
    assert result.ok
    assert "Worker (start_line=3, end_line=15; definition_line=6)" in result.output
    assert "Worker.run(self, value) (start_line=7, end_line=15; definition_line=8)" in result.output
    assert "Worker.run.helper(item) (start_line=12, end_line=13; definition_line=12)" in result.output
    assert "[FUN] helper(value) (start_line=17, end_line=18; definition_line=17)" in result.output
    assert "return item + 1" not in result.output  # Outline does not send implementation text.


def test_grep_to_innermost_symbol_to_exact_source(navigation):
    registry, context = navigation
    hit = registry.execute("grep_files", {"path": ".", "pattern": r"return item \+ 1"}, context)
    assert hit.ok
    path, line, _ = hit.output.split(":", 2)
    located = registry.execute("find_symbols", {"path": path, "line": int(line)}, context)
    assert "SYMBOL: Worker.run.helper\n" in located.output
    args = read_args(located)
    assert args == {"path": "sample.py", "start_line": 12, "end_line": 13}
    source = registry.execute("read_file", args, context)
    assert source.ok
    assert "TRUNCATED: no" in source.output
    assert source.output.endswith("12:         def helper(item):\n13:             return item + 1")
    assert "return helper(value)" not in source.output


@pytest.mark.parametrize("line", [7, 8, 9, 10, 11, 14, 15])
def test_async_method_lookup_covers_decorator_signature_and_body(navigation, line):
    result = find(navigation, line=line)
    assert "SYMBOL: Worker.run\n" in result.output
    assert read_args(result) == {"path": "sample.py", "start_line": 7, "end_line": 15}
    registry, context = navigation
    source = registry.execute("read_file", read_args(result), context)
    assert "7:     @trace" in source.output
    assert source.output.endswith("15:         return helper(value)")


@pytest.mark.parametrize("line", [3, 4, 5, 6])
def test_multiline_class_decorator_is_included(navigation, line):
    result = find(navigation, line=line)
    assert "SYMBOL: Worker\n" in result.output
    assert read_args(result) == {"path": "sample.py", "start_line": 3, "end_line": 15}


def test_type_filter_can_select_outer_class_and_duplicate_names_are_unambiguous(navigation):
    result = find(navigation, line=13, symbol_type="class")
    assert "SYMBOL: Worker\n" in result.output
    assert read_args(result)["start_line"] == 3
    result = find(navigation, line=18)
    assert "SYMBOL: helper\n" in result.output
    assert read_args(result) == {"path": "sample.py", "start_line": 17, "end_line": 18}
    result = find(navigation, line=6, symbol_type="function")
    assert "No enclosing function" in result.output


@pytest.mark.parametrize("line", [1, 2, 16, 19, 20, 21])
def test_module_level_gaps_and_eof_do_not_claim_a_nearby_function(navigation, line):
    result = find(navigation, line=line)
    assert result.ok
    assert "No enclosing function or class" in result.output
    assert "read_file arguments:" not in result.output


@pytest.mark.parametrize("line", [0, -1, True, 1.5, "13", None])
def test_invalid_hit_lines_are_rejected(navigation, line):
    result = find(navigation, line=line)
    assert not result.ok
    assert "line must be an integer >= 1" in result.output


def test_line_lookup_rejects_directory_and_variable_filter(navigation):
    result = find(navigation, path=".", line=13)
    assert not result.ok
    assert "single Python file" in result.output
    result = find(navigation, line=13, symbol_type="variable")
    assert not result.ok
    assert "line lookup supports" in result.output


def test_directory_outline_still_works_with_type_filter(navigation, tmp_path):
    (tmp_path / "other.py").write_text("class Other:\n    pass\n", encoding="utf-8")
    result = find(navigation, path=".", symbol_type="class")
    assert result.ok
    assert "Found 2 symbol(s)" in result.output
    assert "sample.py" in result.output
    assert "other.py" in result.output
    assert "Other (start_line=1, end_line=2; definition_line=1)" in result.output
    assert "[FUN]" not in result.output


def test_parse_error_is_reported_instead_of_a_fabricated_boundary(navigation, tmp_path):
    (tmp_path / "sample.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    result = find(navigation, line=2)
    assert not result.ok
    assert "Cannot parse sample.py" in result.output


def test_line_lookup_respects_workspace_boundary(navigation):
    result = find(navigation, path="../outside.py", line=1)
    assert not result.ok
    assert "Path escapes workspace" in result.output


def test_long_function_uses_existing_read_pagination(navigation, tmp_path):
    body = "def long_function():\n" + "    pass\n" * 230
    (tmp_path / "long.py").write_text(body, encoding="utf-8")
    located = find(navigation, path="long.py", line=220)
    args = read_args(located)
    assert args == {"path": "long.py", "start_line": 1, "end_line": 231}
    registry, context = navigation
    first = registry.execute("read_file", args, context)
    assert "LINES: 1-200" in first.output
    assert "continue with start_line 201" in first.output
    last = registry.execute("read_file", {**args, "start_line": 201}, context)
    assert "LINES: 201-231" in last.output
    assert "TRUNCATED: no" in last.output


def test_adjacent_single_line_definitions_do_not_overlap(navigation, tmp_path):
    (tmp_path / "single.py").write_text("def first(): return 1\ndef second(): return 2\n", encoding="utf-8")
    assert read_args(find(navigation, path="single.py", line=1)) == {
        "path": "single.py", "start_line": 1, "end_line": 1,
    }
    assert read_args(find(navigation, path="single.py", line=2)) == {
        "path": "single.py", "start_line": 2, "end_line": 2,
    }
