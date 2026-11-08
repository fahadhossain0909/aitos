"""UNWIRED single-host paper/backtest prototype. Never an exchange/order fence.

Explicit standalone file only; no runners/config/imports activate this module.
SQLite local disk is the authority; an outbox is only at-least-once evidence.
No live scopes, migrations, venue calls, TTL leases or automatic ownership takeover.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from aitos.trading.financial_amendment import (
    AMENDMENT_KEY,
    CostAmendment,
    apply_amendment,
    decode_metadata,
)

MAX_INTEGER = 2**63 - 1
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
TABLES = {
    "revision_meta",
    "revision_writers",
    "revision_events",
    "revision_heads",
    "revision_outbox",
}


class RevisionConflict(ValueError):
    """Caller must reconcile; do not blindly retry financial side effects."""


class JournalIntegrityError(RuntimeError):
    """Stored evidence cannot be safely interpreted."""


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("bounded ASCII identity required")
    return value


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError("signed 64-bit revision/generation required")
    return value


def canonical(payload):
    count = 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if count > 10000 or depth > 16:
            raise ValueError("bounded JSON structure required")
        if value is None or type(value) is bool:
            return
        if type(value) is int:
            if not -MAX_INTEGER - 1 <= value <= MAX_INTEGER:
                raise ValueError("JSON integer out of range")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("finite JSON required")
        elif type(value) is str:
            value.encode("utf-8")
        elif type(value) is list:
            for item in value:
                visit(item, depth + 1)
        elif type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON string keys required")
                visit(key, depth + 1)
                visit(item, depth + 1)
        else:
            raise ValueError("plain JSON types required")

    if type(payload) is not dict:
        raise ValueError("JSON object required")
    visit(payload)
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("payload exceeds 64 KiB")
    return text


@dataclass(frozen=True)
class Scope:
    mode: str
    venue: str
    account: str
    run: str

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in {"paper", "backtest"}:
            raise ValueError("prototype rejects live/unknown modes")
        for value in (self.venue, self.account, self.run):
            identifier(value)

    @property
    def key(self):
        return canonical(
            dict(mode=self.mode, venue=self.venue, account=self.account, run=self.run)
        )


@dataclass(frozen=True)
class Writer:
    scope: Scope
    writer_id: str
    generation: int

    def __post_init__(self):
        if type(self.scope) is not Scope:
            raise ValueError("trusted scope required")
        identifier(self.writer_id)
        integer(self.generation, 1)


@dataclass(frozen=True)
class Revision:
    scope_key: str
    trade_id: str
    revision: int
    event_id: str
    writer_id: str
    generation: int
    payload_json: str
    digest: str

    @property
    def payload(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class AppendResult:
    event: Revision
    head_revision: int
    duplicate: bool


SCHEMA = [
    "CREATE TABLE revision_meta(version INTEGER NOT NULL CHECK(version=1))",
    "CREATE TABLE revision_writers(scope TEXT PRIMARY KEY, writer_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0))",
    """CREATE TABLE revision_events(
        scope TEXT NOT NULL, trade_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        event_id TEXT NOT NULL, writer_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
        payload TEXT NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY(scope,trade_id,revision), UNIQUE(scope,event_id))""",
    """CREATE TABLE revision_heads(scope TEXT NOT NULL, trade_id TEXT NOT NULL,
        revision INTEGER NOT NULL, PRIMARY KEY(scope,trade_id),
        FOREIGN KEY(scope,trade_id,revision) REFERENCES revision_events(scope,trade_id,revision))""",
    """CREATE TABLE revision_outbox(scope TEXT NOT NULL, trade_id TEXT NOT NULL,
        revision INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
        PRIMARY KEY(scope,trade_id,revision),
        FOREIGN KEY(scope,trade_id,revision) REFERENCES revision_events(scope,trade_id,revision))""",
]


class RevisionJournal:
    """Explicit standalone test/prototype file; must never point at live journals."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if str(path) == ":memory:" or not self.path.parent.is_dir():
            raise ValueError(
                "explicit standalone file in an existing directory required"
            )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables:
                if tables != TABLES or db.execute(
                    "SELECT version FROM revision_meta"
                ).fetchall() != [(1,)]:
                    raise JournalIntegrityError(
                        "not a standalone revision prototype database"
                    )
            else:
                for sql in SCHEMA:
                    db.execute(sql)
                db.execute("INSERT INTO revision_meta VALUES(1)")
        with self._connect() as db:
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise JournalIntegrityError("WAL unavailable")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _owned(db, writer):
        if type(writer) is not Writer:
            raise ValueError("writer token required")
        actual = db.execute(
            "SELECT writer_id,generation FROM revision_writers WHERE scope=?",
            (writer.scope.key,),
        ).fetchone()
        if actual != (writer.writer_id, writer.generation):
            raise RevisionConflict("stale writer generation")

    def acquire(
        self, scope: Scope, writer_id: str, *, expected_generation: int
    ) -> Writer:
        if type(scope) is not Scope:
            raise ValueError("trusted scope required")
        identifier(writer_id)
        integer(expected_generation)
        if expected_generation == MAX_INTEGER:
            raise RevisionConflict("generation exhausted")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT generation FROM revision_writers WHERE scope=?", (scope.key,)
            ).fetchone()
            if (row[0] if row else 0) != expected_generation:
                raise RevisionConflict("ownership compare-and-swap failed")
            generation = expected_generation + 1
            db.execute(
                "INSERT INTO revision_writers VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET writer_id=excluded.writer_id,generation=excluded.generation",
                (scope.key, writer_id, generation),
            )
        return Writer(scope, writer_id, generation)

    @staticmethod
    def _decode(row):
        try:
            if row is None:
                raise ValueError()
            event = Revision(*row)
            integer(event.revision, 1)
            integer(event.generation, 1)
            identifier(event.trade_id)
            identifier(event.event_id)
            identifier(event.writer_id)
            scope = json.loads(event.scope_key)
            if (
                Scope(scope["mode"], scope["venue"], scope["account"], scope["run"]).key
                != event.scope_key
            ):
                raise ValueError()
            payload = event.payload
            if (
                canonical(payload) != event.payload_json
                or payload.get("trade_id") != event.trade_id
                or payload.get("state")
                not in {"position_opened", "exit_triggered", "position_closed"}
            ):
                raise ValueError()
            if AMENDMENT_KEY in payload:
                decode_metadata(payload[AMENDMENT_KEY])
            if (
                hashlib.sha256(event.payload_json.encode("utf-8")).hexdigest()
                != event.digest
            ):
                raise ValueError()
            return event
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise JournalIntegrityError("invalid committed revision evidence") from exc

    @staticmethod
    def _head(db, scope_key, trade_id):
        head = db.execute(
            "SELECT revision FROM revision_heads WHERE scope=? AND trade_id=?",
            (scope_key, trade_id),
        ).fetchone()
        if head is None:
            if db.execute(
                "SELECT 1 FROM revision_events WHERE scope=? AND trade_id=? UNION SELECT 1 FROM revision_outbox WHERE scope=? AND trade_id=? LIMIT 1",
                (scope_key, trade_id, scope_key, trade_id),
            ).fetchone():
                raise JournalIntegrityError("orphaned revision history")
            return None
        event = None
        count = 0
        terminal = False
        for row in db.execute(
            "SELECT e.*,o.delivered FROM revision_events e LEFT JOIN revision_outbox o USING(scope,trade_id,revision) WHERE e.scope=? AND e.trade_id=? ORDER BY e.revision",
            (scope_key, trade_id),
        ):
            current = RevisionJournal._decode(row[:8])
            count += 1
            if current.revision != count or row[8] not in (0, 1):
                raise JournalIntegrityError("inconsistent revision history/outbox")
            if AMENDMENT_KEY in current.payload:
                try:
                    metadata = current.payload[AMENDMENT_KEY]
                    if json.loads(scope_key)["mode"] != "paper":
                        raise ValueError("financial amendments are paper-only")
                    if (
                        event is None
                        or metadata["parent_revision"] != event.revision
                        or metadata["parent_digest"] != event.digest
                    ):
                        raise ValueError("amendment parent mismatch")
                    expected = apply_amendment(event.payload, metadata)
                    if canonical(expected) != current.payload_json:
                        raise ValueError("unauthorized amendment delta")
                    # Lazy typed validation avoids importing the adapter on module import.
                    from aitos.trading.paper_revision import decode_trade

                    decode_trade(event.payload)
                    decode_trade(current.payload)
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    raise JournalIntegrityError(
                        "invalid financial amendment lineage"
                    ) from exc
            elif terminal:
                raise JournalIntegrityError(
                    "terminal history requires explicit amendment"
                )
            terminal = current.payload["state"] == "position_closed"
            event = current
        outbox_count = db.execute(
            "SELECT count(*) FROM revision_outbox WHERE scope=? AND trade_id=?",
            (scope_key, trade_id),
        ).fetchone()[0]
        if event is None or event.revision != head[0] or outbox_count != count:
            raise JournalIntegrityError("inconsistent revision head/history")
        return event

    @staticmethod
    def _scope_receipts(db, scope_key):
        """Check authoritative journal uniqueness, not projection duplicates."""
        identities = set()
        digests = set()
        records = []
        for row in db.execute(
            "SELECT * FROM revision_events WHERE scope=?", (scope_key,)
        ):
            event = RevisionJournal._decode(row)
            metadata = event.payload.get(AMENDMENT_KEY)
            if metadata is None:
                continue
            identity = (metadata["source"], metadata["adjustment_id"])
            digest = metadata["receipt_sha256"]
            if identity in identities or digest in digests:
                raise JournalIntegrityError("duplicate authoritative financial receipt")
            identities.add(identity)
            digests.add(digest)
            records.append(metadata)
        return records

    def head(self, scope: Scope, trade_id: str) -> Revision | None:
        if type(scope) is not Scope:
            raise ValueError("trusted scope required")
        identifier(trade_id)
        with self._connect() as db:
            db.execute("BEGIN")
            return self._head(db, scope.key, trade_id)

    def find_event(self, scope: Scope, event_id: str) -> Revision | None:
        """Validated immutable identity lookup for exact redelivery handling."""
        if type(scope) is not Scope:
            raise ValueError("trusted scope required")
        identifier(event_id)
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM revision_events WHERE scope=? AND event_id=?",
                (scope.key, event_id),
            ).fetchone()
            if row is None:
                return None
            event = self._decode(row)
            self._head(db, scope.key, event.trade_id)
            return event

    def is_current(self, writer: Writer) -> bool:
        """Point-in-time storage ownership observation, not order authorization."""
        with self._connect() as db:
            db.execute("BEGIN")
            try:
                self._owned(db, writer)
            except RevisionConflict:
                return False
            return True

    def heads(self, scope: Scope, *, limit=1000) -> list[Revision]:
        """Complete bounded recovery snapshot; never truncate financial state."""
        if (
            type(scope) is not Scope
            or type(limit) is not int
            or not 1 <= limit <= 10000
        ):
            raise ValueError("trusted scope and bounded recovery limit required")
        with self._connect() as db:
            db.execute("BEGIN")
            ids = db.execute(
                "SELECT trade_id FROM revision_heads WHERE scope=? UNION SELECT trade_id FROM revision_events WHERE scope=? UNION SELECT trade_id FROM revision_outbox WHERE scope=? ORDER BY trade_id LIMIT ?",
                (scope.key, scope.key, scope.key, limit + 1),
            ).fetchall()
            if len(ids) > limit:
                raise JournalIntegrityError(
                    "recovery exceeds limit; no partial financial snapshot"
                )
            self._scope_receipts(db, scope.key)
            return [self._head(db, scope.key, trade_id) for (trade_id,) in ids]

    def append(
        self,
        writer: Writer,
        *,
        trade_id: str,
        event_id: str,
        expected_revision: int,
        payload: dict,
    ) -> AppendResult:
        identifier(trade_id)
        identifier(event_id)
        integer(expected_revision)
        text = canonical(payload)
        if AMENDMENT_KEY in payload:
            raise ValueError(
                "financial metadata requires the explicit amendment command"
            )
        if (
            payload.get("trade_id") != trade_id
            or type(payload.get("state")) is not str
            or payload.get("state")
            not in {
                "position_opened",
                "exit_triggered",
                "position_closed",
            }
        ):
            raise ValueError("matching identity and recognized state required")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, writer)
            key = writer.scope.key
            previous = db.execute(
                "SELECT * FROM revision_events WHERE scope=? AND event_id=?",
                (key, event_id),
            ).fetchone()
            current = self._head(db, key, trade_id)
            current_revision = current.revision if current else 0
            if previous:
                original = self._decode(previous)
                if original.trade_id != trade_id or original.payload_json != text:
                    raise RevisionConflict(
                        "event identity reused for different business payload"
                    )
                return AppendResult(original, current_revision, True)
            if expected_revision != current_revision:
                raise RevisionConflict("revision compare-and-swap failed")
            if current_revision == MAX_INTEGER:
                raise RevisionConflict("revision exhausted")
            if current and current.payload["state"] == "position_closed":
                raise RevisionConflict(
                    "terminal snapshot is immutable; explicit amendment protocol required"
                )
            revision = current_revision + 1
            event = Revision(
                key,
                trade_id,
                revision,
                event_id,
                writer.writer_id,
                writer.generation,
                text,
                digest,
            )
            self._insert(db, event)
            return AppendResult(event, revision, False)

    @staticmethod
    def _insert(db, event):
        db.execute(
            "INSERT INTO revision_events VALUES(?,?,?,?,?,?,?,?)",
            tuple(event.__dict__.values()),
        )
        db.execute(
            "INSERT INTO revision_heads VALUES(?,?,?) ON CONFLICT(scope,trade_id) DO UPDATE SET revision=excluded.revision",
            (event.scope_key, event.trade_id, event.revision),
        )
        db.execute(
            "INSERT INTO revision_outbox(scope,trade_id,revision) VALUES(?,?,?)",
            (event.scope_key, event.trade_id, event.revision),
        )

    def amend_closed(
        self,
        writer: Writer,
        *,
        trade_id: str,
        event_id: str,
        expected_revision: int,
        expected_digest: str,
        amendment: CostAmendment,
    ) -> AppendResult:
        """Paper-only command; never accepts an arbitrary replacement snapshot.

        Source/receipt identity is unique within the trusted scope. An exact retry
        must reuse its event ID, parent revision/digest and command bytes.
        """
        if (
            type(writer) is not Writer
            or writer.scope.mode != "paper"
            or type(amendment) is not CostAmendment
        ):
            raise ValueError("explicit paper writer and cost amendment required")
        identifier(trade_id)
        identifier(event_id)
        integer(expected_revision, 1)
        metadata = amendment.metadata(expected_revision, expected_digest)
        canonical(metadata)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, writer)
            key = writer.scope.key
            receipts = self._scope_receipts(db, key)
            current = self._head(db, key, trade_id)
            previous = db.execute(
                "SELECT * FROM revision_events WHERE scope=? AND event_id=?",
                (key, event_id),
            ).fetchone()
            if previous:
                original = self._decode(previous)
                if original.trade_id != trade_id or canonical(
                    original.payload.get(AMENDMENT_KEY, {})
                ) != canonical(metadata):
                    raise RevisionConflict(
                        "amendment event ID reused for a different command"
                    )
                return AppendResult(original, current.revision if current else 0, True)
            if (
                current is None
                or current.revision != expected_revision
                or current.digest != expected_digest
            ):
                raise RevisionConflict("amendment parent compare-and-swap failed")
            if current.revision == MAX_INTEGER:
                raise RevisionConflict("revision exhausted")
            for recorded in receipts:
                if recorded and (
                    (
                        recorded["source"] == amendment.source
                        and recorded["adjustment_id"] == amendment.adjustment_id
                    )
                    or recorded["receipt_sha256"] == amendment.receipt_sha256
                ):
                    raise RevisionConflict(
                        "receipt already recorded; reuse the original event ID"
                    )
            from aitos.trading.paper_revision import decode_trade

            decode_trade(current.payload)
            try:
                payload = apply_amendment(current.payload, metadata)
                decode_trade(payload)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise RevisionConflict(
                    "amendment requires valid closed evidence and a new receipt"
                ) from exc
            text = canonical(payload)
            event = Revision(
                key,
                trade_id,
                current.revision + 1,
                event_id,
                writer.writer_id,
                writer.generation,
                text,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            self._insert(db, event)
            return AppendResult(event, event.revision, False)

    def pending(self, scope: Scope, *, limit=100) -> list[Revision]:
        if type(scope) is not Scope:
            raise ValueError("trusted scope required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded outbox batch required")
        with self._connect() as db:
            db.execute("BEGIN")
            # Prototype integrity pass, not a production latency/capacity claim.
            self._scope_receipts(db, scope.key)
            identities = db.execute(
                "SELECT trade_id FROM revision_events WHERE scope=? UNION SELECT trade_id FROM revision_heads WHERE scope=? UNION SELECT trade_id FROM revision_outbox WHERE scope=?",
                (scope.key, scope.key, scope.key),
            ).fetchall()
            for (trade_id,) in identities:
                self._head(db, scope.key, trade_id)
            result = []
            for row in db.execute(
                "SELECT e.* FROM revision_events e JOIN revision_outbox o USING(scope,trade_id,revision) WHERE o.scope=? AND o.delivered=0 ORDER BY e.trade_id,e.revision LIMIT ?",
                (scope.key, limit),
            ):
                event = self._decode(row)
                result.append(event)
            return result

    def outbox_counts(self, scope: Scope):
        if type(scope) is not Scope:
            raise ValueError("trusted scope required")
        with self._connect() as db:
            db.execute("BEGIN")
            self._scope_receipts(db, scope.key)
            ids = db.execute(
                "SELECT trade_id FROM revision_events WHERE scope=? UNION SELECT trade_id FROM revision_heads WHERE scope=? UNION SELECT trade_id FROM revision_outbox WHERE scope=?",
                (scope.key, scope.key, scope.key),
            ).fetchall()
            for (trade_id,) in ids:
                self._head(db, scope.key, trade_id)
            total, delivered = db.execute(
                "SELECT count(*),coalesce(sum(delivered),0) FROM revision_outbox WHERE scope=?",
                (scope.key,),
            ).fetchone()
            return dict(total=total, pending=total - delivered, delivered=delivered)

    def acknowledge(self, writer: Writer, event: Revision):
        self.acknowledge_batch(writer, [event])

    def acknowledge_batch(self, writer: Writer, events):
        """Atomic local ACK after sink verification; NOT remote exactly once."""
        if (
            type(events) is not list
            or not 1 <= len(events) <= 100
            or any(type(event) is not Revision for event in events)
        ):
            raise ValueError("bounded committed evidence batch required")
        keys = {(event.scope_key, event.trade_id, event.revision) for event in events}
        if len(keys) != len(events):
            raise ValueError("duplicate acknowledgement key")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, writer)
            self._scope_receipts(db, writer.scope.key)
            checked = set()
            for event in events:
                if event.scope_key != writer.scope.key:
                    raise RevisionConflict("outbox scope mismatch")
                if event.trade_id not in checked:
                    self._head(db, event.scope_key, event.trade_id)
                    checked.add(event.trade_id)
                actual = db.execute(
                    "SELECT * FROM revision_events WHERE scope=? AND trade_id=? AND revision=?",
                    (event.scope_key, event.trade_id, event.revision),
                ).fetchone()
                if self._decode(actual) != event:
                    raise JournalIntegrityError(
                        "outbox acknowledgement evidence mismatch"
                    )
            for event in events:
                if (
                    db.execute(
                        "UPDATE revision_outbox SET delivered=1 WHERE scope=? AND trade_id=? AND revision=?",
                        (event.scope_key, event.trade_id, event.revision),
                    ).rowcount
                    != 1
                ):
                    raise JournalIntegrityError("missing outbox receipt")


# Declaration only. Never executed by RevisionJournal or any application runner.
PROJECTION_DDL = """
CREATE TABLE IF NOT EXISTS prototype_revision_evidence (
    scope_key String, trade_id String, revision UInt64, event_id String,
    writer_id String, generation UInt64, payload_json String, digest String
) ENGINE = MergeTree ORDER BY (scope_key,trade_id,revision,event_id)
"""


def verify_projection_batch(records):
    """Validate a bounded sink batch; NOT complete-history recovery certification.

    Exact retries may repeat. A revision or scoped event ID must not disagree.
    The caller must inspect sink acceptance before acknowledging the local outbox.
    """
    revisions = {}
    events = {}
    for count, record in enumerate(records, 1):
        if count > 1000 or type(record) is not Revision:
            raise ValueError("bounded revision evidence batch required")
        checked = RevisionJournal._decode(tuple(record.__dict__.values()))
        revision_key = (checked.scope_key, checked.trade_id, checked.revision)
        event_key = (checked.scope_key, checked.event_id)
        if (revision_key in revisions and revisions[revision_key] != checked) or (
            event_key in events and events[event_key] != checked
        ):
            raise JournalIntegrityError("conflicting projection evidence")
        revisions[revision_key] = checked
        events[event_key] = checked
    return list(revisions.values())
