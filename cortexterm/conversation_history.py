"""Access to the raw archive embedded in the current conversation checkpoint."""

from __future__ import annotations

from typing import Any


def get_archived_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest checkpoint contains the cumulative, chronological archive.

    Do not combine checkpoints or read session files here: that would duplicate
    evidence or accidentally expose other conversations. Positions are stable
    as Pi compaction appends newly removed messages to this archive.
    """
    for message in reversed(messages):
        if message.get("isCompactionSummary"):
            archive = message.get("compactedMessages")
            return archive if isinstance(archive, list) else []
    return []
