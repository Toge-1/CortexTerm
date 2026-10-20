from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from cortexterm.agent_loop import run_agent_turn
from cortexterm.context_manager import estimate_tokens
from cortexterm.memory_v2.retrieval import time_window, tokenize
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.memory_v2.store import make_record
from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.tools.memory_tools import memory_read_tool
from cortexterm.tools.remember import remember_tool
from cortexterm.types import AgentStep


@pytest.fixture
def memory(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return MemoryService(root, tmp_path / "memory.sqlite3")


def put(service, text, *, scope="local", **kwargs):
    record = make_record(
        project="" if scope == "user" else service.project,
        workspace=service.workspace if scope == "local" else "",
        statement=text, scope=scope, **kwargs,
    )
    service.store.apply([record])
    return record


def test_scope_filtered_before_search_and_direct_read(memory, tmp_path):
    record = put(memory, "tenant-z forbidden launch command")
    other = tmp_path / "other"
    other.mkdir()
    service = MemoryService(other, memory.store.path)
    assert service.search("forbidden")["results"] == []
    assert service.read(record["id"]) is None
    shared = put(memory, "prefer explanations in Chinese", scope="user")
    assert service.read(shared["id"]) is not None


def test_record_and_quoted_evidence_survive_restart(memory):
    recorder = TurnRecorder("昨天修什么", turn_id="stable")
    recorder.append({"role": "tool_result", "content": "test passed", "toolUseId": "tool1",
                     "thinkingBlocks": [{"secret": "not public"}]})
    memory.capture(recorder)
    memory.capture(recorder)
    fresh = MemoryService(memory.root, memory.store.path)
    turns = fresh.store.turns(fresh.project, fresh.workspace)
    assert len(turns) == 1
    assert turns[0]["events"][1]["id"] == "stable:1"
    assert "thinkingBlocks" not in turns[0]["events"][1]
    assert fresh.read("stable")["evidence"][1]["quote"] == "test passed"


def test_temporal_query_does_not_require_semantic_match(memory):
    yesterday = datetime(2026, 10, 19, 21, tzinfo=timezone.utc).timestamp()
    recorder = TurnRecorder("请修复审批竞争导致永久等待", turn_id="night")
    recorder.recorded_at = yesterday
    memory.capture(recorder)
    future = TurnRecorder("明天的秘密", turn_id="future")
    future.recorded_at = yesterday + 86400 * 3
    memory.capture(future)
    result = memory.search("昨天晚上叫你干什么", now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    assert [r["id"] for r in result["results"]] == ["night"]


def test_secret_redaction_and_raw_evidence(memory):
    rec = TurnRecorder("password=hunter2")
    memory.capture(rec)
    text = json.dumps(memory.read(rec.id))
    assert "hunter2" not in text
    assert "REDACTED" in text
    identity = memory.remember("A safe fact", "local", "general", ["password=hunter2"])
    assert "hunter2" not in json.dumps(memory.read(identity))


def test_supersession_atomic_and_history_readable(memory):
    old = put(memory, "test with pytest")
    new = make_record(project=memory.project, workspace=memory.workspace, statement="test with py -m pytest")
    memory.store.apply([new], [{"old_id": old["id"], "new_id": new["id"], "version": 1}])
    assert [r["id"] for r in memory.store.records(memory.project, memory.workspace)] == [new["id"]]
    assert memory.read(old["id"])["status"] == "superseded"
    failed = make_record(project=memory.project, workspace=memory.workspace, statement="lost update")
    with pytest.raises(ValueError):
        memory.store.apply([failed], [{"old_id": old["id"], "new_id": failed["id"], "version": 1}])
    assert memory.read(failed["id"]) is None


def test_cross_scope_supersede_rejected(memory):
    old = put(memory, "project command", scope="project")
    new = make_record(project=memory.project, workspace=memory.workspace, statement="local command")
    with pytest.raises(ValueError):
        memory.store.apply([new], [{"old_id": old["id"], "new_id": new["id"], "version": 1}])
    assert memory.read(old["id"])["status"] == "active"


def test_no_result_and_strict_budget(memory):
    put(memory, "pytest settings " * 2000)
    assert memory.context("zzunrelated999") == ""
    assert estimate_tokens(memory.context("pytest", max_tokens=120)) <= 120
    assert memory.context("pytest", max_tokens=0) == ""


def test_file_change_invalidates_derived_fact(memory):
    import hashlib
    path = memory.root / "config.py"
    path.write_text("old")
    put(memory, "config uses old namespace", file_hashes={"config.py": hashlib.sha256(b"old").hexdigest()})
    assert memory.search("namespace")["results"]
    path.write_text("new")
    assert memory.search("namespace")["results"] == []


def test_concurrent_jobs_claim_once(memory):
    memory.capture(TurnRecorder("work", turn_id="once"))
    barrier = threading.Barrier(2)
    found = []
    def claim():
        barrier.wait()
        found.append(memory.store.claim(memory.project, memory.workspace))
    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(x is not None for x in found) == 1


def test_expired_lease_is_recoverable(memory):
    memory.capture(TurnRecorder("work", turn_id="expired"))
    assert memory.store.claim(memory.project, memory.workspace, lease_seconds=-1)
    assert memory.store.claim(memory.project, memory.workspace)["id"] == "expired"


def test_bad_llm_evidence_does_not_publish(memory):
    class BadExtractor:
        def extract(self, turn, existing):
            return {"memories": [{"statement": "invented", "evidence": [
                {"event_id": turn["events"][0]["id"], "quote": "never appeared"}]}]}
    memory.capture(TurnRecorder("real words"))
    memory.model = BadExtractor()
    assert memory.process_one()
    assert not memory.store.records(memory.project, memory.workspace)
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"failed": 1}


def test_valid_extraction_and_idempotence(memory):
    class Extractor:
        def extract(self, turn, existing):
            return {"memories": [{"statement": "Use py -m pytest", "kind": "procedure",
                                 "keywords": ["测试"], "entities": ["tests"],
                                 "evidence": [{"event_id": turn["events"][0]["id"], "quote": "py -m pytest"}]}]}
    rec = TurnRecorder("Always run py -m pytest")
    memory.capture(rec)
    memory.model = Extractor()
    assert memory.process_one()
    assert not memory.process_one()
    assert len(memory.search("测试")["results"]) == 1
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}


def test_automatic_merge_cannot_override_explicit_memory(memory):
    old = put(memory, "use command one")
    rec = TurnRecorder("use command two")
    turn = {"id": rec.id, "recorded_at": rec.recorded_at, "events": rec.events}
    with pytest.raises(ValueError, match="explicit"):
        memory._validate_proposal(turn, {"memories": [{
            "statement": "use command two", "supersedes": [old["id"]],
            "evidence": [{"event_id": rec.events[0]["id"], "quote": "command two"}],
        }]}, memory.store.records(memory.project, memory.workspace))


def test_agent_records_tool_pairs_and_exception(memory):
    class Model:
        def next(self, messages):
            raise RuntimeError("provider failed")
    messages = run_agent_turn(model=Model(), tools=ToolRegistry([]),
                              messages=[{"role": "user", "content": "keep my question"}],
                              cwd=str(memory.root), memory_service=memory)
    assert "provider failed" in messages[-1]["content"]
    assert memory.store.turns(memory.project, memory.workspace)[0]["request"] == "keep my question"


def test_real_agent_tool_result_capture(memory):
    from cortexterm.tooling import ToolDefinition, ToolResult
    class Model:
        def next(self, messages):
            if messages[-1]["role"] == "tool_result":
                return AgentStep(type="assistant", content="done")
            return AgentStep(type="tool_calls", calls=[{"id": "A", "toolName": "echo", "input": {}}])
    tools = ToolRegistry([ToolDefinition(name="echo", description="", input_schema={},
                                       validator=lambda x: x, run=lambda i, c: ToolResult(ok=True, output="answer"))])
    run_agent_turn(model=Model(), tools=tools, messages=[{"role": "user", "content": "go"}],
                   cwd=str(memory.root), memory_service=memory)
    events = memory.store.turns(memory.project, memory.workspace)[0]["events"]
    assert [e["role"] for e in events] == ["user", "assistant_tool_call", "tool_result", "assistant"]
    assert events[1]["toolUseId"] == events[2]["toolUseId"] == "A"


def test_remember_approval_and_immediate_visibility(memory):
    class Deny:
        def ensure_edit(self, *args):
            raise RuntimeError("denied")
    data = remember_tool.validator({"content": "The widget test command is py -m pytest", "scope": "project"})
    context = ToolContext(cwd=str(memory.root), memory_service=memory, permissions=Deny())
    with pytest.raises(RuntimeError, match="denied"):
        remember_tool.run(data, context)
    assert not memory.search("widget")["results"]
    context.permissions = None
    assert remember_tool.run(data, context).ok
    assert memory.search("widget")["results"]


def test_time_and_multilingual_tokenizer():
    assert "审批" in tokenize("审批失败")
    assert "session" in tokenize("session_lock.py")
    window = time_window("yesterday evening", datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert window[1] - window[0] == 6 * 3600
    assert time_window("2026-99-99", datetime.now().astimezone()) is None


def test_expired_worker_cannot_publish_or_fail_new_owner(memory):
    memory.capture(TurnRecorder("lease ownership", turn_id="leased"))
    old = memory.store.claim(memory.project, memory.workspace, lease_seconds=-1)
    new = memory.store.claim(memory.project, memory.workspace)
    with pytest.raises(ValueError, match="reclaimed"):
        memory.store.apply([], completed_turn="leased", claim_attempt=old["attempts"])
    memory.store.fail("leased", "old worker failed", attempt=old["attempts"])
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"running": 1}
    memory.store.apply([], completed_turn="leased", claim_attempt=new["attempts"])
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}


def test_raw_search_finds_the_tail_of_long_logs(memory):
    rec = TurnRecorder("debug the build")
    rec.append({"role": "tool_result", "toolUseId": "call",
                "content": "unrelated build output\n" * 3000 + "SpecialZebraFailure needs the timezone fixture"})
    memory.capture(rec)
    context = memory.context("SpecialZebraFailure", mode="raw", max_tokens=400)
    assert "SpecialZebraFailure" in context
    assert estimate_tokens(context) <= 400
    identity = memory.search("SpecialZebraFailure", mode="raw")["results"][0]["id"]
    assert "SpecialZebraFailure" in memory.read(identity)["statement"]


def test_explicit_correction_preserves_previous_evidence(memory):
    old = memory.remember("Retry 409 errors", "project", "decision", ["retry"])
    new = memory.remember("Never retry 409 errors", "project", "decision", ["retry"], [old])
    assert memory.read(old)["status"] == "superseded"
    assert memory.read(new)["status"] == "active"
    assert {"source": new, "target": old, "kind": "supersedes"} in memory.read(new)["links"]
    assert old not in [r["id"] for r in memory.search("409")["results"]]


def test_explicit_replacement_cannot_cross_scope(memory):
    old = memory.remember("Use old command", "user", "procedure", ["command"])
    with pytest.raises(ValueError, match="scopes"):
        memory.remember("Use new command", "local", "procedure", ["command"], [old])
    assert memory.read(old)["status"] == "active"


def test_start_resumes_existing_pending_turn(memory):
    class Extractor:
        def extract(self, turn, existing):
            return {"memories": []}
    memory.capture(TurnRecorder("already persisted before restart"))
    resumed = MemoryService(memory.root, memory.store.path, model=Extractor())
    resumed.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and resumed.store.stats(resumed.project, resumed.workspace)["jobs"] != {"done": 1}:
        time.sleep(.01)
    resumed.close()
    assert resumed.store.stats(resumed.project, resumed.workspace)["jobs"] == {"done": 1}


def test_model_budget_leaves_unprocessed_history_pending(memory):
    class AtLimit:
        max_calls = 1
        calls = [{}]
        def extract(self, *args):
            raise AssertionError("no more calls allowed")
    memory.capture(TurnRecorder("work pending"))
    memory.model = AtLimit()
    assert not memory.process_one()
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"pending": 1}


def test_cited_event_id_reads_full_original_evidence(memory):
    rec = TurnRecorder("source question")
    rec.append({"role": "tool_result", "toolUseId": "T", "content": "Original output beyond a short quotation"})
    memory.capture(rec)
    fact = put(memory, "A derived observation", source_ids=[rec.events[1]["id"]])
    metadata = memory.read(fact["id"])
    assert metadata["source_turn_ids"] == [rec.id]
    result = memory_read_tool.run({"id": rec.events[1]["id"], "offset": 0},
                                  ToolContext(cwd=str(memory.root), memory_service=memory))
    event = json.loads(json.loads(result.output)["text"])
    assert event["content"] == rec.events[1]["content"]
    assert event["turn_id"] == rec.id


def test_exhausted_crash_retries_become_visible_failure(memory):
    memory.capture(TurnRecorder("repeatedly interrupted"))
    for _ in range(3):
        assert memory.store.claim(memory.project, memory.workspace, lease_seconds=-1)
    assert memory.store.claim(memory.project, memory.workspace) is None
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"failed": 1}


def test_memory_slash_command_reports_running_service(memory):
    from types import SimpleNamespace
    from cortexterm.cli_commands import try_handle_local_command
    memory.capture(TurnRecorder("a pending turn"))
    text = try_handle_local_command("/memory", memory_mgr=SimpleNamespace(v2=memory))
    assert "Memory V2 / hybrid" in text
    assert "'pending': 1" in text
    assert "Automatic extraction: off" in text


def test_extractor_reasoning_setting_is_provider_scoped():
    from cortexterm.memory_v2.model import MemoryModel
    for host, expected in [("https://api.deepseek.com/anthropic", True),
                           ("https://api.anthropic.com", False)]:
        model = MemoryModel({"model": "test", "baseUrl": host})
        requests = []
        def request(body):
            requests.append(body)
            return {"content": [{"type": "text", "text": '{"memories": []}'}]}
        model.adapter._request = request
        assert model.extract({"events": []}, []) == {"memories": []}
        assert ("thinking" in requests[0]) is expected
        if expected:
            assert requests[0]["thinking"] == {"type": "disabled"}


def test_runtime_prefetch_is_opt_in_while_memory_tools_remain_usable(memory):
    from cortexterm.memory import MemoryManager
    from cortexterm.tools.memory_tools import memory_search_tool
    manager = MemoryManager.__new__(MemoryManager)
    manager.v2 = memory
    memory.remember("Use the workspace lease before loading", "local", "procedure", ["workspace"])
    assert manager.get_relevant_context(query="workspace") == ""
    result = memory_search_tool.run({"query": "workspace", "limit": 5},
                                   ToolContext(cwd=str(memory.root), memory_service=memory))
    assert result.ok and "workspace lease" in result.output
    memory.auto_recall = True
    assert "workspace lease" in manager.get_relevant_context(query="workspace")


def test_retry_nudges_are_not_misrecorded_as_human_preferences(memory):
    class Model:
        calls = 0
        def next(self, messages):
            self.calls += 1
            return AgentStep(type="assistant", content="" if self.calls == 1 else "finished")
    run_agent_turn(model=Model(), tools=ToolRegistry([]), cwd=str(memory.root),
                   messages=[{"role": "user", "content": "the real human request"}],
                   memory_service=memory)
    events = memory.store.turns(memory.project, memory.workspace)[0]["events"]
    assert [e["content"] for e in events if e["role"] == "user"] == ["the real human request"]


def test_duplicate_extracted_fact_is_noop_but_both_sources_remain(memory):
    class Extractor:
        def extract(self, turn, existing):
            return {"memories": [{"statement": "Use py -m pytest", "evidence": [
                {"event_id": turn["events"][0]["id"], "quote": "py -m pytest"}]}]}
    for index in range(2):
        memory.model = None
        memory.capture(TurnRecorder("Use py -m pytest", turn_id=f"repeated-{index}"))
        memory.model = Extractor()
        assert memory.process_one()
    assert len(memory.store.records(memory.project, memory.workspace)) == 1
    assert len(memory.store.turns(memory.project, memory.workspace)) == 2
