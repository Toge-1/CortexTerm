from pathlib import Path
from unittest.mock import Mock

import pytest

from cortexterm.tooling import ToolContext
from cortexterm.workspace import resolve_tool_path


class MockPermissionManager:
    """Mock permission manager for testing."""

    def __init__(self):
        self.access_checks = []

    def ensure_path_access(self, path: str, intent: str) -> None:
        """Mock ensure_path_access method."""
        self.access_checks.append((path, intent))


def test_resolve_tool_path_with_absolute_path_and_permissions(tmp_path: Path) -> None:
    """Test resolving an absolute path with a permission manager."""
    permissions = MockPermissionManager()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    target_file = tmp_path / "test.txt"
    target_file.write_text("content", encoding="utf-8")

    result = resolve_tool_path(context, str(target_file.absolute()), "read")

    assert result == target_file.resolve()
    assert len(permissions.access_checks) == 1
    assert permissions.access_checks[0][0] == str(target_file.resolve())
    assert permissions.access_checks[0][1] == "read"


def test_resolve_tool_path_with_absolute_path_without_permissions(tmp_path: Path) -> None:
    """Test resolving an absolute path without a permission manager."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    target_file = tmp_path / "test.txt"
    target_file.write_text("content", encoding="utf-8")

    result = resolve_tool_path(context, str(target_file.absolute()), "read")

    assert result == target_file.resolve()


def test_resolve_tool_path_with_relative_path_and_permissions(tmp_path: Path) -> None:
    """Test resolving a relative path with a permission manager."""
    permissions = MockPermissionManager()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    target_file = tmp_path / "subdir" / "test.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)

    result = resolve_tool_path(context, "subdir/test.txt", "write")

    assert result == target_file.resolve()
    assert len(permissions.access_checks) == 1
    assert permissions.access_checks[0][0] == str(target_file.resolve())
    assert permissions.access_checks[0][1] == "write"


def test_resolve_tool_path_with_relative_path_without_permissions(tmp_path: Path) -> None:
    """Test resolving a relative path without a permission manager."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    target_file = tmp_path / "subdir" / "test.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)

    result = resolve_tool_path(context, "subdir/test.txt", "read")

    assert result == target_file.resolve()


def test_resolve_tool_path_with_dot_notation(tmp_path: Path) -> None:
    """Test resolving a path with '.' notation."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "./test.txt", "read")

    expected = (tmp_path / "test.txt").resolve()
    assert result == expected


def test_resolve_tool_path_with_double_dot_notation(tmp_path: Path) -> None:
    """Reject '..' when it escapes the fallback workspace root."""
    subdir = tmp_path / "subdir"
    subdir.mkdir(parents=True, exist_ok=True)
    context = ToolContext(cwd=str(subdir), permissions=None)

    with pytest.raises(PermissionError, match="Path escapes workspace"):
        resolve_tool_path(context, "../test.txt", "read")


def test_resolve_tool_path_with_mixed_dot_notations(tmp_path: Path) -> None:
    """Reject multiple '..' segments that escape the workspace root."""
    subdir1 = tmp_path / "subdir1"
    subdir2 = subdir1 / "subdir2"
    subdir2.mkdir(parents=True, exist_ok=True)
    context = ToolContext(cwd=str(subdir2), permissions=None)

    with pytest.raises(PermissionError, match="Path escapes workspace"):
        resolve_tool_path(context, "../../test.txt", "read")


def test_resolve_tool_path_with_extra_separators(tmp_path: Path) -> None:
    """Test resolving a path with extra path separators."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "subdir///test.txt", "read")

    expected = (tmp_path / "subdir" / "test.txt").resolve()
    assert result == expected


def test_resolve_tool_path_current_directory(tmp_path: Path) -> None:
    """Test resolving the current directory."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, ".", "read")

    assert result == tmp_path.resolve()


def test_resolve_tool_path_trailing_separator(tmp_path: Path) -> None:
    """Test resolving a path with a trailing separator."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    subdir = tmp_path / "subdir"
    subdir.mkdir(parents=True, exist_ok=True)

    result = resolve_tool_path(context, "subdir/", "read")

    assert result == subdir.resolve()


def test_resolve_tool_path_escape_workspace_without_permissions(tmp_path: Path) -> None:
    """Test that escaping workspace raises PermissionError when no permissions manager."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    with pytest.raises(PermissionError) as exc_info:
        resolve_tool_path(context, "../escape.txt", "read")

    assert "Path escapes workspace" in str(exc_info.value)
    assert "../escape.txt" in str(exc_info.value)


def test_resolve_tool_path_deep_escape_without_permissions(tmp_path: Path) -> None:
    """Test that deeply escaping workspace raises PermissionError."""
    subdir = tmp_path / "deep" / "nested" / "dir"
    subdir.mkdir(parents=True, exist_ok=True)
    context = ToolContext(cwd=str(subdir), permissions=None)

    with pytest.raises(PermissionError) as exc_info:
        resolve_tool_path(context, "../../../escape.txt", "read")

    assert "Path escapes workspace" in str(exc_info.value)


def test_resolve_tool_path_escape_workspace_with_permissions(tmp_path: Path) -> None:
    """Test that escaping workspace with permissions manager delegates to ensure_path_access."""
    permissions = MockPermissionManager()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    result = resolve_tool_path(context, "../escape.txt", "read")

    assert len(permissions.access_checks) == 1
    assert permissions.access_checks[0][0].endswith("escape.txt")
    assert permissions.access_checks[0][1] == "read"


def test_resolve_tool_path_normalization_removes_symlinks(tmp_path: Path) -> None:
    """Test that path normalization resolves symbolic links."""
    import os

    context = ToolContext(cwd=str(tmp_path), permissions=None)

    target_file = tmp_path / "actual.txt"
    target_file.write_text("content", encoding="utf-8")

    if os.name == "nt":
        pytest.skip("Creating symlinks requires additional Windows privileges")

    link_file = tmp_path / "link.txt"
    link_file.symlink_to("actual.txt")

    result = resolve_tool_path(context, "link.txt", "read")

    assert result == target_file.resolve()


def test_resolve_tool_path_empty_path(tmp_path: Path) -> None:
    """Test resolving an empty path (current directory)."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "", "read")

    assert result == tmp_path.resolve()


def test_resolve_tool_path_with_spaces_in_path(tmp_path: Path) -> None:
    """Test resolving a path with spaces."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "sub dir/test file.txt", "read")

    expected = (tmp_path / "sub dir" / "test file.txt").resolve()
    assert result == expected


def test_resolve_tool_path_with_unicode_characters(tmp_path: Path) -> None:
    """Test resolving a path with unicode characters."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "测试/文件.txt", "read")

    expected = (tmp_path / "测试" / "文件.txt").resolve()
    assert result == expected


def test_resolve_tool_path_different_intents(tmp_path: Path) -> None:
    """Test resolving path with different intent values."""
    permissions = MockPermissionManager()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    intents = ["read", "write", "delete", "execute"]
    for intent in intents:
        permissions.access_checks.clear()
        result = resolve_tool_path(context, "test.txt", intent)

        assert len(permissions.access_checks) == 1
        assert permissions.access_checks[0][1] == intent


def test_resolve_tool_path_absolute_path_on_different_drive_windows(tmp_path: Path) -> None:
    """Reject an absolute path outside the workspace without a permission manager."""
    import os

    if os.name != "nt":
        pytest.skip("Windows-specific test")

    context = ToolContext(cwd=str(tmp_path), permissions=None)

    with pytest.raises(PermissionError, match="Path escapes workspace"):
        resolve_tool_path(context, "C:\\Windows\\System32", "read")


def test_resolve_tool_path_nested_directories(tmp_path: Path) -> None:
    """Test resolving deeply nested directory paths."""
    nested = tmp_path / "a" / "b" / "c" / "d" / "e"
    nested.mkdir(parents=True, exist_ok=True)
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "a/b/c/d/e/test.txt", "read")

    expected = nested.resolve() / "test.txt"
    assert result == expected


def test_resolve_tool_path_case_preservation(tmp_path: Path) -> None:
    """Test that path case is preserved on case-sensitive systems."""
    context = ToolContext(cwd=str(tmp_path), permissions=None)

    result = resolve_tool_path(context, "TestFile.TXT", "read")

    expected = (tmp_path / "TestFile.TXT").resolve()
    assert result == expected


def test_resolve_tool_path_permission_manager_called_with_correct_path(tmp_path: Path) -> None:
    """Test that permission manager is called with the fully resolved path."""
    permissions = MockPermissionManager()
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    result = resolve_tool_path(context, "./subdir/../test.txt", "read")

    # The permission manager should be called with the normalized path
    assert len(permissions.access_checks) == 1
    called_path = permissions.access_checks[0][0]
    # The called path should not contain '..' or '.'
    assert ".." not in called_path
    assert called_path.endswith("test.txt")
    assert result == Path(called_path)
