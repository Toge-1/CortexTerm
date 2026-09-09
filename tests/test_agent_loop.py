from cortexterm.agent_loop import run_agent_turn
from cortexterm.context_manager import ContextManager
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult
from cortexterm.types import AgentStep, ChatMessage, ModelAdapter, StepDiagnostics


class ScriptedModel(ModelAdapter):
    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = steps
        self.calls = 0
        self.summary_prompts: list[str] = []

    def summarize(self, prompt: str, *, max_tokens: int) -> str:
        self.summary_prompts.append(prompt)
        return "## Goal\nKeep working.\n\n## Critical Context\n- Earlier messages summarized."

    def next(self, messages: list[ChatMessage]) -> AgentStep:
        step = self._steps[self.calls]
        self.calls += 1
        return step


def test_agent_turn_executes_tool_and_returns_assistant() -> None:
    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "1", "toolName": "echo", "input": {"text": "hi"}}],
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any(message["role"] == "tool_result" for message in messages)


def test_agent_turn_emits_callbacks() -> None:
    events: list[tuple[str, str]] = []

    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(type="tool_calls", content="working", contentKind="progress", calls=[{"id": "1", "toolName": "echo", "input": {"text": "hi"}}]),
            AgentStep(type="assistant", content="done"),
        ]
    )

    run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        on_tool_start=lambda name, _input: events.append(("start", name)),
        on_tool_result=lambda name, _output, _error: events.append(("result", name)),
        on_assistant_message=lambda content: events.append(("assistant", content)),
        on_progress_message=lambda content: events.append(("progress", content)),
    )

    assert ("progress", "working") in events
    assert ("start", "echo") in events
    assert ("result", "echo") in events
    assert ("assistant", "done") in events


def test_agent_turn_emits_context_compaction_events() -> None:
    model = ScriptedModel([AgentStep(type="assistant", content="done")])
    registry = ToolRegistry([])
    ctx = ContextManager(
        model="default",
        context_window=200,
        reserve_tokens=50,
        keep_recent_tokens=60,
    )
    events: list[tuple[str, dict]] = []
    assistant_events: list[str] = []
    messages = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": "x" * 120}
        for _ in range(20)
    ]

    run_agent_turn(
        model=model,
        tools=registry,
        messages=messages,
        cwd=".",
        context_manager=ctx,
        on_context_event=lambda event, payload: events.append((event, payload)),
        on_assistant_message=assistant_events.append,
    )

    assert [event for event, _payload in events] == ["compact_start", "compact_done"]
    assert events[0][1]["before_tokens"] > 0
    assert events[1][1]["after_tokens"] > 0
    assert events[1][1]["strategy"] == "pi"
    assert model.summary_prompts
    assert assistant_events == ["done"]


def test_agent_turn_compacts_after_tool_result_before_next_model_call() -> None:
    seen_messages: list[list[ChatMessage]] = []

    class RecordingModel(ScriptedModel):
        def next(self, messages: list[ChatMessage]) -> AgentStep:
            seen_messages.append(list(messages))
            return super().next(messages)

    model = RecordingModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "tool-1", "toolName": "echo", "input": {"text": "x" * 300}}],
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _input, _context: ToolResult(ok=True, output="y" * 400),
            )
        ]
    )
    ctx = ContextManager(
        model="default",
        context_window=300,
        reserve_tokens=80,
        keep_recent_tokens=80,
    )
    messages: list[ChatMessage] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old" * 100},
        {"role": "assistant", "content": "old answer" * 30},
        {"role": "user", "content": "run echo"},
    ]

    run_agent_turn(
        model=model,
        tools=registry,
        messages=messages,
        cwd=".",
        context_manager=ctx,
    )

    assert len(seen_messages) == 2
    assert not any(message.get("isCompactionSummary") for message in seen_messages[0])
    assert any(message.get("isCompactionSummary") for message in seen_messages[1])
    assert any(message.get("role") == "tool_result" for message in seen_messages[1])


def test_failed_compaction_preserves_original_context() -> None:
    original: list[ChatMessage] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old " * 300},
        {"role": "assistant", "content": "answer " * 200},
        {"role": "user", "content": "latest"},
    ]
    seen: list[ChatMessage] = []
    events: list[str] = []

    class FailingSummaryModel(ScriptedModel):
        def summarize(self, prompt: str, *, max_tokens: int) -> str:
            del prompt, max_tokens
            raise RuntimeError("summary unavailable")

        def next(self, messages: list[ChatMessage]) -> AgentStep:
            seen.extend(messages)
            return super().next(messages)

    model = FailingSummaryModel([AgentStep(type="assistant", content="done")])
    ctx = ContextManager(
        context_window=300,
        reserve_tokens=80,
        keep_recent_tokens=40,
    )

    result = run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=original,
        cwd=".",
        context_manager=ctx,
        on_context_event=lambda event, _payload: events.append(event),
    )

    assert seen == original
    assert events == ["compact_start", "compact_failed"]
    assert result[: len(original)] == original


def test_agent_turn_retries_empty_response_then_continues() -> None:
    model = ScriptedModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry([])

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any(
        message["role"] == "user" and "last response was empty" in message["content"]
        for message in messages
    )


def test_agent_turn_handles_recoverable_pause_turn() -> None:
    model = ScriptedModel(
        [
            AgentStep(
                type="assistant",
                content="",
                diagnostics=StepDiagnostics(stopReason="pause_turn", ignoredBlockTypes=["thinking"]),
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry([])
    progress_events: list[str] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        on_progress_message=progress_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any("pause_turn" in event for event in progress_events)


def test_agent_turn_returns_fallback_after_repeated_empty_responses() -> None:
    model = ScriptedModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
        ]
    )
    registry = ToolRegistry([])

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert "empty response" in messages[-1]["content"].lower()


def test_tool_registry_dispose_calls_disposer() -> None:
    disposed: list[bool] = []
    registry = ToolRegistry([], disposer=lambda: disposed.append(True))

    registry.dispose()

    assert disposed == [True]
