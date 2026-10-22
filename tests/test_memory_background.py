from __future__ import annotations

import json

import pytest

from cortexterm.agent_loop import run_agent_turn
from cortexterm.memory_v2.model import MemoryModel
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.memory_v2.store import make_record
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult
from cortexterm.types import AgentStep


@pytest.fixture
def memory(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return MemoryService(root, tmp_path / "memory.sqlite3")


class Extractor:
    def __init__(self, scope="local", statement="pytest interpreter convention", action="ADD", targets=None):
        self.scope, self.statement, self.action, self.targets = scope, statement, action, targets or []

    def extract(self, turn, existing):
        return {"memories": [{"statement": self.statement, "scope": self.scope,
                              "evidence_ids": list(turn["source_fragments"])[-1:]}]}

    def consolidate(self, candidates, existing, comparisons):
        return {"decisions": [{"candidate_id": c["id"], "action": self.action, "targets": self.targets}
                              for c in candidates]}


def capture(memory, identity="turn", text="pytest interpreter convention", session=""):
    recorder = TurnRecorder(text, session=session, turn_id=identity)
    memory.capture(recorder)
    return recorder


def test_model_selects_fragments_and_program_supplies_exact_original_quotes(memory):
    text = "unrelated logs\n" * 210 + "\nUse pytest with this interpreter — 中文标点！\n" * 10
    recorder = capture(memory, text=text)
    old = make_record(project=memory.project, workspace=memory.workspace, statement="old pytest interpreter")
    memory.store.apply([old])
    model = MemoryModel({"model": "test", "baseUrl": "https://api.anthropic.com"})
    selected = {}

    def request(body):
        data = json.loads(body["messages"][0]["content"])
        if "events" in data:
            selected.update(data["events"][0]["fragments"][-1])
            result = {"memories": [{"statement": "Use pytest with this interpreter", "scope": "local",
                                    "evidence_ids": [selected["id"]]}]}
        else:
            assert data["evidence"][0]["quote"] == selected["text"]
            assert len(data["evidence"][0]["quote"]) <= 1000
            assert data["candidates"][0]["evidence_ids"] == [selected["id"]]
            result = {"decisions": [{"candidate_id": data["candidates"][0]["id"], "action": "ADD", "targets": []}]}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    model.adapter._request = request
    memory.model = model
    memory.process_one()
    assert len(model.calls) == 2
    new = next(r for r in memory.store.records(memory.project, memory.workspace) if r["origin"] == "extracted")
    assert "quote" not in new["evidence"][0]  # Store offsets, not another copy of original text.
    assert new["source_ids"] == [recorder.events[0]["id"]]
    assert memory.read(new["id"])["evidence"][0]["quote"] == selected["text"]
    assert memory.read(selected["id"])["quote"] == selected["text"]
    assert memory.read(new["source_ids"][0])["content"] == text


@pytest.mark.parametrize("scope", ["local", "project", "user"])
def test_scope_selection_visibility_and_exact_source_access(memory, tmp_path, scope):
    recorder = TurnRecorder("shared convention", turn_id="source")
    recorder.append({"role": "assistant", "content": "unrelated private response"})
    memory.capture(recorder)

    class Scoped(Extractor):
        def extract(self, turn, existing):
            return {"memories": [{"statement": "shared convention", "scope": scope,
                                  "evidence_ids": [next(iter(turn["source_fragments"]))]}]}

    memory.model = Scoped()
    memory.process_one()
    record = memory.store.records(memory.project, memory.workspace)[0]
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = MemoryService(other_dir, memory.store.path)
    other.project = "different-repository"
    assert bool(other.read(record["id"])) is (scope == "user")
    other.project = memory.project  # Another checkout of the same repository.
    assert bool(other.read(record["id"])) is (scope != "local")
    if scope != "local":
        assert other.read("source:0")["content"] == "shared convention"
        assert other.read("source:1") is None
        assert other.read("source") is None
    else:
        assert other.read("source:0") is None


@pytest.mark.parametrize("scope", ["project", "user"])
def test_narrower_identical_fact_cannot_suppress_broader_fact(memory, scope):
    old = make_record(project=memory.project, workspace=memory.workspace, statement="pytest interpreter convention")
    memory.store.apply([old])
    capture(memory)
    memory.model = Extractor(scope=scope)
    memory.process_one()
    assert {r["scope"] for r in memory.store.records(memory.project, memory.workspace)} == {"local", scope}


@pytest.mark.parametrize("scope", ["project", "user"])
def test_background_can_correct_old_remember_record_within_same_scope(memory, scope):
    old = make_record(project=memory.project if scope == "project" else "", workspace="", scope=scope,
                      statement="old pytest interpreter convention", origin="explicit")
    memory.store.apply([old])
    capture(memory, text="Replace the old pytest interpreter convention")
    memory.model = Extractor(scope=scope, action="SUPERSEDE", targets=[old["id"]])
    memory.process_one()
    assert memory.read(old["id"])["status"] == "superseded"
    assert '"action": "SUPERSEDE"' in memory.feedback()


def test_old_checkpoint_quotation_is_ignored_and_original_is_read(memory):
    capture(memory, text="actual original text")
    turn = memory.store.claim(memory.project, memory.workspace)
    memory.store.save_candidates(turn["id"], {"memories": [{"statement": "derived observation",
        "evidence": [{"event_id": "turn:0", "quote": "copied incorrectly"}]}]}, turn["attempts"])
    memory.store.defer(turn["id"], turn["attempts"])
    memory.model = Extractor()
    memory.process_one()
    record = memory.store.records(memory.project, memory.workspace)[0]
    assert memory.read(record["id"])["evidence"][0]["quote"] == "actual original text"


def test_agent_receives_committed_feedback_without_recording_it_as_conversation(memory):
    capture(memory, session="chat")
    snapshots = []

    class Model:
        def next(self, messages):
            status = next(m["content"] for m in messages if m.get("isRuntimeMemoryContext"))
            snapshots.append(status)
            if len(snapshots) == 1:
                assert '"state": "pending"' in status
                memory.model = Extractor()
                memory.process_one()
                memory.model = None
                return AgentStep(type="tool_calls", calls=[{"id": "A", "toolName": "echo", "input": {}}])
            assert '"state": "done"' in status and '"action": "ADD"' in status
            return AgentStep(type="assistant", kind="final", content="done")

    tools = ToolRegistry([ToolDefinition(name="echo", description="", input_schema={}, validator=lambda i: i,
                                       run=lambda i, c: ToolResult(ok=True, output="ok"))])
    result = run_agent_turn(model=Model(), tools=tools, messages=[{"role": "user", "content": "next task"}],
                            cwd=str(memory.root), memory_service=memory, memory_session="chat")
    assert len(snapshots) == 2
    assert not any(m.get("isRuntimeMemoryContext") for m in result)
    assert all(e["role"] != "system" for t in memory.store.turns(memory.project, memory.workspace) for e in t["events"])
    assert memory.feedback("another-chat") == ""


def test_duplicate_and_failure_feedback_do_not_claim_new_memory_was_saved(memory):
    old = make_record(project=memory.project, workspace=memory.workspace, statement="pytest interpreter convention")
    memory.store.apply([old])
    capture(memory)
    memory.model = Extractor()
    memory.process_one()
    outcome = memory.store.recent_outcomes(memory.project, memory.workspace, "")[0]["outcomes"][0]
    assert outcome["action"] == "NOOP" and outcome["memory_id"] is None and outcome["targets"] == [old["id"]]
    memory.model = None
    capture(memory, identity="bad")
    memory.model = Extractor(scope="invalid")
    memory.process_one()
    status = memory.store.recent_outcomes(memory.project, memory.workspace, "")[0]
    assert status["state"] == "failed" and status["outcomes"] == []
