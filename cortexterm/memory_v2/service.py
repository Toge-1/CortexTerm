"""Project memory with evidence validation, scoped retrieval and durable extraction."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from cortexterm.context_manager import estimate_tokens
from .retrieval import bm25, cosine, rrf, time_window
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
        self.recorder.append(value)


class MemoryService:
    def __init__(self, project_root: Path, db_path: Path, *, model=None,
                 mode: str = "hybrid", graph: bool = False, max_tokens: int = 2400,
                 embedder=None):
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
            # Limit source size deterministically; every retained quote still points
            # to the original durable event. Pairing IDs survive tool batching.
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
            existing = self.store.records(self.project, self.workspace)
            comparison = turn["request"] + " " + " ".join(
                str(e.get("content", "")) for e in bounded_events if e["role"] == "user"
            )
            ranked = bm25(comparison, [r["statement"] for r in existing])[:8]
            offered = [existing[i] for i, _ in ranked if existing[i]["scope"] == "local"]
            proposal = self.model.extract(
                {**turn, "events": bounded_events},
                [{"id": r["id"], "statement": r["statement"], "version": r["version"]} for r in offered],
            )
            records, changes = self._validate_proposal(turn, proposal, offered)
            self.store.apply(records, changes, completed_turn=turn["id"], claim_attempt=turn["attempts"])
            try:
                self._embed(records)
            except Exception as exc:
                LOG.warning("Memory saved without vectors (%s)", type(exc).__name__)
        except Exception as exc:
            self.store.fail(turn["id"], type(exc).__name__ + ": " + redact(str(exc)), attempt=turn["attempts"])
            LOG.warning("Memory extraction failed (%s); original evidence retained", type(exc).__name__)
        return True

    def _validate_proposal(self, turn: dict, proposal: dict, existing: list[dict]):
        candidates = proposal.get("memories")
        if not isinstance(candidates, list) or len(candidates) > 4:
            raise ValueError("At most four candidate memories are allowed")
        events = {event["id"]: event for event in turn["events"]}
        previous = {r["id"]: r for r in existing}
        records, changes = [], []
        replaced = set()
        for index, candidate in enumerate(candidates):
            statement = candidate.get("statement", "")
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 1200 or _SECRET.search(statement):
                raise ValueError("Invalid memory statement")
            quotes = candidate.get("evidence", [])
            if not isinstance(quotes, list) or not 1 <= len(quotes) <= 8:
                raise ValueError("Each memory needs source evidence")
            evidence = []
            for item in quotes:
                event = events.get(item.get("event_id"))
                quote = item.get("quote")
                if event is None or not isinstance(quote, str) or len(quote.strip()) < 4:
                    raise ValueError("Missing evidence")
                source = event.get("content", "")
                if not isinstance(source, str):
                    source = json.dumps(source, ensure_ascii=False)
                if event.get("role") == "assistant_tool_call":
                    source = json.dumps(event.get("input", {}), ensure_ascii=False)
                if quote not in source:
                    raise ValueError("Evidence quote is not present in the cited event")
                evidence.append({"event_id": event["id"], "role": event["role"], "quote": quote[:4000]})
            keywords = candidate.get("keywords", [])
            entities = candidate.get("entities", [])
            if any(not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                   for values in (keywords, entities)):
                raise ValueError("Keywords/entities must be lists of strings")
            record = make_record(
                project=self.project, workspace=self.workspace, statement=statement,
                kind=str(candidate.get("kind", "experience"))[:30],
                keywords=[redact(v)[:100] for v in keywords[:12]],
                entities=[v[:200] for v in entities[:6]], evidence=evidence,
                source_ids=[e["event_id"] for e in evidence], recorded_at=turn["recorded_at"],
                origin="extracted", record_id=stable_id(turn["id"], str(index)),
            )
            records.append(record)
            for old_id in candidate.get("supersedes", []):
                if old_id not in previous or old_id in replaced:
                    raise ValueError("Invalid replacement target")
                old = previous[old_id]
                if old["origin"] == "explicit":
                    raise ValueError("Automatic extraction cannot override explicit user memory")
                changes.append({"old_id": old_id, "new_id": record["id"], "version": old["version"]})
                replaced.add(old_id)
        return records, changes

    def remember(self, content: str, scope: str, kind: str, tags: list[str],
                 supersedes: list[str] | None = None) -> str:
        if scope not in {"user", "project", "local"} or _SECRET.search(content):
            raise ValueError("Invalid or sensitive memory")
        record = make_record(
            project="" if scope == "user" else self.project,
            workspace=self.workspace if scope == "local" else "",
            statement=content, scope=scope, kind=kind, keywords=tags,
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

    def _embed(self, records: list[dict]) -> None:
        if self.embedder is None or not records:
            return
        vectors = self.embedder.encode([r["statement"] for r in records])
        if len(vectors) != len(records):
            raise ValueError("Embedding count mismatch")
        with self.store.connect() as db:
            db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?)", [
                (r["id"], self.embedder.model_id, json.dumps(vector))
                for r, vector in zip(records, vectors)
            ])

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
               include_superseded: bool = False, mode: str | None = None) -> dict:
        started = time.monotonic()
        mode = mode or self.mode
        if mode == "off":
            return {"results": [], "trace": {"mode": mode}}
        now = now or datetime.now().astimezone()
        window = time_window(query, now)
        turns = [t for t in self.store.turns(self.project, self.workspace) if t["recorded_at"] <= now.timestamp()]
        records = [r for r in self.store.records(self.project, self.workspace, active_only=not include_superseded)
                   if r["recorded_at"] <= now.timestamp() and self._fresh(r)]
        by_id = {r["id"]: r for r in records}
        rankings: list[list[str]] = []
        channels: dict[str, list[str]] = {}
        raw = [r for t in turns for r in self._turn_chunks(t)]
        if mode == "raw":
            # Explicit memories work in both variants. Raw never depends on extraction.
            raw += [r for r in records if r["origin"] in {"explicit", "legacy"}]
            by_id = {r["id"]: r for r in raw}
            channels["raw"] = [raw[i]["id"] for i, _ in bm25(query, [r["statement"] for r in raw])[:40]]
        else:
            channels["statement"] = [records[i]["id"] for i, _ in bm25(query, [r["statement"] for r in records])[:40]]
            channels["keywords"] = [records[i]["id"] for i, _ in bm25(
                query, [r["statement"] + " " + " ".join(r["keywords"] + r["entities"]) for r in records],
            )[:40]]
            # Raw evidence is a fallback, preserving details extraction may omit.
            channels["raw"] = [raw[i]["id"] for i, _ in bm25(query, [r["statement"] for r in raw])[:15]]
            by_id.update({r["id"]: r for r in raw})
            if self.embedder and records:
                try:
                    vector = self.embedder.encode([query])[0]
                    with self.store.connect() as db:
                        vectors = db.execute("SELECT id,vector FROM vectors WHERE model=?", (self.embedder.model_id,))
                        ranked = sorted(((r["id"], cosine(vector, json.loads(r["vector"]))) for r in vectors
                                         if r["id"] in by_id), key=lambda v: -v[1])
                    channels["semantic"] = [i for i, score in ranked[:40] if score >= .35]
                except Exception as exc:
                    LOG.warning("Semantic memory unavailable: %s", type(exc).__name__)
        if window:
            matching = [t for t in turns if window[0] <= t["recorded_at"] < window[1]]
            channels["time"] = [t["id"] for t in matching]
            by_id.update({t["id"]: self._turn_record(t) for t in matching})
            # Explicit time questions are hard-filtered; recency cannot override it.
            by_id = {i: r for i, r in by_id.items() if window[0] <= r["recorded_at"] < window[1]}
        rankings = [[i for i in ranking if i in by_id] for ranking in channels.values()]
        fused = rrf(rankings)
        if self.graph and mode != "raw" and not window:
            neighbors = [i for i in self.store.related([i for i, _ in fused[:3]]) if i in by_id]
            # Expansion adds candidates, not a free relevance boost for every neighbor.
            neighbor_rows = [by_id[i] for i in neighbors]
            channels["graph"] = [neighbor_rows[i]["id"] for i, _ in bm25(
                query, [r["statement"] for r in neighbor_rows],
            )[:10]]
            fused = rrf(rankings + [channels["graph"]])
        results = [{**by_id[i], "score": score} for i, score in fused[:limit]]
        trace = {"mode": mode, "channels": channels, "time_window": window,
                 "eligible_records": len(records), "seconds": round(time.monotonic() - started, 4)}
        self.last_trace = trace
        return {"results": results, "trace": trace}

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
                chunks.append(make_record(
                    project=self.project, workspace=self.workspace,
                    statement=f"{event['role']} {event.get('toolName', '')}: {excerpt}",
                    kind="episode", evidence=[{"event_id": event["id"], "quote": excerpt}],
                    source_ids=[event["id"]], recorded_at=turn["recorded_at"], origin="raw",
                    record_id=f"{turn['id']}#chunk:{event['id'].rsplit(':', 1)[-1]}:{start}",
                ))
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
            text = record["statement"]
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
            return {**record, "source_turn_ids": list(dict.fromkeys(
                i.rsplit(":", 1)[0] for i in record["source_ids"]
            ))}
        turn_id = identity.split("#chunk:", 1)[0]
        turns = self.store.turns(self.project, self.workspace)
        turn = next((t for t in turns if t["id"] == turn_id), None)
        if turn:
            return self._turn_record(turn)
        # Source IDs printed by memory_search/read can themselves be opened.
        for turn in turns:
            event = next((e for e in turn["events"] if e["id"] == identity), None)
            if event:
                return {**event, "turn_id": turn["id"], "session": turn["session"]}
        return None

    def import_legacy(self, manager) -> int:
        before = len(self.store.records(self.project, self.workspace, active_only=False))
        for scope, memory in manager.memories.items():
            for entry in memory.entries:
                record = make_record(
                    project="" if scope.value == "user" else self.project,
                    workspace=self.workspace if scope.value == "local" else "",
                    scope=scope.value, statement=redact(entry.content), kind=entry.category,
                    keywords=entry.tags, recorded_at=entry.created_at, origin="legacy",
                    record_id=stable_id("legacy", scope.value, entry.id, entry.content,
                                        "" if scope.value == "user" else self.project),
                )
                self.store.apply([record])
        return len(self.store.records(self.project, self.workspace, active_only=False)) - before
