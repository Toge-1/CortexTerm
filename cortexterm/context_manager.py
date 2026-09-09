"""Context window management for LLM conversations.

Tracks token usage, estimates context window consumption, and provides
auto-compaction to prevent context overflow in long conversations.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from cortexterm.config import CORTEXTERM_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default context window sizes (tokens)
DEFAULT_CONTEXT_WINDOWS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-20240307": 100_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "default": 128_000,  # Fallback
}

# Auto-compaction threshold (95% of context window)
AUTOCOMPACT_THRESHOLD = 0.95

# Pi-compatible defaults. The trigger leaves room for the next model response,
# while the retained budget controls how much recent work stays verbatim.
DEFAULT_RESERVE_TOKENS = 16_384
DEFAULT_KEEP_RECENT_TOKENS = 20_000

# Estimated tokens per character (rough average for English/Code)
CHARS_PER_TOKEN = 4.0

# Minimum messages to keep after compaction
MIN_MESSAGES_TO_KEEP = 10

# System prompt is always kept (counts as 1 message)
SYSTEM_PROMPT_RESERVED = 1

SUMMARY_FORMAT = """Create a structured context checkpoint summary that another coding agent will use to continue the work.

Use this EXACT format:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
- [Requirements mentioned by the user, or "(none)"]

## Progress
### Done
- [x] [Completed work]

### In Progress
- [ ] [Current work]

### Blocked
- [Blocking issues, or "(none)"]

## Key Decisions
- **[Decision]**: [Reason]

## Next Steps
1. [What should happen next]

## Critical Context
- [Concrete facts, paths, commands, errors, and values needed to continue]

Be concise, but preserve exact file paths, function names, commands, decisions, and unresolved errors."""

UPDATE_SUMMARY_FORMAT = """Update the previous checkpoint with the new conversation. Preserve useful facts from the previous checkpoint, add the new progress, and remove items that are no longer relevant.

""" + SUMMARY_FORMAT

TURN_PREFIX_FORMAT = """This is the prefix of one unusually large turn. Its recent suffix will remain verbatim.

Summarize only what is needed to understand that retained suffix using this format:

## Original Request
[What the user asked for]

## Early Progress
- [Decisions and work completed in this prefix]

## Context for Suffix
- [Facts needed to understand the retained recent messages]

Be concise and preserve exact paths, commands, and errors."""


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

# 预编译的正则表达式用于快速 CJK 字符检测
import re
_CJK_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]')


def estimate_tokens(text: str) -> int:
    """改进的 token 估算，支持中英文
    
    - 英文/代码：约 4 字符/token
    - 中文/日文：约 1.5 字符/token
    - 混合文本：使用启发式估算
    
    性能优化：使用正则表达式替代逐字符 ord() 检查，速度快 10-50 倍
    """
    if not text:
        return 0
    
    # 使用正则表达式快速统计 CJK 字符数量
    cjk_count = len(_CJK_PATTERN.findall(text))
    
    # CJK 字符约 1.5 字符/token，英文约 4 字符/token
    ascii_chars = len(text) - cjk_count
    
    return max(1, int(cjk_count / 1.5 + ascii_chars / 4.0))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate tokens for a single message."""
    tokens = 0
    
    # Role overhead
    role = message.get("role", "")
    if role == "system":
        tokens += 3  # System prompt overhead
    elif role == "user":
        tokens += 4  # User message overhead
    elif role == "assistant":
        tokens += 3  # Assistant overhead
    elif role == "assistant_tool_call":
        tokens += 7  # Tool call overhead
    elif role == "tool_result":
        tokens += 6  # Tool result overhead
    elif role == "assistant_progress":
        tokens += 3
    
    # Content tokens
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    
    # Tool call input/output
    if "input" in message:
        input_str = json.dumps(message["input"]) if isinstance(message["input"], dict) else str(message["input"])
        tokens += estimate_tokens(input_str)
    
    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_message_tokens(msg) for msg in messages)


def _serialize_message(message: dict[str, Any]) -> str:
    """Serialize one message as quoted history rather than a live conversation."""
    role = message.get("role", "unknown")
    content = str(message.get("content", ""))
    if role == "user":
        return f"[User]: {content}"
    if role == "assistant":
        return f"[Assistant]: {content}"
    if role == "assistant_progress":
        return f"[Assistant progress]: {content}"
    if role == "assistant_tool_call":
        tool_input = json.dumps(message.get("input"), ensure_ascii=False, default=str)
        return f"[Assistant tool call]: {message.get('toolName', 'unknown')}({tool_input})"
    if role == "tool_result":
        if len(content) > 2_000:
            omitted = len(content) - 2_000
            content = f"{content[:2_000]}\n...[{omitted} characters truncated]"
        status = "error" if message.get("isError") else "result"
        return f"[Tool {status} {message.get('toolName', 'unknown')}]: {content}"
    return f"[{role}]: {content}"


def serialize_conversation(messages: list[dict[str, Any]]) -> str:
    """Convert messages into text for the standalone summarization request."""
    return "\n\n".join(_serialize_message(message) for message in messages)


def _summary_prompt(
    messages: list[dict[str, Any]],
    *,
    previous_summary: str | None = None,
    turn_prefix: bool = False,
) -> str:
    prompt = (
        f"<conversation>\n{serialize_conversation(messages)}\n</conversation>\n\n"
    )
    if previous_summary:
        prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    if turn_prefix:
        return prompt + TURN_PREFIX_FORMAT
    return prompt + (UPDATE_SUMMARY_FORMAT if previous_summary else SUMMARY_FORMAT)


_READ_FILE_TOOLS = {"read_file"}
_MODIFY_FILE_TOOLS = {
    "write_file",
    "edit_file",
    "modify_file",
    "patch_file",
    "multi_edit",
    "notebook_edit",
}


def _collect_path_values(value: Any) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path", "file_path", "filePath"} and isinstance(item, str) and item.strip():
                paths.add(item.strip())
            else:
                paths.update(_collect_path_values(item))
    elif isinstance(value, list):
        for item in value:
            paths.update(_collect_path_values(item))
    return paths


def _extract_file_operations(messages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    read_files: set[str] = set()
    modified_files: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant_tool_call":
            continue
        tool_name = str(message.get("toolName", ""))
        paths = _collect_path_values(message.get("input"))
        if tool_name in _READ_FILE_TOOLS:
            read_files.update(paths)
        if tool_name in _MODIFY_FILE_TOOLS:
            modified_files.update(paths)
    read_files.difference_update(modified_files)
    return sorted(read_files), sorted(modified_files)


def _format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    sections: list[str] = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append(
            "<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>"
        )
    return "\n\n" + "\n\n".join(sections) if sections else ""


def _is_valid_cut(messages: list[dict[str, Any]], index: int) -> bool:
    """Return whether keeping from index preserves every tool-call/result pair."""
    if index < 0 or index >= len(messages):
        return False
    # Pi only cuts at user-like or assistant events. CortexTerm stores an
    # assistant tool call as its own role, so it is an explicit candidate here.
    # Tool results and any future metadata/event roles are never cut points.
    if messages[index].get("role") not in {
        "user",
        "assistant",
        "assistant_progress",
        "assistant_tool_call",
    }:
        return False

    removed_tool_ids = {
        str(message.get("toolUseId"))
        for message in messages[:index]
        if message.get("role") == "assistant_tool_call"
    }
    kept_result_ids = {
        str(message.get("toolUseId"))
        for message in messages[index:]
        if message.get("role") == "tool_result"
    }
    return removed_tool_ids.isdisjoint(kept_result_ids)


@dataclass
class CompactionPreparation:
    """The old history to summarize and the recent suffix to retain verbatim."""

    base_system_messages: list[dict[str, Any]]
    messages_to_summarize: list[dict[str, Any]]
    turn_prefix_messages: list[dict[str, Any]]
    kept_messages: list[dict[str, Any]]
    previous_summary: str | None
    previously_archived_messages: list[dict[str, Any]]
    is_split_turn: bool


def prepare_pi_compaction(
    messages: list[dict[str, Any]],
    keep_recent_tokens: int,
) -> CompactionPreparation | None:
    """Choose a Pi-style cut point by recent token budget and turn boundaries."""
    base_system_messages = [
        message
        for message in messages
        if message.get("role") == "system" and not message.get("isCompactionSummary")
    ]
    previous_summary_message = next(
        (
            message
            for message in reversed(messages)
            if message.get("role") == "system" and message.get("isCompactionSummary")
        ),
        None,
    )
    previous_summary = (
        str(previous_summary_message.get("content", ""))
        if previous_summary_message
        else None
    )
    previously_archived_messages = (
        list(previous_summary_message.get("compactedMessages", []))
        if previous_summary_message
        and isinstance(previous_summary_message.get("compactedMessages"), list)
        else []
    )
    conversation = [message for message in messages if message.get("role") != "system"]
    if len(conversation) < 2:
        return None

    valid_cuts = [index for index in range(len(conversation)) if _is_valid_cut(conversation, index)]
    if not valid_cuts:
        return None

    accumulated = 0
    threshold_index = len(conversation) - 1
    for index in range(len(conversation) - 1, -1, -1):
        accumulated += estimate_message_tokens(conversation[index])
        threshold_index = index
        if accumulated >= keep_recent_tokens:
            break

    cut_index = next((index for index in valid_cuts if index >= threshold_index), valid_cuts[-1])
    if cut_index == 0:
        return None

    starts_turn = conversation[cut_index].get("role") == "user"
    turn_start = -1
    if not starts_turn:
        for index in range(cut_index - 1, -1, -1):
            if conversation[index].get("role") == "user":
                turn_start = index
                break

    is_split_turn = turn_start >= 0
    history_end = turn_start if is_split_turn else cut_index
    messages_to_summarize = conversation[:history_end]
    turn_prefix_messages = conversation[history_end:cut_index] if is_split_turn else []
    if not messages_to_summarize and not turn_prefix_messages:
        return None

    return CompactionPreparation(
        base_system_messages=base_system_messages,
        messages_to_summarize=messages_to_summarize,
        turn_prefix_messages=turn_prefix_messages,
        kept_messages=conversation[cut_index:],
        previous_summary=previous_summary,
        previously_archived_messages=previously_archived_messages,
        is_split_turn=is_split_turn,
    )


# ---------------------------------------------------------------------------
# Context tracking
# ---------------------------------------------------------------------------

@dataclass
class ContextStats:
    """Current context window statistics."""
    total_tokens: int = 0
    context_window: int = 0
    usage_percentage: float = 0.0
    messages_count: int = 0
    system_tokens: int = 0
    conversation_tokens: int = 0
    tool_calls_count: int = 0
    is_near_limit: bool = False
    should_compact: bool = False


@dataclass
class ContextManager:
    """Manages context window tracking and auto-compaction."""
    model: str = "default"
    context_window: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    strategy: Literal["pi", "legacy"] = "pi"
    enabled: bool = True
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    
    def __post_init__(self):
        if self.strategy not in {"pi", "legacy"}:
            raise ValueError(f"Unknown compaction strategy: {self.strategy}")
        self.reserve_tokens = max(1, int(self.reserve_tokens))
        self.keep_recent_tokens = max(1, int(self.keep_recent_tokens))
        if self.context_window == 0:
            self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
                self.model, DEFAULT_CONTEXT_WINDOWS["default"]
            )
    
    def update_model(self, model: str) -> None:
        """Update model and adjust context window."""
        self.model = model
        self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
            model, DEFAULT_CONTEXT_WINDOWS["default"]
        )
    
    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message and update tracking."""
        self.messages.append(message)
    
    def get_stats(self) -> ContextStats:
        """Calculate current context statistics."""
        if not self.messages:
            return ContextStats(
                context_window=self.context_window,
            )
        
        # Count tokens
        system_tokens = 0
        conversation_tokens = 0
        tool_calls = 0
        
        for msg in self.messages:
            msg_tokens = estimate_message_tokens(msg)
            if msg.get("role") == "system":
                system_tokens += msg_tokens
            else:
                conversation_tokens += msg_tokens
            
            if msg.get("role") == "assistant_tool_call":
                tool_calls += 1
        
        total_tokens = system_tokens + conversation_tokens
        usage_pct = (total_tokens / self.context_window * 100) if self.context_window > 0 else 0
        
        is_near_limit = usage_pct >= 80  # Warning at 80%
        if not self.enabled:
            should_compact = False
        elif self.strategy == "legacy":
            should_compact = usage_pct >= (AUTOCOMPACT_THRESHOLD * 100)
        else:
            should_compact = total_tokens > self.context_window - self.reserve_tokens
        
        return ContextStats(
            total_tokens=total_tokens,
            context_window=self.context_window,
            usage_percentage=usage_pct,
            messages_count=len(self.messages),
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_calls_count=tool_calls,
            is_near_limit=is_near_limit,
            should_compact=should_compact,
        )
    
    def should_auto_compact(self) -> bool:
        """Check if auto-compaction should trigger."""
        stats = self.get_stats()
        return stats.should_compact
    
    def compact_messages(
        self,
        summarizer: Callable[[str], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Compact messages with the configured Pi or legacy strategy."""
        stats = self.get_stats()
        if not stats.should_compact:
            return self.messages

        if self.strategy == "pi":
            if summarizer is None:
                raise RuntimeError("Pi-style compaction requires a model summarizer")
            return self._compact_messages_pi(stats, summarizer)
        return self._compact_messages_legacy(stats)

    def _compact_messages_pi(
        self,
        stats: ContextStats,
        summarizer: Callable[[str], str],
    ) -> list[dict[str, Any]]:
        preparation = prepare_pi_compaction(self.messages, self.keep_recent_tokens)
        if preparation is None:
            raise RuntimeError("No safe Pi-style compaction boundary was found")

        history_summary = preparation.previous_summary or "No prior history."
        if preparation.messages_to_summarize:
            history_summary = summarizer(
                _summary_prompt(
                    preparation.messages_to_summarize,
                    previous_summary=preparation.previous_summary,
                )
            )

        summary = history_summary
        if preparation.turn_prefix_messages:
            turn_summary = summarizer(
                _summary_prompt(preparation.turn_prefix_messages, turn_prefix=True)
            )
            summary = f"{history_summary}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_summary}"

        archived_messages = (
            preparation.previously_archived_messages
            + preparation.messages_to_summarize
            + preparation.turn_prefix_messages
        )
        read_files, modified_files = _extract_file_operations(archived_messages)
        summary += _format_file_operations(read_files, modified_files)

        summary_message = {
            "role": "system",
            "content": summary,
            "isCompactionSummary": True,
            # Kept in the session file for replay/audit; adapters only send content.
            "compactedMessages": archived_messages,
        }
        compacted = (
            preparation.base_system_messages
            + [summary_message]
            + preparation.kept_messages
        )
        after_tokens = estimate_messages_tokens(compacted)
        self.compaction_history.append(
            {
                "timestamp": time.time(),
                "strategy": "pi",
                "before_tokens": stats.total_tokens,
                "after_tokens": after_tokens,
                "messages_removed": stats.messages_count - len(compacted),
                "kept_tokens": estimate_messages_tokens(preparation.kept_messages),
                "archived_messages": len(summary_message["compactedMessages"]),
                "read_files": read_files,
                "modified_files": modified_files,
                "is_split_turn": preparation.is_split_turn,
            }
        )
        self.messages = compacted
        return compacted

    def _compact_messages_legacy(self, stats: ContextStats) -> list[dict[str, Any]]:
        """Preserve the original deletion-based CortexTerm implementation."""
        
        # Calculate target: reduce to ~70% of context window
        target_tokens = int(self.context_window * 0.70)
        
        # Always keep system prompt
        system_messages = [m for m in self.messages if m.get("role") == "system"]
        other_messages = [m for m in self.messages if m.get("role") != "system"]
        
        # Remove old progress messages first
        filtered = [
            m for m in other_messages
            if m.get("role") != "assistant_progress"
        ]
        
        # If still too large, drop oldest messages one at a time.
        # Prefer dropping tool-call/tool-result pairs first, then plain
        # assistant/user messages.  Always keep the most recent messages.
        while estimate_messages_tokens(filtered) > target_tokens and len(filtered) > MIN_MESSAGES_TO_KEEP:
            removed = False
            for i in range(len(filtered) - MIN_MESSAGES_TO_KEEP):
                role = filtered[i].get("role")
                # Drop tool-call + its result as a pair
                if role == "assistant_tool_call":
                    if (i + 1 < len(filtered) and
                            filtered[i + 1].get("role") == "tool_result"):
                        del filtered[i:i + 2]
                    else:
                        del filtered[i]
                    removed = True
                    break
                # Drop standalone tool_result (orphaned)
                if role == "tool_result":
                    del filtered[i]
                    removed = True
                    break
                # Drop plain user/assistant messages
                if role in ("user", "assistant"):
                    del filtered[i]
                    removed = True
                    break

            if not removed:
                break
        
        # Add compaction marker
        compaction_marker = {
            "role": "system",
            "content": (
                f"[Context compacted at {time.strftime('%H:%M:%S')}. "
                f"Previous {stats.messages_count - len(filtered) - len(system_messages)} messages summarized. "
                f"Token usage reduced from {stats.usage_percentage:.0f}% to "
                f"{estimate_messages_tokens(filtered) / self.context_window * 100:.0f}%]"
            ),
        }
        
        # Build final message list
        compacted = system_messages + [compaction_marker] + filtered
        
        # Record compaction
        self.compaction_history.append({
            "timestamp": time.time(),
            "strategy": "legacy",
            "before_tokens": stats.total_tokens,
            "after_tokens": estimate_messages_tokens(compacted),
            "messages_removed": stats.messages_count - len(compacted),
        })
        
        self.messages = compacted
        return compacted
    
    def get_context_summary(self) -> str:
        """Get a human-readable context usage summary."""
        stats = self.get_stats()
        
        if stats.messages_count == 0:
            return "Context: empty"
        
        status = "✓"
        if stats.is_near_limit:
            status = "⚠"
        if stats.should_compact:
            status = "🔴"
        
        return (
            f"Context: {status} {stats.usage_percentage:.0f}% "
            f"({stats.total_tokens:,}/{stats.context_window:,} tokens, "
            f"{stats.messages_count} msgs, {stats.tool_calls_count} tools)"
        )
    
    def format_context_details(self) -> str:
        """Get detailed context information for /context command."""
        stats = self.get_stats()
        
        lines = [
            "Context Window Usage",
            "=" * 50,
            f"Model: {self.model}",
            f"Compaction strategy: {self.strategy}",
            f"Reserve tokens: {self.reserve_tokens:,}",
            f"Keep recent tokens: {self.keep_recent_tokens:,}",
            f"Context window: {stats.context_window:,} tokens",
            "",
            f"Total tokens: {stats.total_tokens:,}",
            f"Usage: {stats.usage_percentage:.1f}%",
            f"Messages: {stats.messages_count}",
            f"Tool calls: {stats.tool_calls_count}",
            "",
        ]
        
        if stats.should_compact:
            lines.append("⚠️  WARNING: Context is near capacity!")
            lines.append("Auto-compaction will trigger soon.")
            lines.append("")
        
        if self.compaction_history:
            lines.append("Compaction History:")
            for comp in self.compaction_history[-3:]:  # Last 3
                ts = time.strftime("%H:%M:%S", time.localtime(comp["timestamp"]))
                lines.append(
                    f"  {ts} [{comp.get('strategy', 'legacy')}]: "
                    f"{comp['messages_removed']} messages removed, "
                    f"{comp['before_tokens']:,} → {comp['after_tokens']:,} tokens"
                )
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_context_state(manager: ContextManager) -> None:
    """Save context manager state to disk."""
    state_path = CORTEXTERM_DIR / "context_state.json"
    CORTEXTERM_DIR.mkdir(parents=True, exist_ok=True)
    
    state = {
        "model": manager.model,
        "context_window": manager.context_window,
        "messages": manager.messages,
        "compaction_history": manager.compaction_history[-10:],  # Keep last 10
        "strategy": manager.strategy,
        "enabled": manager.enabled,
        "reserve_tokens": manager.reserve_tokens,
        "keep_recent_tokens": manager.keep_recent_tokens,
    }
    
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_context_state() -> ContextManager | None:
    """Load context manager state from disk."""
    state_path = CORTEXTERM_DIR / "context_state.json"
    if not state_path.exists():
        return None
    
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return ContextManager(
            model=state.get("model", "default"),
            context_window=state.get("context_window", 0),
            messages=state.get("messages", []),
            compaction_history=state.get("compaction_history", []),
            strategy=state.get("strategy", "pi"),
            enabled=state.get("enabled", True),
            reserve_tokens=state.get("reserve_tokens", DEFAULT_RESERVE_TOKENS),
            keep_recent_tokens=state.get("keep_recent_tokens", DEFAULT_KEEP_RECENT_TOKENS),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def clear_context_state() -> None:
    """Clear saved context state."""
    state_path = CORTEXTERM_DIR / "context_state.json"
    if state_path.exists():
        state_path.unlink()
