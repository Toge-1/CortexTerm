import json

from cortexterm.anthropic_adapter import AnthropicModelAdapter
from cortexterm.tooling import ToolDefinition, ToolRegistry


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name="read_file",
                description="Read file",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _input, _context: None,
            )
        ]
    )


def test_anthropic_adapter_parses_tool_use(monkeypatch) -> None:
    payload = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "<progress>thinking</progress>"},
            {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "README.md"}},
        ],
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=60: DummyResponse(payload))
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    step = adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "read me"}])

    assert step.type == "tool_calls"
    assert step.content == "thinking"
    assert step.contentKind == "progress"
    assert step.calls[0]["toolName"] == "read_file"


def test_anthropic_adapter_parses_final_text(monkeypatch) -> None:
    payload = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "<final>done</final>"}],
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=60: DummyResponse(payload))
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    step = adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "finish"}])

    assert step.type == "assistant"
    assert step.content == "done"
    assert step.kind == "final"


def test_anthropic_adapter_sends_compaction_summary_as_first_user_turn(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_urlopen(request, timeout=60):
        del timeout
        requests.append(json.loads(request.data.decode("utf-8")))
        return DummyResponse(
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "<final>done</final>"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    adapter.next(
        [
            {"role": "system", "content": "base system prompt"},
            {
                "role": "system",
                "content": "summary of the earlier conversation",
                "isCompactionSummary": True,
            },
            {
                "role": "assistant_tool_call",
                "toolUseId": "tool-1",
                "toolName": "read_file",
                "input": {"path": "README.md"},
            },
            {
                "role": "tool_result",
                "toolUseId": "tool-1",
                "toolName": "read_file",
                "content": "file contents",
                "isError": False,
            },
        ]
    )

    body = requests[0]
    assert body["system"] == "base system prompt"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant", "user"]
    assert "<summary>\nsummary of the earlier conversation\n</summary>" in body["messages"][0]["content"][0]["text"]
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_adapter_merges_compaction_summary_with_kept_user_turn(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_urlopen(request, timeout=60):
        del timeout
        requests.append(json.loads(request.data.decode("utf-8")))
        return DummyResponse(
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "<final>done</final>"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    adapter.next(
        [
            {"role": "system", "content": "base system prompt"},
            {
                "role": "system",
                "content": "summary of the earlier conversation",
                "isCompactionSummary": True,
            },
            {"role": "user", "content": "kept user request"},
        ]
    )

    body = requests[0]
    assert [message["role"] for message in body["messages"]] == ["user"]
    assert [block["text"] for block in body["messages"][0]["content"]] == [
        "The conversation history before this point was compacted into the "
        "following summary:\n\n<summary>\nsummary of the earlier conversation\n</summary>",
        "kept user request",
    ]


def test_anthropic_adapter_summary_request_has_no_tools(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_urlopen(request, timeout=60):
        del timeout
        requests.append(json.loads(request.data.decode("utf-8")))
        return DummyResponse(
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "summary"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    adapter = AnthropicModelAdapter(
        {
            "model": "claude",
            "baseUrl": "https://api.anthropic.com",
            "authToken": "x",
            "maxOutputTokens": 2048,
        },
        _tool_registry(),
    )

    result = adapter.summarize("summarize this", max_tokens=4096)

    assert result == "summary"
    assert "tools" not in requests[0]
    assert requests[0]["max_tokens"] == 2048

