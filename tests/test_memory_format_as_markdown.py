"""Tests for MemoryFile.format_as_markdown method."""

import pytest
from unittest.mock import patch
from cortexterm.memory import MemoryFile, MemoryEntry, MemoryScope


class TestFormatAsMarkdown:
    """Test cases for MemoryFile.format_as_markdown method."""

    def test_format_empty_memory_with_header(self):
        """Test formatting empty memory file with header included."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        result = memory_file.format_as_markdown(include_header=True)

        assert "# User Memory" in result
        assert "*Last updated:" in result
        assert "## " not in result  # No categories for empty memory

    def test_format_empty_memory_without_header(self):
        """Test formatting empty memory file without header."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        result = memory_file.format_as_markdown(include_header=False)

        assert "# User Memory" not in result
        assert "*Last updated:" not in result
        assert result == ""

    def test_format_user_scope_memory(self):
        """Test formatting USER scope memory with correct header."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="architecture",
            content="Use MVC pattern"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=True)

        assert "# User Memory" in result

    def test_format_project_scope_memory(self):
        """Test formatting PROJECT scope memory with correct header."""
        memory_file = MemoryFile(scope=MemoryScope.PROJECT)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.PROJECT,
            category="convention",
            content="Use snake_case"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=True)

        assert "# Project Memory" in result

    def test_format_local_scope_memory(self):
        """Test formatting LOCAL scope memory with correct header."""
        memory_file = MemoryFile(scope=MemoryScope.LOCAL)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.LOCAL,
            category="decision",
            content="Use pytest"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=True)

        assert "# Local Memory" in result

    def test_format_single_entry_without_tags(self):
        """Test formatting memory with single entry without tags."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="architecture",
            content="Use MVC pattern"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "## Architecture" in result
        assert "- Use MVC pattern" in result
        assert "`" not in result  # No tags, no backticks

    def test_format_single_entry_with_tags(self):
        """Test formatting memory with single entry with tags."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="pattern",
            content="Use factory pattern",
            tags=["design", "creational"]
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "## Pattern" in result
        assert "- Use factory pattern `design creational`" in result

    def test_format_multiple_entries_same_category(self):
        """Test formatting memory with multiple entries in same category."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry1 = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="convention",
            content="Use snake_case"
        )
        entry2 = MemoryEntry(
            id="test-2",
            scope=MemoryScope.USER,
            category="convention",
            content="Use type hints"
        )
        memory_file.add_entry(entry1)
        memory_file.add_entry(entry2)

        result = memory_file.format_as_markdown(include_header=False)

        assert "## Convention" in result
        assert "- Use snake_case" in result
        assert "- Use type hints" in result
        assert result.count("## Convention") == 1  # Only one category header

    def test_format_multiple_entries_different_categories(self):
        """Test formatting memory with entries in different categories."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry1 = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="architecture",
            content="Use MVC"
        )
        entry2 = MemoryEntry(
            id="test-2",
            scope=MemoryScope.USER,
            category="convention",
            content="Use snake_case"
        )
        memory_file.add_entry(entry1)
        memory_file.add_entry(entry2)

        result = memory_file.format_as_markdown(include_header=False)

        assert "## Architecture" in result
        assert "## Convention" in result
        assert "- Use MVC" in result
        assert "- Use snake_case" in result

    def test_format_category_title_capitalization(self):
        """Test that category titles are properly capitalized."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="my_custom_category",
            content="Test content"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "## My_Custom_Category" in result

    def test_format_entry_with_empty_tags(self):
        """Test formatting entry with empty tags list."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="Test content",
            tags=[]
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "- Test content" in result
        assert "`" not in result  # No backticks for empty tags

    def test_format_entry_with_single_tag(self):
        """Test formatting entry with single tag."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="Test content",
            tags=["important"]
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "- Test content `important`" in result

    def test_format_entry_with_multiple_tags(self):
        """Test formatting entry with multiple tags."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="Test content",
            tags=["tag1", "tag2", "tag3"]
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "- Test content `tag1 tag2 tag3`" in result

    def test_format_preserves_entry_order_within_category(self):
        """Test that entries maintain their order within a category."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entries = [
            MemoryEntry(id=f"test-{i}", scope=MemoryScope.USER, category="general", content=f"Content {i}")
            for i in range(5)
        ]
        for entry in entries:
            memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        lines = result.strip().split("\n")
        content_lines = [line for line in lines if line.startswith("- ")]
        assert len(content_lines) == 5
        for i, line in enumerate(content_lines):
            assert f"Content {i}" in line

    def test_format_with_special_characters_in_content(self):
        """Test formatting content with special characters."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="Special chars: <>&\"' and unicode: 中文 🎉"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "Special chars: <>&\"' and unicode: 中文 🎉" in result

    def test_format_with_newlines_in_content(self):
        """Test formatting content containing newlines."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="Line 1\nLine 2\nLine 3"
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert "Line 1\nLine 2\nLine 3" in result

    def test_format_timestamp_in_header(self):
        """Test that header includes formatted timestamp."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        fixed_time = 1704067200  # 2024-01-01 00:00:00 UTC

        with patch("time.strftime", return_value="2024-01-01 00:00"):
            result = memory_file.format_as_markdown(include_header=True)

        assert "*Last updated: 2024-01-01 00:00*" in result

    @patch("time.strftime")
    def test_format_header_structure(self, mock_strftime):
        """Test the complete header structure."""
        mock_strftime.return_value = "2024-01-01 00:00"
        memory_file = MemoryFile(scope=MemoryScope.USER)

        result = memory_file.format_as_markdown(include_header=True)

        expected_parts = [
            "# User Memory",
            "",
            "*Last updated: 2024-01-01 00:00*",
            ""
        ]
        for part in expected_parts:
            assert part in result

    def test_format_output_is_string(self):
        """Test that the output is always a string."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        result = memory_file.format_as_markdown()

        assert isinstance(result, str)

    def test_format_with_very_long_content(self):
        """Test formatting with very long content."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        long_content = "A" * 10000
        entry = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content=long_content
        )
        memory_file.add_entry(entry)

        result = memory_file.format_as_markdown(include_header=False)

        assert long_content in result
        assert len(result) > 10000

    def test_format_combined_entries_with_and_without_tags(self):
        """Test formatting mix of entries with and without tags."""
        memory_file = MemoryFile(scope=MemoryScope.USER)
        entry_without_tags = MemoryEntry(
            id="test-1",
            scope=MemoryScope.USER,
            category="general",
            content="No tags entry"
        )
        entry_with_tags = MemoryEntry(
            id="test-2",
            scope=MemoryScope.USER,
            category="general",
            content="With tags entry",
            tags=["tag1"]
        )
        memory_file.add_entry(entry_without_tags)
        memory_file.add_entry(entry_with_tags)

        result = memory_file.format_as_markdown(include_header=False)

        assert "- No tags entry" in result
        assert "- With tags entry `tag1`" in result
