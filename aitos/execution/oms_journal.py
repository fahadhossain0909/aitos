"""Transactional, scope-keyed OMS journal with CAS revisions and writer fencing.

SQLite WAL is supported for single-host paper/test deployments only. Production
multi-host deployments must use the same schema/transactions on PostgreSQL.
This journal is append-only; ClickHouse is a projection, never authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\\Z")
_MAX = 2**63 - 1


class OMSConflict(RuntimeError):
    """CAS conflict or stale writer; caller must reconcile, never blind retry."""


class OMSIntegrityError(RuntimeError):
    """Journal chain or stored payload failed integrity validation."""


@dataclass(frozen=True)
class WriterFence:
    scope: str
    writer_id: str
    generation: int


@dataclass(frozen=True)
class OMSEvent:
    scope: str
    aggregate_id: str
    revision: int
    event_id: str
    writer_id: str
    generation: int
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class TransactionalOMSJournal:
    """Append-only financial event journal with atomic CAS and fencing.

    A scope has exactly one current writer generation. Every append verifies
    the generation inside BEGIN IMMEDIATE and advances the aggregate revision
    atomically. Event hashes chain each aggregate's history.
    """

    def __init__(self, path: str | Path, *, scope: str):
        self.path = Path(path).resolve()
        self.scope = self._id(scope, "scope")
        if not self.path.parent.is_dir():
            raise ValueError("journal parent directory must already exist")
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS oms_writer_fences(
              scope TEXT PRIMARY KEY, writer_id TEXT NOT NULL,
              generation INTEGER NOT NULL CHECK(generation>0));
            CREATE TABLE IF NOT EXISTS oms_heads(
              scope TEXT NOT NULL, aggregate_id TEXT NOT NULL,
              revision INTEGER NOT NULL CHECK(revision>=0),
              event_hash TEXT NOT NULL,
              PRIMARY KEY(scope,aggregate_id));
            CREATE TABLE IF NOT EXISTS oms_events(
              scope TEXT NOT NULL, aggregate_id TEXT NOT NULL,
              revision INTEGER NOT NULL CHECK(revision>0), event_id TEXT NOT NULL,
              writer_id TEXT NOT NULL, generation INTEGER NOT NULL,
              payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
              event_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(scope,aggregate_id,revision),
              UNIQUE(scope,event_id));
            CREATE TABLE IF NOT EXISTS oms_outbox(
              scope TEXT NOT NULL, aggregate_id TEXT NOT NULL, revision INTEGER NOT NULL,
              projected INTEGER NOT NULL DEFAULT 0 CHECK(projected IN (0,1)),
              PRIMARY KEY(scope,aggregate_id,revision),
              FOREIGN KEY(scope,aggregate_id,revision)
                REFERENCES oms_events(scope,aggregate_id,revision));
            CREATE TRIGGER IF NOT EXISTS oms_events_no_update
              BEFORE UPDATE ON oms_events BEGIN
                SELECT RAISE(ABORT,'OMS journal is append-only');
              END;
            CREATE TRIGGER IF NOT EXISTS oms_events_no_delete
              BEFORE DELETE ON oms_events BEGIN
                SELECT RAISE(ABORT,'OMS journal is append-only');
              END;
            """)
            db.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _id(value: str, label: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"valid bounded {label} required")
        return value

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def acquire_writer(self, writer_id: str, *, expected_generation: int) -> WriterFence:
        writer_id = self._id(writer_id, "writer_id")
        if type(expected_generation) is not int or not 0 <= expected_generation < _MAX:
            raise ValueError("expected_generation must be a non-negative int64")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT writer_id,generation FROM oms_writer_fences WHERE scope=?",
                             (self.scope,)).fetchone()
            actual = int(row["generation"]) if row else 0
            if actual != expected_generation:
                db.rollback()
                raise OMSConflict("writer generation CAS failed")
            generation = actual + 1
            db.execute("""INSERT INTO oms_writer_fences(scope,writer_id,generation)
                VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET
                writer_id=excluded.writer_id,generation=excluded.generation""",
                (self.scope, writer_id, generation))
            db.commit()
        return WriterFence(self.scope, writer_id, generation)

    @staticmethod
    def _payload(value: dict[str, Any]) -> str:
        if type(value) is not dict:
            raise ValueError("event payload must be a JSON object")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("OMS event payload exceeds 64 KiB")
        return encoded

    def append(self, fence: WriterFence, *, aggregate_id: str, event_id: str,
               expected_revision: int, payload: dict[str, Any]) -> OMSEvent:
        aggregate_id = self._id(aggregate_id, "aggregate_id")
        event_id = self._id(event_id, "event_id")
        if type(fence) is not WriterFence or fence.scope != self.scope:
            raise OMSConflict("writer fence belongs to another scope")
        if type(expected_revision) is not int or not 0 <= expected_revision < _MAX:
            raise ValueError("expected_revision must be non-negative int64")
        encoded = self._payload(payload)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT writer_id,generation FROM oms_writer_fences WHERE scope=?",
                               (self.scope,)).fetchone()
            if owner is None or (owner["writer_id"], owner["generation"]) != (
                fence.writer_id, fence.generation
            ):
                db.rollback()
                raise OMSConflict("stale writer fencing token")
            existing = db.execute("SELECT aggregate_id,revision,payload_json FROM oms_events "
                                  "WHERE scope=? AND event_id=?",
                                  (self.scope,event_id)).fetchone()
            if existing:
                if existing["aggregate_id"] != aggregate_id or existing["payload_json"] != encoded:
                    db.rollback()
                    raise OMSConflict("event id reused with different content")
                head = db.execute("SELECT event_hash FROM oms_events WHERE scope=? AND aggregate_id=? "
                                  "AND revision=?", (self.scope,aggregate_id,existing["revision"])).fetchone()
                db.commit()
                return self._event(self.scope, aggregate_id, existing["revision"], event_id,
                                   fence, encoded, "", head["event_hash"])
            head = db.execute("SELECT revision,event_hash FROM oms_heads WHERE scope=? AND aggregate_id=?",
                              (self.scope,aggregate_id)).fetchone()
            revision = int(head["revision"]) if head else 0
            previous_hash = str(head["event_hash"]) if head else "0" * 64
            if revision != expected_revision:
                db.rollback()
                raise OMSConflict(f"revision CAS failed: expected {expected_revision}, actual {revision}")
            next_revision = revision + 1
            material = "\\0".join((self.scope,aggregate_id,str(next_revision),event_id,
                fence.writer_id,str(fence.generation),previous_hash,encoded))
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            db.execute("INSERT INTO oms_events(scope,aggregate_id,revision,event_id,writer_id,"
                       "generation,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                       (self.scope,aggregate_id,next_revision,event_id,fence.writer_id,
                        fence.generation,encoded,previous_hash,digest))
            db.execute("""INSERT INTO oms_heads(scope,aggregate_id,revision,event_hash)
                VALUES(?,?,?,?) ON CONFLICT(scope,aggregate_id) DO UPDATE SET
                revision=excluded.revision,event_hash=excluded.event_hash""",
                (self.scope,aggregate_id,next_revision,digest))
            db.execute("INSERT INTO oms_outbox(scope,aggregate_id,revision) VALUES(?,?,?)",
                       (self.scope,aggregate_id,next_revision))
            db.commit()
        return self._event(self.scope,aggregate_id,next_revision,event_id,fence,
                           encoded,previous_hash,digest)

    @staticmethod
    def _event(scope, aggregate_id, revision, event_id, fence, encoded, previous, digest):
        return OMSEvent(scope,aggregate_id,int(revision),event_id,fence.writer_id,
                        fence.generation,json.loads(encoded),previous,digest)

    def read(self, aggregate_id: str) -> list[OMSEvent]:
        aggregate_id = self._id(aggregate_id, "aggregate_id")
        with self._db() as db:
            rows = db.execute("SELECT * FROM oms_events WHERE scope=? AND aggregate_id=? "
                              "ORDER BY revision", (self.scope,aggregate_id)).fetchall()
        events=[]
        previous="0"*64
        for row in rows:
            material="\\0".join((self.scope,aggregate_id,str(row["revision"]),row["event_id"],
                row["writer_id"],str(row["generation"]),previous,row["payload_json"]))
            expected=hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                raise OMSIntegrityError("OMS event chain verification failed")
            fence=WriterFence(self.scope,row["writer_id"],int(row["generation"]))
            events.append(self._event(self.scope,aggregate_id,row["revision"],row["event_id"],
                fence,row["payload_json"],previous,expected))
            previous=expected
        return events
