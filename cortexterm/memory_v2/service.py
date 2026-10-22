"""Project memory with evidence validation, scoped retrieval and durable extraction."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from cortexterm.context_manager import estimate_tokens
from .retrieval import bm25, excerpt, parse_time_range, search_text, tokenize, token_spans
from .store import MemoryStore, make_record, stable_id

LOG = logging.getLogger(__name__)
_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|(?i:bearer)\s+[A-Za-z0-9._-]{12,}|"
    r"(?i:api[_-]?key|password|passwd|secret|auth[_-]?token)\s*[:=]\s*\S+"
)


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.fullmatch(
            r"(?i:api[_-]?key|password|passwd|secret|auth[_-]?token)", k,
        ) else redact_value(v)) for k, v in value.items()}
    return value


def event_text(event: dict) -> str:
    value = event.get("input", {}) if event.get("role") == "assistant_tool_call" else event.get("content", "")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def source_fragments(event: dict) -> list[dict]:
    """Stable offsets into original text, with paragraph/line boundaries when possible."""
    text = event_text(event)
    fragments, start = [], 0
    while start < len(text):
        end = min(start + 1000, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + 500, end)
            if boundary >= 0:
                end = boundary + 1
        fragments.append({"id": f"{event['id']}#part:{start}", "event_id": event["id"],
                          "start": start, "end": end, "text": text[start:end]})
        start = end
    return fragments


def project_identity(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=cwd,
            capture_output=True, text=True, timeout=2, check=True,
        )
        return os.path.normcase(str((cwd / result.stdout.strip()).resolve()))
    except (OSError, subprocess.SubprocessError):
        return os.path.normcase(str(cwd.resolve()))


class TurnRecorder:
    """Capture new public events independently of compaction/list positions."""

    def __init__(self, request: str, session: str = "", turn_id: str | None = None):
        self.id = turn_id or uuid.uuid4().hex
        self.session = session
        self.recorded_at = time.time()
        self.request = redact(request)
        self.events: list[dict] = []
        self.append({"role": "user", "content": request})

    def append(self, message: dict) -> None:
        if message.get("role") == "system" or message.get("isCompactionSummary"):
            return
        event = {k: v for k, v in message.items()
                 if k in {"role", "content", "input", "toolName", "toolUseId", "isError"}}
        # Deliberately exclude thinkingBlocks and full re-injected histories.
        event = redact_value(event)
        event["id"] = f"{self.id}:{len(self.events)}"
        event["recorded_at"] = time.time()
        self.events.append(event)


class RecordedMessages(list):
    def __init__(self, messages, recorder: TurnRecorder):
        super().__init__(messages)
        self.recorder = recorder

    def append(self, value):
        super().append(value)
        # The real user's request was captured at turn entry. User-role messages
        # added inside the loop are program-generated retry/continue instructions.
        if value.get("role") != "user":
            self.recorder.append(value)


class MemoryService:
    def __init__(self, project_root: Path, db_path: Path, *, model=None,
                 mode: str = "hybrid", graph: bool = False, max_tokens: int = 2400,
                 embedder=None, auto_recall: bool = False):
        if mode not in {"raw", "hybrid", "off"}:
            raise ValueError("Unsupported memory mode")
        self.root = Path(project_root).resolve()
        self.workspace = os.path.normcase(str(self.root))
        self.project = project_identity(self.root)
        self.store = MemoryStore(db_path)
        self.model = model
        self.mode = mode
        self.graph = graph
        self.max_tokens = max_tokens
        self.embedder = embedder
        if graph or embedder is not None:
            LOG.warning("Memory retrieval now uses one BM25 ranking; graph/embedder options are ignored")
        self.auto_recall = auto_recall
        self.last_trace: dict = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def start(self) -> None:
        """Resume durable jobs without requiring the user to finish another turn."""
        if self.model and not self._stop.is_set():
            with self._worker_lock:
                if self._worker is None:
                    self._worker = threading.Thread(target=self._work, daemon=True, name="memory-extractor")
                    self._worker.start()
            self._wake.set()

    def capture(self, recorder: TurnRecorder) -> None:
        """Persist a completed/failed turn before waking a best-effort worker."""
        self.store.enqueue({
            "id": recorder.id, "project": self.project, "workspace": self.workspace,
            "session": recorder.session, "recorded_at": recorder.recorded_at,
            "request": recorder.request, "events": recorder.events,
        })
        self.start()

    def _work(self):
        while not self._stop.is_set():
            try:
                while not self._stop.is_set() and self.process_one():
                    pass
            except Exception as exc:
                LOG.warning("Memory worker unavailable (%s); queue retained", type(exc).__name__)
            self._wake.wait(1)
            self._wake.clear()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=1)

    def process_one(self) -> bool:
        if self.model is None:
            return False
        if hasattr(self.model, "max_calls") and len(self.model.calls) >= self.model.max_calls:
            # A process-level spending limit is not a failed history item.
            return False
        turn = self.store.claim(self.project, self.workspace)
        if turn is None:
            return False
        try:
            # Bound model input; full original events remain in SQLite for memory_read.
            bounded_events = []
            chars = 0
            # Keep the initial request, then prefer recent results over early logs.
            ordered = turn["events"][:1] + list(reversed(turn["events"][1:]))
            for event in ordered:
                item = dict(event)
                if isinstance(item.get("content"), str):
                    item["content"] = item["content"][:6000]
                size = len(json.dumps(item, ensure_ascii=False))
                if chars + size > 24000:
                    continue
                bounded_events.append(item)
                chars += size
            positions = {e["id"]: n for n, e in enumerate(turn["events"])}
            bounded_events.sort(key=lambda e: positions[e["id"]])
            fragments = {f["id"]: f for e in bounded_events for f in source_fragments(e)}
            supplied_turn = {**turn, "events": bounded_events, "source_fragments": fragments}
            source_events = {e["id"]: e for e in bounded_events}
            proposal = self.store.candidates(turn["id"])
            if proposal is None:
                # Stage 1 sees only this turn, never old facts chosen using the user's initial question.
                proposal = self.model.extract(supplied_turn, [])
                self._validate_proposal(supplied_turn, proposal, [])
                self.store.save_candidates(turn["id"], proposal, turn["attempts"])
            records, _ = self._validate_proposal(supplied_turn, proposal, [])
            existing = [r for r in self.store.records(self.project, self.workspace)
                        if r["recorded_at"] <= time.time() and self._fresh(r)]
            known = list(existing)
            checked = {}
            unique, outcomes = [], []
            for record in records:
                normalized = " ".join(record["statement"].split())
                old = next((r for r in known if self._covers_scope(r, record)
                            and " ".join(r["statement"].split()) == normalized), None)
                if old is None:
                    unique.append(record)
                    known.append(record)
                else:
                    outcomes.append(self._outcome(record, "NOOP", [old["id"]]))
                    if "version" in old:
                        checked[old["id"]] = {"id": old["id"], "version": old["version"]}
            records = unique
            comparisons, offered = self._comparison_context(records, existing)
            changes = []
            if offered:
                if hasattr(self.model, "max_calls") and len(self.model.calls) >= self.model.max_calls:
                    self.store.defer(turn["id"], turn["attempts"])
                    return False
                comparison_records = [{**r, "evidence": [
                    {**e, "role": source_events[e["event_id"]]["role"],
                     "quote": event_text(source_events[e["event_id"]])[e["start"]:e["end"]]}
                    for e in r["evidence"]]} for r in records]
                decision = self.model.consolidate(comparison_records, offered, comparisons)
                proposed = {r["id"]: r for r in records}
                records, changes = self._validate_decisions(records, decision, offered, comparisons)
                by_id = {r["id"]: r for r in offered}
                for item in decision["decisions"]:
                    outcomes.append(self._outcome(proposed[item["candidate_id"]], item["action"], item.get("targets", [])))
                    if item["action"] == "NOOP":
                        for identity in item["targets"]:
                            checked[identity] = {"id": identity, "version": by_id[identity]["version"]}
            else:
                outcomes.extend(self._outcome(r, "ADD", []) for r in records)
            self.store.apply(records, changes, completed_turn=turn["id"], claim_attempt=turn["attempts"],
                             checked_references=list(checked.values()), outcomes=outcomes)
        except Exception as exc:
            self.store.fail(turn["id"], type(exc).__name__ + ": " + redact(str(exc)), attempt=turn["attempts"])
            LOG.warning("Memory extraction failed (%s); original evidence retained", type(exc).__name__)
        return True

    @staticmethod
    def _outcome(record: dict, action: str, targets: list[str]) -> dict:
        return {"action": action, "memory_id": record["id"] if action != "NOOP" else None,
                "scope": record["scope"], "statement": record["statement"], "targets": targets}

    @staticmethod
    def _same_scope(left: dict, right: dict) -> bool:
        return all(left[k] == right[k] for k in ("scope", "project", "workspace"))

    @classmethod
    def _covers_scope(cls, old: dict, candidate: dict) -> bool:
        return (cls._same_scope(old, candidate) or old["scope"] == "user"
                or (old["scope"] == "project" and candidate["scope"] == "local"
                    and old["project"] == candidate["project"]))

    def _can_replace(self, record: dict, candidate: dict) -> bool:
        return self._same_scope(record, candidate) and record.get("status", "active") == "active"

    def _comparison_context(self, candidates: list[dict], existing: list[dict]):
        # Filter authority BEFORE top-k. Read-only references cannot crowd out editable records.
        offered: dict[str, dict] = {}
        comparisons = []
        for candidate in candidates:
            writable = [r for r in existing if self._can_replace(r, candidate)]
            references = [r for r in existing if not self._can_replace(r, candidate)
                          and self._covers_scope(r, candidate)]
            groups = {}
            for name, rows in (("replaceable", writable), ("read_only", references)):
                ranked = bm25(search_text(candidate), [search_text(r) for r in rows])[:8]
                selected = [rows[i] for i, _ in ranked]
                groups[name] = [r["id"] for r in selected]
                offered.update((r["id"], r) for r in selected)
            comparisons.append({"candidate_id": candidate["id"], **groups})
        return comparisons, list(offered.values())

    def _validate_decisions(self, candidates, proposal, existing, comparisons):
        decisions = proposal.get("decisions") if isinstance(proposal, dict) else None
        if not isinstance(decisions, list) or len(decisions) != len(candidates):
            raise ValueError("Consolidation must decide exactly once for each candidate")
        by_id = {r["id"]: r for r in candidates}
        previous = {r["id"]: r for r in existing}
        allowed = {r["candidate_id"]: r for r in comparisons}
        seen, replaced = set(), set()
        records, changes = [], []
        for item in decisions:
            if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str):
                raise ValueError("Invalid consolidation decision")
            identity = item["candidate_id"]
            if identity not in by_id or identity in seen:
                raise ValueError("Unknown or repeated candidate ID")
            seen.add(identity)
            action, targets = item.get("action"), item.get("targets", [])
            if (not isinstance(targets, list) or any(not isinstance(t, str) for t in targets)
                    or len(set(targets)) != len(targets)):
                raise ValueError("Invalid consolidation targets")
            group = allowed[identity]
            if action == "NOOP":
                if not targets or any(t not in group["replaceable"] + group["read_only"] for t in targets):
                    raise ValueError("NOOP must reference an offered existing memory")
                if any(not self._covers_scope(previous[t], by_id[identity]) for t in targets):
                    raise ValueError("A narrower memory cannot cover this candidate's scope")
                continue
            if action == "ADD":
                if targets:
                    raise ValueError("ADD cannot replace existing memories")
            elif action == "SUPERSEDE":
                if not targets:
                    raise ValueError("SUPERSEDE needs a replacement target")
                for target in targets:
                    if (target not in group["replaceable"] or target in replaced
                            or not self._can_replace(previous[target], by_id[identity])):
                        raise ValueError("Cannot supersede a read-only, unoffered or repeated target")
                    changes.append({"old_id": target, "new_id": identity, "version": previous[target]["version"]})
                    replaced.add(target)
            else:
                raise ValueError("Unknown consolidation action")
            records.append(by_id[identity])
        # A NOOP must not rely solely on a fact this same batch makes inactive.
        for item in decisions:
            if item["action"] == "NOOP" and all(t in replaced for t in item["targets"]):
                raise ValueError("NOOP references only memories superseded by this batch")
        return records, changes

    def _validate_proposal(self, turn: dict, proposal: dict, existing: list[dict]):
        candidates = proposal.get("memories") if isinstance(proposal, dict) else None
        if not isinstance(candidates, list) or len(candidates) > 4:
            raise ValueError("At most four candidate memories are allowed")
        events = {event["id"]: event for event in turn["events"]}
        fragments = turn.get("source_fragments")
        if fragments is None:
            fragments = {f["id"]: f for e in turn["events"] for f in source_fragments(e)}
        previous = {r["id"]: r for r in existing}
        records, changes = [], []
        replaced = set()
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ValueError("Invalid candidate memory")
            statement = candidate.get("statement", "")
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 1200 or _SECRET.search(statement):
                raise ValueError("Invalid memory statement")
            evidence_ids = candidate.get("evidence_ids")
            if evidence_ids is None:
                # Old saved checkpoints identify messages. Recover their original
                # fragments without trusting or validating model-copied quotations.
                source_ids = candidate.get("source_ids")
                if source_ids is None and isinstance(candidate.get("evidence"), list):
                    source_ids = [e.get("event_id") for e in candidate["evidence"] if isinstance(e, dict)]
                if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8
                        or any(not isinstance(i, str) or i not in events for i in source_ids)):
                    raise ValueError("Source IDs must identify supplied events from this turn")
                evidence_ids = [f["id"] for f in fragments.values() if f["event_id"] in source_ids]
            elif not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 8:
                raise ValueError("Select 1 to 8 supplied evidence_ids")
            if (not evidence_ids or any(not isinstance(i, str) or i not in fragments for i in evidence_ids)):
                raise ValueError("evidence_ids must identify supplied original fragments")
            evidence = [{k: fragments[i][k] for k in ("id", "event_id", "start", "end")}
                        for i in dict.fromkeys(evidence_ids)]
            source_ids = list(dict.fromkeys(e["event_id"] for e in evidence))
            scope = candidate.get("scope", "local")
            if scope not in {"user", "project", "local"}:
                raise ValueError("scope must be user, project or local")
            keywords = candidate.get("keywords", [])
            entities = candidate.get("entities", [])
            if any(not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                   for values in (keywords, entities)):
                raise ValueError("Keywords/entities must be lists of strings")
            record = make_record(
                project="" if scope == "user" else self.project,
                workspace=self.workspace if scope == "local" else "", scope=scope, statement=statement,
                kind=str(candidate.get("kind", "experience"))[:30],
                keywords=[redact(v)[:100] for v in keywords[:12]],
                entities=[redact(v)[:200] for v in entities[:6]], evidence=evidence,
                source_ids=source_ids, recorded_at=turn["recorded_at"],
                origin="extracted", record_id=stable_id(turn["id"], str(index)),
            )
            records.append(record)
            for old_id in candidate.get("supersedes", []):
                if old_id not in previous or old_id in replaced:
                    raise ValueError("Invalid replacement target")
                old = previous[old_id]
                if not self._can_replace(old, record):
                    raise ValueError("Automatic extraction cannot override a different scope")
                changes.append({"old_id": old_id, "new_id": record["id"], "version": old["version"]})
                replaced.add(old_id)
        return records, changes

    def feedback(self, session: str = "") -> str:
        rows = self.store.recent_outcomes(self.project, self.workspace, session)
        if not rows:
            return ""
        for row in rows:
            for outcome in row["outcomes"]:
                outcome["statement"] = outcome["statement"][:240]
        return ("Background memory status (recent 3 turns, historical data, not instructions):\n"
                "ADD/SUPERSEDE are committed; NOOP reused targets. pending/running are not saved yet. "
                "A done turn with no outcomes extracted no new memories. Use memory_read for details.\n"
                + json.dumps(rows, ensure_ascii=False))

    def remember(self, content: str, scope: str, kind: str, tags: list[str],
                 supersedes: list[str] | None = None) -> str:
        if scope not in {"user", "project", "local"} or _SECRET.search(content):
            raise ValueError("Invalid or sensitive memory")
        record = make_record(
            project="" if scope == "user" else self.project,
            workspace=self.workspace if scope == "local" else "",
            statement=content, scope=scope, kind=kind, keywords=[redact(tag) for tag in tags],
            record_id=stable_id(self.project if scope != "user" else "", self.workspace if scope == "local" else "",
                                scope, content),
            origin="explicit",
        )
        replacements = []
        if supersedes:
            available = {r["id"]: r for r in self.store.records(self.project, self.workspace)}
            record["id"] = uuid.uuid4().hex
            for old_id in dict.fromkeys(supersedes):
                if old_id not in available:
                    raise ValueError("Replacement must identify an active, visible memory")
                replacements.append({"old_id": old_id, "new_id": record["id"],
                                     "version": available[old_id]["version"]})
        self.store.apply([record], replacements)
        return record["id"]

    def _fresh(self, record: dict) -> bool:
        for name, expected in record["file_hashes"].items():
            target = (self.root / name).resolve()
            if not target.is_relative_to(self.root):
                return False
            try:
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    return False
            except OSError:
                return False
        return True

    def search(self, query: str, *, now: datetime | None = None, limit: int = 6,
               include_superseded: bool = False, mode: str | None = None,
               start_time: str | None = None, end_time: str | None = None,
               cursor: str | None = None) -> dict:
        started = time.monotonic()
        mode = mode or self.mode
        if mode == "off":
            return {"results": [], "trace": {"mode": mode}}
        if mode not in {"raw", "hybrid"}:
            raise ValueError("Unsupported memory mode")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        now = now or datetime.now().astimezone()
        window = parse_time_range(start_time, end_time)
        query = query.strip()
        if not query and window is None:
            raise ValueError("Provide a nonempty query or both start_time and end_time")
        if cursor is not None and query:
            raise ValueError("cursor is only supported for time-only history browsing")
        if not query:
            return self._browse_history(window, now.timestamp(), limit, cursor, mode, started)
        turns = [t for t in self.store.turns(self.project, self.workspace) if t["recorded_at"] <= now.timestamp()]
        records = [r for r in self.store.records(self.project, self.workspace, active_only=not include_superseded)
                   if r["recorded_at"] <= now.timestamp() and self._fresh(r)]
        if window:
            # Time is eligibility only, never a ranking bonus.
            turns = [t for t in turns if window[0] <= t["recorded_at"] < window[1]]
            records = [r for r in records if window[0] <= r["recorded_at"] < window[1]]
        raw = [r for t in turns for r in self._turn_chunks(t)]
        if mode == "raw":
            records = [r for r in records if r["origin"] in {"explicit", "legacy"}]
        candidates = sorted(records + raw, key=lambda r: r["id"])
        documents = [search_text(r) for r in candidates]
        ranked = bm25(query, documents)
        terms = set(tokenize(query))
        hits = []
        for index, score in ranked:
            row = candidates[index]
            hit = {**row, "score": score, "result_type": "history_chunk" if row["origin"] == "raw" else "memory",
                   "excerpt": excerpt(row["statement"], query),
                   "source_turn_ids": row.get("source_turn_ids", list(dict.fromkeys(
                       i.rsplit(":", 1)[0] for i in row["source_ids"]))),
                   "matched_terms": sorted(terms.intersection(tokenize(documents[index])))}
            if row["origin"] == "raw":
                # Absolute match locations distinguish shared overlap from two distinct facts in one event.
                prefix = row["content_prefix_length"]
                hit["_matches"] = {(row["chunk_start"] + a - prefix, row["chunk_start"] + b - prefix)
                                   for term, a, b in token_spans(row["statement"])
                                   if term in terms and a >= prefix}
            hits.append(hit)
        results = self._collapse_overlaps(hits)[:limit]
        trace = {"mode": mode, "strategy": "bm25", "channels": {"bm25": [r["id"] for r in results]},
                 "time_window": window, "eligible_records": len(records), "candidate_count": len(candidates),
                 "matched_count": len(ranked), "seconds": round(time.monotonic() - started, 4)}
        if self.graph or self.embedder is not None:
            trace["ignored_options"] = [name for name, active in
                                        (("graph", self.graph), ("embedder", self.embedder is not None)) if active]
        self.last_trace = trace
        return {"results": results, "trace": trace, "next_cursor": None}

    @staticmethod
    def _collapse_overlaps(hits: list[dict]) -> list[dict]:
        groups: list[dict] = []
        for hit in hits:  # Highest scoring representative keeps its BM25 score; never sum scores.
            matches = hit.get("_matches", set())
            overlaps = [r for r in groups if matches and r.get("event_id") == hit.get("event_id")
                        and matches.intersection(r.get("_matches", set()))]
            if not overlaps:
                if hit["origin"] == "raw":
                    hit["matched_chunk_ids"] = [hit["id"]]
                groups.append(hit)
                continue
            target = overlaps[0]
            for other in overlaps[1:] + [hit]:
                target["_matches"].update(other["_matches"])
                target["matched_chunk_ids"].extend(other.get("matched_chunk_ids", [other["id"]]))
                if other is not hit:
                    groups.remove(other)
        for row in groups:
            row.pop("_matches", None)
        return groups

    def _browse_history(self, window, now_stamp, limit, cursor, mode, started):
        scope = stable_id(self.project, self.workspace, str(window))
        after, as_of = None, now_stamp
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                    raise ValueError()
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (data["scope"] != scope or data["version"] != 1
                        or not isinstance(data["id"], str) or not data["id"]
                        or any(type(data[k]) not in (int, float) or not math.isfinite(data[k])
                               for k in ("time", "as_of"))):
                    raise ValueError()
                after = (data["time"], data["id"])
                as_of = min(now_stamp, data["as_of"])
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise ValueError("Invalid history cursor or changed time range/workspace") from error
        rows = self.store.history_page(self.project, self.workspace, start=window[0], end=window[1],
                                       as_of=as_of, after=after, limit=limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more:
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "version": 1, "scope": scope, "as_of": as_of,
                "time": rows[-1]["recorded_at"], "id": rows[-1]["id"],
            }).encode()).decode()
        results = [{"id": r["id"], "kind": "episode", "result_type": "history_turn", "origin": "raw",
                    "scope": "local", "recorded_at": r["recorded_at"], "session": r["session"],
                    "statement": excerpt(r["request"], ""), "source_ids": [r["id"] + ":0"],
                    "source_turn_ids": [r["id"]], "score": None} for r in rows]
        trace = {"mode": mode, "strategy": "chronological", "order": "recorded_at,id ascending",
                 "time_window": window, "seconds": round(time.monotonic() - started, 4)}
        self.last_trace = trace
        return {"results": results, "trace": trace, "has_more": has_more, "next_cursor": next_cursor}

    def _turn_record(self, turn: dict) -> dict:
        lines = []
        evidence = []
        for event in turn["events"]:
            content = event.get("content", event.get("input", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            lines.append(f"{event['role']} {event.get('toolName', '')}: {content}")
            evidence.append({"event_id": event["id"], "role": event["role"], "quote": content})
        return make_record(
            project=self.project, workspace=self.workspace, statement="\n".join(lines),
            kind="episode", evidence=evidence, source_ids=[e["id"] for e in turn["events"]],
            recorded_at=turn["recorded_at"], origin="raw", record_id=turn["id"],
        )

    def _turn_chunks(self, turn: dict) -> list[dict]:
        """Search bounded overlapping evidence chunks, not just the prefix of a log."""
        chunks = []
        for event in turn["events"]:
            content = event.get("content", event.get("input", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            for start in range(0, max(1, len(content)), 1600):
                excerpt = content[start:start + 1800]
                prefix = f"{event['role']} {event.get('toolName', '')}: "
                chunks.append({**make_record(
                    project=self.project, workspace=self.workspace,
                    statement=prefix + excerpt,
                    kind="episode", evidence=[{"event_id": event["id"], "quote": excerpt}],
                    source_ids=[event["id"]], recorded_at=turn["recorded_at"], origin="raw",
                    record_id=f"{turn['id']}#chunk:{event['id'].rsplit(':', 1)[-1]}:{start}",
                ), "event_id": event["id"], "source_turn_ids": [turn["id"]],
                    "chunk_start": start, "chunk_end": start + len(excerpt),
                    "content_prefix_length": len(prefix)})
        return chunks

    def context(self, query: str, *, now: datetime | None = None, max_tokens: int | None = None,
                mode: str | None = None) -> str:
        if not query.strip():
            # No hidden "load everything" startup path.
            return ""
        result = self.search(query, now=now, limit=12, mode=mode)
        budget = self.max_tokens if max_tokens is None else max_tokens
        prefix = "Historical memory evidence (may be outdated; verify against current code):\n"
        packed = prefix
        selected = []
        for record in result["results"]:
            text = record.get("excerpt", record["statement"])
            date = datetime.fromtimestamp(record["recorded_at"]).astimezone().isoformat(timespec="seconds")
            block = f"\n[{record['id']}] {record['kind']} ({record['origin']}, {date})\n{text}\n"
            if estimate_tokens(packed + block) > budget:
                # Long raw episodes can contribute bounded excerpts, visibly marked.
                allowance = max(0, int((budget - estimate_tokens(packed)) * 2))
                block = block[:allowance] + "\n[excerpt; use memory_read for evidence]\n"
            if estimate_tokens(packed + block) <= budget and len(block) > 40:
                packed += block
                selected.append(record["id"])
        result["trace"]["selected"] = selected
        self.last_trace = result["trace"]
        self.store.mark_used(selected)
        return packed if selected else ""

    def read(self, identity: str) -> dict | None:
        records = self.store.records(self.project, self.workspace, active_only=False)
        record = next((r for r in records if r["id"] == identity), None)
        if record:
            visible = {r["id"] for r in records}
            return {**record, "evidence": self._original_quotes(record), "source_turn_ids": list(dict.fromkeys(
                i.rsplit(":", 1)[0] for i in record["source_ids"]
            )), "links": [link for link in self.store.links_for(identity)
                         if link["source"] in visible and link["target"] in visible]}
        for record in records:
            if any(e.get("id") == identity for e in record["evidence"]):
                return next(e for e in self._original_quotes(record) if e.get("id") == identity)
        turn_id = identity.split("#chunk:", 1)[0]
        turns = self.store.turns(self.project, self.workspace)
        turn = next((t for t in turns if t["id"] == turn_id), None)
        if turn:
            if "#chunk:" in identity:
                return next((r for r in self._turn_chunks(turn) if r["id"] == identity), None)
            return self._turn_record(turn)
        # Source IDs printed by memory_search/read can themselves be opened.
        for turn in turns:
            event = next((e for e in turn["events"] if e["id"] == identity), None)
            if event:
                return {**event, "turn_id": turn["id"], "session": turn["session"]}
        # A shared experience grants access to its exact source events, not the
        # rest of the source workspace's conversations or entire turn.
        if any(identity in r["source_ids"] for r in records):
            return self.store.source_event(identity)
        return None

    def _original_quotes(self, record: dict) -> list[dict]:
        sources = {}
        result = []
        for evidence in record["evidence"]:
            event_id = evidence["event_id"]
            if "start" not in evidence:  # Existing records remain readable.
                result.append(evidence)
                continue
            if event_id not in sources:
                sources[event_id] = self.store.source_event(event_id)
            event = sources[event_id]
            if event is not None:
                result.append({**evidence, "role": event["role"],
                               "quote": event_text(event)[evidence["start"]:evidence["end"]]})
            else:
                result.append({**evidence, "source_missing": True})
        return result

    def import_legacy(self, manager) -> int:
        before = len(self.store.records(self.project, self.workspace, active_only=False))
        for scope, memory in manager.memories.items():
            for entry in memory.entries:
                record = make_record(
                    project="" if scope.value == "user" else self.project,
                    workspace=self.workspace if scope.value == "local" else "",
                    scope=scope.value, statement=redact(entry.content), kind=entry.category,
                    keywords=[redact(tag) for tag in entry.tags], recorded_at=entry.created_at, origin="legacy",
                    record_id=stable_id("legacy", scope.value, entry.id, entry.content,
                                        "" if scope.value == "user" else self.project),
                )
                self.store.apply([record])
        return len(self.store.records(self.project, self.workspace, active_only=False)) - before
