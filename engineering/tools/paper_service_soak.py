"""Opt-in two-process real ClickHouse restart/recovery rehearsal.

Creates a unique synthetic database on 127.0.0.1 only. Never drops data or sends
venue requests. Requires explicit dedicated-service acknowledgement. Not HA proof.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aitos.trading.revision_journal import PROJECTION_DDL, RevisionJournal, Scope
from aitos.trading.revision_projector import ClickHouseEvidenceSink, RevisionProjector


def guard(environment):
    if environment.get("AITOS_SERVICE_TEST_ACK") != "isolated-test-services-only":
        raise ValueError("dedicated disposable service acknowledgement required")
    port = int(environment.get("AITOS_SERVICE_TEST_PORT", "18123"))
    if not 1024 <= port <= 65535:
        raise ValueError("dedicated high port required")
    return port


def require(condition):
    if condition is not True:
        raise RuntimeError(
            "Rehearsal invariant failed; not a passing recovery certificate"
        )


def evidence_metadata(phase):
    commit = os.environ.get("GITHUB_SHA", "")
    return dict(
        evidence_schema_version=1,
        phase=phase,
        source_commit=(
            commit if re.fullmatch("[a-f0-9]{40}", commit) else "not_recorded"
        ),
        observed_at=datetime.now(timezone.utc).isoformat(),
        synthetic=True,
    )


async def run(phase, root, seconds):
    if phase not in ("seed", "probe-offline", "recover-soak"):
        raise ValueError("recognized explicit phase required")
    port = guard(os.environ)
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("bounded soak duration required")
    import clickhouse_connect

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "synthetic-manifest.json"
    if phase == "seed":
        if manifest.exists() or (root / "journal.sqlite").exists():
            raise ValueError("fresh test directory required")
        database = "aitos_test_" + uuid.uuid4().hex
        admin = await clickhouse_connect.get_async_client(host="127.0.0.1", port=port)
        try:
            await admin.command(
                "CREATE DATABASE {database:Identifier}",
                parameters={"database": database},
            )
        finally:
            await admin.close()
        manifest.write_text(json.dumps({"database": database}))
    else:
        database = json.loads(manifest.read_text())["database"]
        if (
            not isinstance(database, str)
            or not database.startswith("aitos_test_")
            or len(database) != 43
        ):
            raise ValueError("owned test database required")
        if any(c not in "0123456789abcdef" for c in database[11:]):
            raise ValueError("owned test database required")
    if phase == "probe-offline":
        import socket

        journal = RevisionJournal(root / "journal.sqlite")
        scope = Scope("paper", "synthetic", "service-test", database)
        before = journal.outbox_counts(scope)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                raise RuntimeError("offline probe unexpectedly connected")
        except (ConnectionRefusedError, ConnectionResetError, TimeoutError):
            pass
        after = journal.outbox_counts(scope)
        require(
            before == after and after["pending"] == 10 and (after["delivered"] == 0)
        )
        (root / "probe-offline-report.json").write_text(
            json.dumps(
                dict(
                    **evidence_metadata(phase),
                    status="transport_unavailable_pending_preserved",
                    counts=after,
                    live_authorized=False,
                    production_certified=False,
                ),
                indent=2,
            )
        )
        return
    client = await clickhouse_connect.get_async_client(
        host="127.0.0.1", port=port, database=database
    )
    try:
        version_rows = (await client.query("SELECT version()")).result_rows
        require(len(version_rows) == 1 and len(version_rows[0]) == 1)
        version = version_rows[0][0]
        require(
            type(version) is str
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,5}", version) is not None
        )
        metadata = json.loads(manifest.read_text())
        if phase == "seed":
            metadata["server_version"] = version
            manifest.write_text(json.dumps(metadata))
        else:
            require(metadata.get("server_version") == version)
        journal = RevisionJournal(root / "journal.sqlite")
        scope = Scope("paper", "synthetic", "service-test", database)
        if phase == "seed":
            await client.command(PROJECTION_DDL)
            writer = journal.acquire(scope, "seed-worker", expected_generation=0)
            for i in range(10):
                journal.append(
                    writer,
                    trade_id=f"test-{i}",
                    event_id=f"event-{i}",
                    expected_revision=0,
                    payload={
                        "trade_id": f"test-{i}",
                        "state": "position_opened",
                        "quantity": 1,
                    },
                )

            def lost_ack(*args):
                raise OSError("synthetic pre-ACK interruption")

            journal.acknowledge_batch = lost_ack
            result = await RevisionProjector(
                journal, writer, ClickHouseEvidenceSink(client)
            ).run_once()
            require(result["status"] == "failed" and len(journal.pending(scope)) == 10)
        else:
            from aitos.trading.revision_journal import Writer

            old = Writer(scope, "seed-worker", 1)
            writer = journal.acquire(scope, "replacement-worker", expected_generation=1)
            stale = await RevisionProjector(
                journal, old, ClickHouseEvidenceSink(client)
            ).run_once()
            require(stale["status"] == "fenced")
            projector = RevisionProjector(
                journal, writer, ClickHouseEvidenceSink(client)
            )
            result = await projector.run_once()
            require(
                result["status"] == "verified"
                and result["last_batch"]
                == dict(checked=10, inserted=0, acknowledged=10)
            )
            count = (
                await client.query("SELECT count() FROM prototype_revision_evidence")
            ).result_rows[0][0]
            require(count == 10 and (not journal.pending(scope)))
            started = time.monotonic()
            iterations = 0
            while time.monotonic() - started < seconds:
                # Keep exercising new remote writes for the entire window. Cycling
                # IDs here turns every iteration after 100 into a local replay.
                i = iterations
                journal.append(
                    writer,
                    trade_id=f"soak-{i}",
                    event_id=f"soak-event-{i}",
                    expected_revision=0,
                    payload={
                        "trade_id": f"soak-{i}",
                        "state": "position_opened",
                        "quantity": 1,
                    },
                )
                report = await projector.run_once()
                require(
                    report["available"]
                    and report["counts"]["pending"] == 0
                    and report["last_batch"]
                    == dict(checked=1, inserted=1, acknowledged=1)
                )
                iterations += 1
                await asyncio.sleep(0.1)
            count = (
                await client.query("SELECT count() FROM prototype_revision_evidence")
            ).result_rows[0][0]
            require(count == 10 + iterations)
            result = {
                "status": "recovered_and_soaked",
                "workload": "continuous_unique_events",
                "iterations": iterations,
                "elapsed_seconds": time.monotonic() - started,
                "remote_rows": count,
                "live_authorized": False,
                "production_certified": False,
            }
        result.update(evidence_metadata(phase), server_version=version)
        (root / f"{phase}-report.json").write_text(json.dumps(result, indent=2))
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["seed", "probe-offline", "recover-soak"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(run(args.phase, args.root, args.seconds))


if __name__ == "__main__":
    main()
