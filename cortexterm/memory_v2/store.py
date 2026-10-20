"""Durable memory records and their evidence, with short SQLite transactions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, workspace TEXT NOT NULL,
                    session TEXT NOT NULL, recorded_at REAL NOT NULL,
                    request TEXT NOT NULL, events TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', lease_until REAL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, workspace TEXT NOT NULL,
                    scope TEXT NOT NULL, kind TEXT NOT NULL, statement TEXT NOT NULL,
                    keywords TEXT NOT NULL, entities TEXT NOT NULL,
                    source_ids TEXT NOT NULL, evidence TEXT NOT NULL,
                    recorded_at REAL NOT NULL, occurred_at REAL,
                    status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
                    origin TEXT NOT NULL, file_hashes TEXT NOT NULL DEFAULT '{}',
                    uses INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS links (
                    source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
                    PRIMARY KEY(source, target, kind)
                );
                CREATE TABLE IF NOT EXISTS vectors (
                    id TEXT NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL,
                    PRIMARY KEY(id, model)
                );
                CREATE INDEX IF NOT EXISTS memory_project ON memories(project, status);
                CREATE INDEX IF NOT EXISTS turns_project ON turns(project, recorded_at);
            """)

    @contextmanager
    def connect(self):
        # Connections never cross threads. No connection is held during model calls.
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, turn: dict) -> bool:
        with self.connect() as db:
            return db.execute(
                "INSERT OR IGNORE INTO turns(id,project,workspace,session,recorded_at,request,events)"
                " VALUES(?,?,?,?,?,?,?)",
                tuple(turn[k] for k in ("id", "project", "workspace", "session", "recorded_at", "request"))
                + (json.dumps(turn["events"], ensure_ascii=False),),
            ).rowcount == 1

    def claim(self, project: str, workspace: str, *, lease_seconds: int = 180) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE turns SET state='failed',lease_until=0,error='Extraction lease expired after three attempts' "
                "WHERE project=? AND workspace=? AND state='running' AND attempts>=3 AND lease_until<?",
                (project, workspace, time.time()),
            )
            row = db.execute(
                "SELECT * FROM turns WHERE project=? AND workspace=? AND attempts<3 "
                "AND (state='pending' OR (state='running' AND lease_until<?)) "
                "ORDER BY recorded_at LIMIT 1", (project, workspace, time.time()),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE turns SET state='running', attempts=attempts+1, lease_until=? WHERE id=?",
                (time.time() + lease_seconds, row["id"]),
            )
            result = dict(row)
            result["attempts"] += 1
            result["events"] = json.loads(result["events"])
            return result

    def fail(self, turn_id: str, error: str, *, attempt: int | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE turns SET state='failed',lease_until=0,error=? WHERE id=? "
                "AND state='running' AND (? IS NULL OR attempts=?)",
                (error[:300], turn_id, attempt, attempt),
            )

    def retry_failed(self, project: str, workspace: str) -> int:
        with self.connect() as db:
            return db.execute(
                "UPDATE turns SET state='pending' WHERE project=? AND workspace=? "
                "AND state='failed' AND attempts<3", (project, workspace),
            ).rowcount

    @staticmethod
    def decode(row) -> dict:
        result = dict(row)
        for key in ("keywords", "entities", "source_ids", "evidence", "file_hashes"):
            result[key] = json.loads(result[key])
        return result

    def records(self, project: str, workspace: str, *, active_only: bool = True) -> list[dict]:
        with self.connect() as db:
            query = (
                "SELECT * FROM memories WHERE "
                "(scope='user' OR (project=? AND scope='project') OR "
                "(project=? AND workspace=? AND scope='local'))"
            )
            if active_only:
                query += " AND status='active'"
            return [self.decode(row) for row in db.execute(query, (project, project, workspace))]

    def turns(self, project: str, workspace: str) -> list[dict]:
        with self.connect() as db:
            # Session conversations are machine/workspace-local; never spill into
            # another checkout merely because it shares the same git repository.
            rows = db.execute(
                "SELECT * FROM turns WHERE project=? AND workspace=? ORDER BY recorded_at",
                (project, workspace),
            )
            return [{**dict(r), "events": json.loads(r["events"])} for r in rows]

    def apply(self, records: list[dict], replacements: list[dict] | None = None,
              *, completed_turn: str | None = None, claim_attempt: int | None = None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if completed_turn and claim_attempt is not None:
                owner = db.execute("SELECT state,attempts FROM turns WHERE id=?", (completed_turn,)).fetchone()
                if owner is None or owner["state"] != "running" or owner["attempts"] != claim_attempt:
                    raise ValueError("Extraction lease was reclaimed by another worker")
            for change in replacements or []:
                old = db.execute("SELECT * FROM memories WHERE id=?", (change["old_id"],)).fetchone()
                new = next((r for r in records if r["id"] == change["new_id"]), None)
                if old is None or new is None or old["version"] != change["version"] or old["status"] != "active":
                    raise ValueError("Memory changed during consolidation; retry from a fresh snapshot")
                if new["id"] == old["id"]:
                    raise ValueError("A memory cannot supersede itself")
                if (old["scope"], old["project"], old["workspace"]) != (
                    new["scope"], new["project"], new["workspace"],
                ):
                    raise ValueError("A replacement cannot cross memory scopes")
                db.execute("UPDATE memories SET status='superseded',version=version+1 WHERE id=?", (old["id"],))
                db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)", (new["id"], old["id"], "supersedes"))
            for record in records:
                encoded = dict(record)
                for key in ("keywords", "entities", "source_ids", "evidence", "file_hashes"):
                    encoded[key] = json.dumps(encoded.get(key, {} if key == "file_hashes" else []), ensure_ascii=False)
                columns = (
                    "id", "project", "workspace", "scope", "kind", "statement", "keywords",
                    "entities", "source_ids", "evidence", "recorded_at", "occurred_at", "origin", "file_hashes",
                )
                inserted = db.execute(
                    f"INSERT OR IGNORE INTO memories({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(encoded.get(k) for k in columns),
                ).rowcount
                if inserted:
                    # Related records share an explicit entity and visibility scope.
                    peers = db.execute(
                        "SELECT id,entities FROM memories WHERE project=? AND workspace=? AND scope=? "
                        "AND id<>? AND status='active' ORDER BY recorded_at DESC LIMIT 200",
                        (record["project"], record["workspace"], record["scope"], record["id"]),
                    )
                    for peer in peers:
                        if set(json.loads(peer["entities"])) & set(record["entities"]):
                            db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)", (record["id"], peer["id"], "related"))
            if completed_turn:
                db.execute("UPDATE turns SET state='done',error='',lease_until=0 WHERE id=?", (completed_turn,))

    def related(self, ids: list[str]) -> list[str]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT source,target FROM links WHERE kind='related' "
                f"AND (source IN ({placeholders}) OR target IN ({placeholders})) LIMIT 200", ids + ids,
            )
            return list(dict.fromkeys(x for r in rows for x in r if x not in ids))

    def links_for(self, identity: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT source,target,kind FROM links WHERE source=? OR target=? LIMIT 100",
                (identity, identity),
            )]

    def mark_used(self, ids: list[str]) -> None:
        with self.connect() as db:
            db.executemany("UPDATE memories SET uses=uses+1 WHERE id=?", [(i,) for i in ids])

    def stats(self, project: str, workspace: str) -> dict[str, Any]:
        records = self.records(project, workspace, active_only=False)
        with self.connect() as db:
            jobs = dict(db.execute(
                "SELECT state,count(*) FROM turns WHERE project=? AND workspace=? GROUP BY state",
                (project, workspace),
            ).fetchall())
        return {"records": len(records), "active": sum(r["status"] == "active" for r in records), "jobs": jobs}


def make_record(*, project: str, workspace: str, statement: str, scope: str = "local",
                kind: str = "experience", keywords=(), entities=(), evidence=(), source_ids=(),
                recorded_at: float | None = None, occurred_at: float | None = None,
                origin: str = "explicit", record_id: str | None = None, file_hashes=None) -> dict:
    return {
        "id": record_id or uuid.uuid4().hex, "project": project, "workspace": workspace,
        "scope": scope, "kind": kind, "statement": statement, "keywords": list(keywords),
        "entities": list(entities), "evidence": list(evidence), "source_ids": list(source_ids),
        "recorded_at": time.time() if recorded_at is None else recorded_at,
        "occurred_at": occurred_at, "origin": origin, "file_hashes": file_hashes or {},
    }
