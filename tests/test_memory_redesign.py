"""Behavioral regressions for unified retrieval and candidate-first consolidation; no API calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cortexterm.memory_v2.model import MemoryModel
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.memory_v2.store import make_record
from cortexterm.tooling import ToolContext, ToolRegistry
from cortexterm.tools.memory_tools import memory_read_tool, memory_search_tool


START = "2026-10-20T18:00:00+08:00"
END = "2026-10-21T00:00:00+08:00"
STAMP = datetime.fromisoformat(START).timestamp()
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


@pytest.fixture
def memory(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return MemoryService(root, tmp_path / "memory.sqlite3")


def fact(memory, text, *, identity=None, scope="local", origin="extracted", **kwargs):
    record = make_record(
        project="" if scope == "user" else memory.project,
        workspace=memory.workspace if scope == "local" else "", statement=text,
        record_id=identity, scope=scope, origin=origin, recorded_at=STAMP, **kwargs,
    )
    memory.store.apply([record])
    return record


def turn(memory, identity, request, output=None, timestamp=STAMP):
    recorder = TurnRecorder(request, turn_id=identity)
    recorder.recorded_at = timestamp
    if output is not None:
        recorder.append({"role": "tool_result", "content": output, "toolUseId": "A"})
    memory.capture(recorder)
    return recorder


def search(memory, query, **kwargs):
    return memory.search(query, now=NOW, **kwargs)


def test_experience_gets_no_second_vote_for_duplicate_search_view(memory):
    # A perfect original match must not lose to a verbose experience just because
    # the latter can appear in both the old statement and keyword rankings.
    fact(memory, "needle " + "padding " * 300, identity="experience")
    turn(memory, "history", "needle")
    response = search(memory, "needle")
    assert response["results"][0]["id"] == "history#chunk:0:0"
    assert {r["id"] for r in response["results"]} == {"experience", "history#chunk:0:0"}
    assert set(response["trace"]["channels"]) == {"bm25"}
    assert response["trace"]["candidate_count"] == 2


@pytest.mark.parametrize("mode", ["raw", "hybrid"])
def test_time_filter_never_supplies_nonmatching_candidates(memory, mode):
    fact(memory, "font family configuration", identity="unrelated", origin="explicit")
    turn(memory, "font-history", "change the terminal font")
    turn(memory, "tests", "pytest import failure", timestamp=STAMP + 5)
    response = search(memory, "pytest", mode=mode, start_time=START, end_time=END)
    assert [r["id"] for r in response["results"]] == ["tests#chunk:0:0"]
    assert response["trace"]["strategy"] == "bm25"
    assert search(memory, "zebranotfound", mode=mode, start_time=START, end_time=END)["results"] == []


def test_keywords_work_in_same_document_and_time_does_not_change_score(memory):
    fact(memory, "Use python -m pytest", identity="m", keywords=["解释器"])
    assert search(memory, "解释器")["results"][0]["id"] == "m"
    plain = search(memory, "pytest")["results"]
    ranged = search(memory, "pytest", start_time=START, end_time=END)["results"]
    assert [(r["id"], r["score"]) for r in plain] == [(r["id"], r["score"]) for r in ranged]


def test_raw_mode_excludes_derived_experiences_even_with_time_bounds(memory):
    fact(memory, "needle derived", identity="derived")
    explicit = fact(memory, "needle explicit", identity="explicit", origin="explicit")
    assert [r["id"] for r in search(memory, "needle", mode="raw", start_time=START, end_time=END)["results"]] == [explicit["id"]]


def test_overlapping_hit_is_one_result_and_chunk_id_reads_that_chunk(memory):
    text = "noise " * 275 + " UniqueFailure " + "noise " * 400
    rec = turn(memory, "long", "run tests", text)
    result = search(memory, "UniqueFailure")["results"]
    assert len(result) == 1
    assert len(result[0]["matched_chunk_ids"]) == 2
    assert "UniqueFailure" in result[0]["excerpt"]
    read = memory.read(result[0]["id"])
    assert read["id"] == result[0]["id"]
    assert read["source_turn_ids"] == [rec.id]
    assert len(read["statement"]) < len(text)
    assert memory.read("long#chunk:999:0") is None
    assert memory.read("long")["evidence"][1]["quote"] == text


def test_separate_matches_from_same_event_are_not_deleted(memory):
    text = "AlphaFailure " + "noise " * 700 + " BetaFailure"
    turn(memory, "separate", "inspect", text)
    hits = search(memory, "AlphaFailure BetaFailure")["results"]
    assert len(hits) == 2
    assert any("AlphaFailure" in h["excerpt"] for h in hits)
    assert any("BetaFailure" in h["excerpt"] for h in hits)


def test_tool_excerpt_keeps_match_near_end_of_chunk(memory):
    turn(memory, "tail", "inspect", "padding " * 185 + " ZebraFailure")
    registry = ToolRegistry([memory_search_tool])
    response = registry.execute("memory_search", {"query": "ZebraFailure"},
                                ToolContext(cwd=str(memory.root), memory_service=memory))
    assert response.ok
    row = json.loads(response.output)["results"][0]
    assert "ZebraFailure" in row["statement"]
    assert row["source_turn_ids"] == ["tail"]
    assert row["event_id"] == "tail:1"


def test_history_pagination_has_stable_ties_no_experiences_and_no_log_loading(memory, monkeypatch):
    for identity in ("c", "a", "e", "b", "d"):
        turn(memory, identity, f"request {identity}", "huge logs " * 1000)
    fact(memory, "derived experience", identity="memory")
    monkeypatch.setattr(memory.store, "turns", lambda *a: pytest.fail("Listing headers must not load logs"))
    monkeypatch.setattr(memory.store, "records", lambda *a, **k: pytest.fail("History listing must not load experiences"))
    seen, cursor = [], None
    for expected_more in (True, True, False):
        response = search(memory, "", start_time=START, end_time=END, limit=2, cursor=cursor)
        assert response["has_more"] is expected_more
        assert response["trace"]["strategy"] == "chronological"
        assert all(r["score"] is None and r["result_type"] == "history_turn" for r in response["results"])
        assert all("huge logs" not in r["statement"] for r in response["results"])
        seen.extend(r["id"] for r in response["results"])
        cursor = response["next_cursor"]
    assert seen == ["a", "b", "c", "d", "e"]
    assert cursor is None


def test_history_cursor_through_tools_and_read_full_turn(memory):
    turn(memory, "a", "first request", "result A")
    turn(memory, "b", "second request", "result B")
    registry = ToolRegistry([memory_search_tool, memory_read_tool])
    context = ToolContext(cwd=str(memory.root), memory_service=memory)
    args = {"start_time": START, "end_time": END, "limit": 1}
    first = json.loads(registry.execute("memory_search", args, context).output)
    second = json.loads(registry.execute("memory_search", {**args, "cursor": first["next_cursor"]}, context).output)
    assert [r["id"] for r in second["results"]] == ["b"]
    full = registry.execute("memory_read", {"id": "b"}, context)
    assert "result B" in full.output
    assert second["next_cursor"] is None


def test_cursor_cannot_be_reused_for_other_range_or_workspace(memory, tmp_path):
    turn(memory, "a", "first")
    turn(memory, "b", "second")
    cursor = search(memory, "", start_time=START, end_time=END, limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        search(memory, "", start_time=START, end_time="2026-10-22T00:00:00+08:00", cursor=cursor)
    other = tmp_path / "other"
    other.mkdir()
    service = MemoryService(other, memory.store.path)
    with pytest.raises(ValueError, match="cursor"):
        search(service, "", start_time=START, end_time=END, cursor=cursor)
    with pytest.raises(ValueError, match="cursor"):
        search(memory, "first", start_time=START, end_time=END, cursor=cursor)


@pytest.mark.parametrize("cursor", ["", "!!!!", "bnVsbA==", "e30=", "W10="])
def test_malformed_cursor_fails_before_database_access(memory, monkeypatch, cursor):
    monkeypatch.setattr(memory.store, "connect", lambda: pytest.fail("Must validate before DB"))
    with pytest.raises(ValueError, match="cursor"):
        search(memory, "", start_time=START, end_time=END, cursor=cursor)


class ScriptedExtractor:
    def __init__(self, statement, decide=None, max_calls=20):
        self.statement, self.decide = statement, decide
        self.max_calls, self.calls = max_calls, []
        self.comparisons = None

    def extract(self, turn, existing):
        assert existing == []  # Old memories are retrieved only after candidate creation.
        self.calls.append("extract")
        return {"memories": [{"statement": self.statement, "keywords": ["pytest", "interpreter"],
                              "scope": "local", "evidence_ids": [list(turn["source_fragments"])[-1]]}]}

    def consolidate(self, candidates, existing, comparisons):
        self.calls.append("consolidate")
        self.comparisons = comparisons
        if self.decide:
            return self.decide(candidates, existing, comparisons)
        return {"decisions": [{"candidate_id": r["id"], "action": "ADD", "targets": []} for r in candidates]}


def test_candidates_drive_comparison_and_local_records_survive_readonly_top_hits(memory):
    old = fact(memory, "pytest interpreter " + "padding " * 100, identity="editable")
    for i in range(12):
        fact(memory, "pytest interpreter", identity=f"shared-{i}", scope="user", origin="explicit")
    turn(memory, "new", "帮我看看", "Use python -m pytest with this interpreter")

    def decide(candidates, existing, comparisons):
        assert comparisons[0]["replaceable"] == [old["id"]]
        assert len(comparisons[0]["read_only"]) == 8
        return {"decisions": [{"candidate_id": candidates[0]["id"], "action": "SUPERSEDE", "targets": [old["id"]]}]}

    memory.model = ScriptedExtractor("Use python -m pytest with this interpreter", decide)
    assert memory.process_one()
    assert memory.model.calls == ["extract", "consolidate"]
    assert memory.read(old["id"])["status"] == "superseded"
    assert memory.read("shared-0")["status"] == "active"
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}


@pytest.mark.parametrize("scope,origin", [("project", "extracted"), ("user", "explicit")])
def test_readonly_cannot_be_overridden_and_failure_keeps_evidence(memory, scope, origin):
    old = fact(memory, "pytest interpreter", identity="protected", scope=scope, origin=origin)
    turn(memory, "new", "help", "Change pytest interpreter to another version")

    def decide(candidates, existing, comparisons):
        assert comparisons[0]["replaceable"] == []
        return {"decisions": [{"candidate_id": candidates[0]["id"], "action": "SUPERSEDE", "targets": [old["id"]]}]}

    memory.model = ScriptedExtractor("Use another pytest interpreter", decide)
    assert memory.process_one()
    assert memory.read(old["id"])["status"] == "active"
    assert len(memory.store.records(memory.project, memory.workspace)) == 1
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"failed": 1}
    assert memory.read("new") is not None


def test_readonly_can_justify_semantic_noop(memory):
    old = fact(memory, "Start pytest using the current interpreter", identity="reference", scope="user", origin="explicit")
    turn(memory, "new", "help", "Use python -m pytest")

    def decide(candidates, existing, comparisons):
        return {"decisions": [{"candidate_id": candidates[0]["id"], "action": "NOOP", "targets": [old["id"]]}]}

    memory.model = ScriptedExtractor("Use python -m pytest", decide)
    memory.process_one()
    assert len(memory.store.records(memory.project, memory.workspace)) == 1
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}
    assert memory.read("new") is not None


def test_exact_duplicates_skip_comparison_call(memory):
    fact(memory, "Use python -m pytest", identity="known", scope="project", origin="explicit")
    turn(memory, "new", "help", "Use python -m pytest")
    memory.model = ScriptedExtractor("Use python -m pytest")
    memory.process_one()
    assert memory.model.calls == ["extract"]
    assert len(memory.store.records(memory.project, memory.workspace)) == 1


def test_no_old_matches_skip_comparison_and_empty_candidates_skip_all_comparisons(memory):
    turn(memory, "first", "help", "Use python -m pytest")
    memory.model = ScriptedExtractor("Use python -m pytest")
    memory.process_one()
    assert memory.model.calls == ["extract"]
    memory.model = None
    turn(memory, "empty", "hello")

    class Empty:
        def extract(self, turn, existing):
            return {"memories": []}
        def consolidate(self, *args):
            pytest.fail("Empty extraction must not call comparison")

    memory.model = Empty()
    memory.process_one()
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 2}


def test_budget_between_stages_checkpoints_and_resumes_without_reextracting(memory):
    fact(memory, "pytest interpreter", identity="old")
    turn(memory, "pending", "help", "Use python -m pytest")
    memory.model = ScriptedExtractor("Use python -m pytest", max_calls=1)
    assert not memory.process_one()
    assert memory.model.calls == ["extract"]
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"pending": 1}
    assert memory.store.candidates("pending") is not None
    restarted = MemoryService(memory.root, memory.store.path, model=ScriptedExtractor("unused"))
    assert restarted.process_one()
    assert restarted.model.calls == ["consolidate"]
    assert restarted.store.candidates("pending") is None
    assert restarted.store.stats(restarted.project, restarted.workspace)["jobs"] == {"done": 1}


def test_comparison_failure_then_retry_uses_checkpoint(memory):
    fact(memory, "pytest interpreter", identity="old")
    turn(memory, "retry", "help", "Use python -m pytest")
    memory.model = ScriptedExtractor("Use python -m pytest", lambda *a: {"decisions": []})
    memory.process_one()
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"failed": 1}
    assert memory.store.candidates("retry") is not None
    memory.store.retry_failed(memory.project, memory.workspace)
    memory.model = ScriptedExtractor("unused")
    memory.process_one()
    assert memory.model.calls == ["consolidate"]
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}


def test_noop_cannot_silently_drop_candidate_when_reference_changed_during_api_call(memory):
    old = fact(memory, "pytest interpreter", identity="old")
    turn(memory, "race", "help", "Use python -m pytest")

    def decide(candidates, existing, comparisons):
        memory.remember("The pytest environment changed", "local", "environment", ["pytest"], [old["id"]])
        return {"decisions": [{"candidate_id": candidates[0]["id"], "action": "NOOP", "targets": [old["id"]]}]}

    memory.model = ScriptedExtractor("Use python -m pytest", decide)
    memory.process_one()
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"failed": 1}
    assert memory.store.candidates("race") is not None
    assert memory.read("old")["status"] == "superseded"


def test_old_worker_cannot_change_checkpoint_or_defer_new_owner(memory):
    turn(memory, "lease", "help")
    old = memory.store.claim(memory.project, memory.workspace, lease_seconds=-1)
    new = memory.store.claim(memory.project, memory.workspace)
    memory.store.save_candidates("lease", {"memories": []}, new["attempts"])
    with pytest.raises(ValueError, match="reclaimed"):
        memory.store.save_candidates("lease", {"memories": ["bad"]}, old["attempts"])
    memory.store.defer("lease", old["attempts"])
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"running": 1}
    assert memory.store.candidates("lease") == {"memories": []}


def test_real_model_adapter_uses_distinct_extraction_and_comparison_payloads(memory):
    model = MemoryModel({"model": "test", "baseUrl": "https://api.anthropic.com"})
    requests = []

    def request(body):
        requests.append(body)
        payload = json.loads(body["messages"][0]["content"])
        if "events" in payload:
            assert "existing" not in payload
            event = payload["events"][-1]
            result = {"memories": [{"statement": "Use python -m pytest", "scope": "local",
                                    "evidence_ids": [event["fragments"][0]["id"]]}]}
        else:
            assert "comparisons" in payload and "existing" in payload
            assert payload["evidence"][0]["quote"] == "Use python -m pytest"
            result = {"decisions": [{"candidate_id": payload["candidates"][0]["id"], "action": "ADD", "targets": []}]}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    model.adapter._request = request
    fact(memory, "pytest command", identity="old")
    turn(memory, "new", "help", "Use python -m pytest")
    memory.model = model
    memory.process_one()
    assert len(requests) == 2
    assert len(model.calls) == 2
    assert memory.store.stats(memory.project, memory.workspace)["jobs"] == {"done": 1}
