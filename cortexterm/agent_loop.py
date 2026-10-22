from __future__ import annotations

from typing import Any, Callable

from cortexterm.context_manager import CompactionBoundaryError, ContextManager, estimate_message_tokens
from cortexterm.conversation_history import get_archived_messages
from cortexterm.logging_config import get_logger
from cortexterm.permissions import PermissionManager
from cortexterm.prompt import build_runtime_time_context
from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.types import AgentStep, ChatMessage, ModelAdapter

logger = get_logger("agent_loop")

MAX_COMPACTION_ATTEMPTS = 3
_RUNTIME_TIME_CONTEXT = "isRuntimeTimeContext"
_RUNTIME_MEMORY_CONTEXT = "isRuntimeMemoryContext"

# 常量：避免重复的提示文本
NUDGE_CONTINUE = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

NUDGE_AFTER_TOOL_RESULT = (
    "Continue from your progress update. You have already used tools in this turn, "
    "so treat plain status text as progress, not a final answer. Respond with the "
    "next concrete tool call, code change, or an explicit <final> answer only if "
    "the task is truly complete."
)

NUDGE_AFTER_EMPTY_RESPONSE = (
    "Your last response was empty after recent tool results. Continue immediately "
    "by trying the next concrete step, adapting to any tool errors, or giving an "
    "explicit <final> answer only if the task is complete."
)

NUDGE_AFTER_EMPTY_NO_TOOLS = (
    "Your last response was empty. Continue immediately with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

RESUME_AFTER_PAUSE = (
    "Resume from the previous pause and continue immediately with the next concrete "
    "tool call, code change, or an explicit <final> answer only if the task is complete."
)

RESUME_AFTER_MAX_TOKENS = (
    "Your previous response hit max_tokens during thinking before producing the next "
    "actionable step. Resume immediately and continue with the next concrete tool call, "
    "code change, or an explicit <final> answer only if the task is complete."
)


def _is_empty_assistant_response(content: str) -> bool:
    return len(content.strip()) == 0


def _format_diagnostics(stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None) -> str:
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(*, is_empty: bool, stop_reason: str | None, ignored_block_types: list[str] | None) -> bool:
    if not is_empty:
        return False
    if stop_reason not in {"pause_turn", "max_tokens"}:
        return False
    return "thinking" in (ignored_block_types or [])


def _should_treat_assistant_as_progress(*, kind: str | None, content: str, saw_tool_result: bool) -> bool:
    if kind == "progress":
        return True
    if kind == "final":
        return False
    if not saw_tool_result:
        return False
    return False


def _without_runtime_time_context(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [message for message in messages
            if not message.get(_RUNTIME_TIME_CONTEXT) and not message.get(_RUNTIME_MEMORY_CONTEXT)]


def _refresh_runtime_memory_context(messages: list[ChatMessage], service, recorder) -> None:
    messages[:] = [m for m in messages if not m.get(_RUNTIME_MEMORY_CONTEXT)]
    feedback = getattr(service, "feedback", None)
    if feedback is None:
        return
    try:
        content = feedback(recorder.session if recorder else "")
    except Exception as error:
        logger.warning("Unable to read memory status (%s)", type(error).__name__)
        return
    if content:
        messages.insert(0, {"role": "system", "content": content, _RUNTIME_MEMORY_CONTEXT: True})


def _refresh_runtime_time_context(messages: list[ChatMessage]) -> None:
    context = {"role": "system", "content": build_runtime_time_context(),
               _RUNTIME_TIME_CONTEXT: True}
    for index, message in enumerate(messages):
        if message.get(_RUNTIME_TIME_CONTEXT):
            messages[index] = context
            return
    messages.insert(0, context)


def run_agent_turn(*, memory_service=None, memory_session: str = "", **kwargs) -> list[ChatMessage]:
    """Run a turn; runtime clock context never becomes saved conversation evidence."""
    recorder = None
    if memory_service is not None:
        from cortexterm.memory_v2.service import TurnRecorder
        request = next((m.get("content", "") for m in reversed(kwargs["messages"])
                        if m.get("role") == "user" and not m.get("isCompactionSummary")), "")
        recorder = TurnRecorder(str(request), memory_session)
    try:
        result = _run_agent_turn_impl(
            **kwargs, memory_service=memory_service, memory_recorder=recorder,
        )
        return _without_runtime_time_context(result)
    finally:
        context_manager = kwargs.get("context_manager")
        if context_manager is not None:
            context_manager.messages = _without_runtime_time_context(context_manager.messages)
        if recorder is not None:
            try:
                memory_service.capture(recorder)
            except Exception as error:
                logger.warning("Unable to persist memory evidence (%s)", type(error).__name__)


def _run_agent_turn_impl(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    max_steps: int = 50,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    on_context_event: Callable[[str, dict[str, Any]], None] | None = None,
    context_manager: ContextManager | None = None,
    memory_service=None,
    memory_recorder=None,
) -> list[ChatMessage]:
    from cortexterm.memory_v2.service import RecordedMessages
    current_messages = RecordedMessages(messages, memory_recorder) if memory_recorder else list(messages)
    saw_tool_result = False
    empty_response_retry_count = 0
    recoverable_thinking_retry_count = 0
    tool_error_count = 0
    step = 0
    def _compact_if_needed() -> str | None:
        """Return a pause reason when the estimated request budget is unsafe."""
        nonlocal current_messages
        if context_manager is None:
            return None
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info(
            "Context: %d tokens (%.0f%%), %d messages",
            stats.total_tokens,
            stats.usage_percentage,
            stats.messages_count,
        )
        budget = context_manager.input_token_budget
        if stats.total_tokens <= budget and not stats.should_compact:
            return None
        if budget <= 0:
            return "reserveTokens leaves no input budget; check the context window and reserve setting."
        if not context_manager.enabled:
            return f"Automatic compaction is disabled ({stats.total_tokens:,} estimated tokens; budget {budget:,})."

        logger.warning(
            "Context near limit, auto-compacting with %s strategy...",
            context_manager.strategy,
        )
        start_payload = {
            "before_tokens": stats.total_tokens,
            "context_window": stats.context_window,
            "usage_percentage": stats.usage_percentage,
            "messages_count": stats.messages_count,
            "strategy": context_manager.strategy,
        }
        if on_context_event:
            on_context_event("compact_start", start_payload)
        for attempt in range(1, MAX_COMPACTION_ATTEMPTS + 1):
            history_length = len(context_manager.compaction_history)
            try:
                max_summary_tokens = max(1, int(context_manager.reserve_tokens * 0.8))
                candidate = context_manager.compact_messages(
                    lambda prompt: model.summarize(prompt, max_tokens=max_summary_tokens)
                )
                after_stats = context_manager.get_stats()
                if after_stats.total_tokens > budget:
                    raise RuntimeError(
                        f"Context remains above the input budget "
                        f"({after_stats.total_tokens:,} > {budget:,} estimated tokens)."
                    )
                current_messages = RecordedMessages(candidate, memory_recorder) if memory_recorder else candidate
                break
            except Exception as error:
                # Pi commits only validated checkpoints. Restore the original
                # list as well for legacy/custom compaction implementations.
                context_manager.messages = current_messages
                del context_manager.compaction_history[history_length:]
                logger.warning("Context compaction attempt %d failed: %s", attempt, error)
                retry = attempt < MAX_COMPACTION_ATTEMPTS and not isinstance(error, CompactionBoundaryError)
                if on_context_event:
                    on_context_event(
                        "compact_retry" if retry else "compact_failed",
                        {**start_payload, "error": str(error), "attempt": attempt,
                         "max_attempts": MAX_COMPACTION_ATTEMPTS},
                    )
                if not retry:
                    return f"Context compaction failed after {attempt} attempt(s): {error}"

        after_stats = context_manager.get_stats()
        payload: dict[str, Any] = {
            "before_tokens": stats.total_tokens,
            "after_tokens": after_stats.total_tokens,
            "context_window": after_stats.context_window,
            "usage_percentage": after_stats.usage_percentage,
            "messages_before": stats.messages_count,
            "messages_after": after_stats.messages_count,
            "summary": context_manager.get_context_summary(),
            "strategy": context_manager.strategy,
        }
        if context_manager.compaction_history:
            payload.update(context_manager.compaction_history[-1])
        if on_context_event:
            on_context_event("compact_done", payload)
        elif on_progress_message:
            on_progress_message(str(payload["summary"]))
        return None

    while max_steps is None or step < max_steps:
        # Observe committed background work without blocking for extraction.
        # Refresh before compaction so this bounded status is included in the budget.
        _refresh_runtime_memory_context(current_messages, memory_service, memory_recorder)
        # Replace one transient system message, counted in the request budget.
        _refresh_runtime_time_context(current_messages)
        # This also runs after tool results are appended, before the next model call.
        pause_reason = _compact_if_needed()
        if pause_reason is not None:
            fallback = (
                f"Turn paused before the next model request. {pause_reason} "
                "The original conversation was preserved; no fallback deletion was used. "
                "Check /context and the summary service or compaction settings, then retry."
            )
            if on_context_event:
                on_context_event("context_blocked", {"error": pause_reason})
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        step += 1
        next_step: AgentStep
        try:
            # Compaction may have taken time. The timestamp has fixed length, so
            # refreshing it here does not change the already-checked budget.
            _refresh_runtime_time_context(current_messages)
            next_step = model.next(current_messages)
        except KeyboardInterrupt:
            raise  # Let Ctrl-C propagate
        except ConnectionError as error:
            fallback = f"Network error (connection failed or dropped): {error}"
            logger.error("Model API connection error: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        except TimeoutError as error:
            fallback = f"Model API timeout: {error}"
            logger.error("Model API timeout: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        except Exception as error:
            # Catch-all for unexpected errors (rate limit, auth, server 5xx, etc.)
            error_type = type(error).__name__
            fallback = f"Model API error ({error_type}): {error}"
            logger.error("Model API error (%s): %s", error_type, error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        if next_step.type == "assistant":
            is_empty = _is_empty_assistant_response(next_step.content)
            if not is_empty and _should_treat_assistant_as_progress(
                kind=getattr(next_step, 'kind', None),
                content=next_step.content,
                saw_tool_result=saw_tool_result,
            ):
                if on_progress_message:
                    on_progress_message(next_step.content)
                current_messages.append({"role": "assistant_progress", "content": next_step.content})
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            NUDGE_AFTER_TOOL_RESULT
                            if saw_tool_result and getattr(next_step, 'kind', None) != "progress"
                            else NUDGE_CONTINUE
                        ),
                    }
                )
                continue

            diagnostics = next_step.diagnostics

            if _is_recoverable_thinking_stop(
                is_empty=is_empty,
                stop_reason=diagnostics.stopReason if diagnostics else None,
                ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
            ) and recoverable_thinking_retry_count < 3:
                recoverable_thinking_retry_count += 1
                stop_reason = diagnostics.stopReason if diagnostics else None
                progress_content = (
                    "Model hit max_tokens during thinking; requesting the next step."
                    if stop_reason == "max_tokens"
                    else "Model returned pause_turn; requesting the next step."
                )
                if on_progress_message:
                    on_progress_message(progress_content)
                current_messages.append({"role": "assistant_progress", "content": progress_content})
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            RESUME_AFTER_PAUSE
                            if stop_reason == "pause_turn"
                            else RESUME_AFTER_MAX_TOKENS
                        ),
                    }
                )
                continue

            if is_empty and empty_response_retry_count < 2:
                empty_response_retry_count += 1
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            NUDGE_AFTER_EMPTY_RESPONSE
                            if saw_tool_result
                            else NUDGE_AFTER_EMPTY_NO_TOOLS
                        ),
                    }
                )
                continue

            if is_empty:
                diagnostics_suffix = _format_diagnostics(
                    diagnostics.stopReason if diagnostics else None,
                    diagnostics.blockTypes if diagnostics else None,
                    diagnostics.ignoredBlockTypes if diagnostics else None,
                )
                if saw_tool_result:
                    fallback = (
                        f"Model returned an empty response after tool execution and the turn was stopped. There were {tool_error_count} tool error(s); retry, adjust the command, or choose a different approach.{diagnostics_suffix}"
                        if tool_error_count > 0
                        else f"Model returned an empty response after tool execution and the turn was stopped. Retry or ask the model to continue the remaining steps.{diagnostics_suffix}"
                    )
                else:
                    fallback = f"Model returned an empty response and the turn was stopped.{diagnostics_suffix}"
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                return current_messages

            if on_assistant_message:
                on_assistant_message(next_step.content)
            current_messages.append({"role": "assistant", "content": next_step.content})
            return current_messages

        if next_step.content:
            role = "assistant_progress" if next_step.contentKind == "progress" else "assistant"
            if role == "assistant_progress":
                if on_progress_message:
                    on_progress_message(next_step.content)
                current_messages.append({"role": role, "content": next_step.content})
                current_messages.append(
                    {
                        "role": "user",
                        "content": NUDGE_CONTINUE,
                    }
                )
            else:
                if on_assistant_message:
                    on_assistant_message(next_step.content)
                current_messages.append({"role": role, "content": next_step.content})

        if not next_step.calls and next_step.content and next_step.contentKind != "progress":
            return current_messages

        for call_index, call in enumerate(next_step.calls):
            current_messages.append(
                {
                    "role": "assistant_tool_call",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "input": call["input"],
                    **(
                        {"thinkingBlocks": next_step.thinkingBlocks}
                        if call_index == 0 and next_step.thinkingBlocks
                        else {}
                    ),
                }
            )

        await_user_messages: list[str] = []
        for call in next_step.calls:
            if on_tool_start:
                on_tool_start(call["toolName"], call["input"])
            result = tools.execute(
                call["toolName"],
                call["input"],
                ToolContext(
                    cwd=cwd,
                    permissions=permissions,
                    archived_messages=get_archived_messages(current_messages),
                    memory_service=memory_service,
                ),
            )
            if on_tool_result:
                on_tool_result(call["toolName"], result.output, not result.ok)
            saw_tool_result = True
            if not result.ok:
                tool_error_count += 1
            current_messages.append(
                {
                    "role": "tool_result",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "content": result.output,
                    "isError": not result.ok,
                }
            )
            if result.awaitUser:
                await_user_messages.append(result.output)

        # A model may emit ask_user alongside other calls in the same assistant
        # response. Those calls were all chosen before any result was available,
        # so finish the batch and preserve every tool-call/result pair before
        # yielding control back to the user.
        if await_user_messages:
            user_prompt = "\n\n".join(await_user_messages)
            if on_assistant_message:
                on_assistant_message(user_prompt)
            current_messages.append({"role": "assistant", "content": user_prompt})
            return current_messages

    fallback = "Reached the maximum tool step limit for this turn."
    if on_assistant_message:
        on_assistant_message(fallback)
    current_messages.append({"role": "assistant", "content": fallback})
    return current_messages
