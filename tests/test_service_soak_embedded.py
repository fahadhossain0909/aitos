"""Rehearse both CLI phases through real embedded SQL, NOT network/service proof."""

import json
import sys
from types import SimpleNamespace

import pytest

from engineering.tools.paper_service_soak import run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "fast_clock", [False, True], ids=["wall-clock", "125-unique-events"]
)
async def test_service_harness_seed_recover_and_soak_against_embedded_sql(
    tmp_path, monkeypatch, fast_clock
):
    pytest.importorskip("chdb")
    from chdb.session import Session

    engine = Session(str(tmp_path / "embedded"))

    class Client:
        async def command(self, sql, parameters=None):
            if sql.startswith("CREATE DATABASE"):
                return  # server-admin transport is NOT verified here
            engine.query(sql)

        async def query(self, sql, parameters=None):
            rows = json.loads(
                str(engine.query(sql, "JSONCompact", params=parameters or {}))
            )["data"]
            if sql.startswith("SELECT count()"):
                rows = [[int(rows[0][0])]]
            return SimpleNamespace(result_rows=rows)

        async def insert(self, table, rows, column_names):
            body = "\n".join(json.dumps(dict(zip(column_names, row))) for row in rows)
            engine.query(
                "INSERT INTO prototype_revision_evidence FORMAT JSONEachRow\n" + body
            )

        async def close(self):
            pass

    async def connect(**kwargs):
        assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 18123
        return Client()

    monkeypatch.setitem(
        sys.modules, "clickhouse_connect", SimpleNamespace(get_async_client=connect)
    )
    monkeypatch.setenv("AITOS_SERVICE_TEST_ACK", "isolated-test-services-only")
    monkeypatch.setenv("AITOS_SERVICE_TEST_PORT", "18123")
    root = tmp_path / "harness"
    try:
        await run("seed", root, 1)
        assert json.loads((root / "seed-report.json").read_text())["status"] == "failed"
        import socket

        def offline(*args, **kwargs):
            raise ConnectionRefusedError("synthetic refused transport")

        with monkeypatch.context() as patch:
            patch.setattr(socket, "create_connection", offline)
            await run("probe-offline", root, 1)
        assert json.loads((root / "probe-offline-report.json").read_text())[
            "counts"
        ] == dict(total=10, pending=10, delivered=0)
        # A module-local fake clock reaches >100 iterations without replacing
        # the event loop's global monotonic clock or claiming wall-clock soak.
        import engineering.tools.paper_service_soak as harness

        ticks = 0

        async def advance(delay):
            nonlocal ticks
            assert delay == 0.1
            ticks += 1

        with monkeypatch.context() as patch:
            if fast_clock:
                patch.setattr(
                    harness, "time", SimpleNamespace(monotonic=lambda: ticks / 125)
                )
                patch.setattr(harness, "asyncio", SimpleNamespace(sleep=advance))
            await run("recover-soak", root, 1)
        report = json.loads((root / "recover-soak-report.json").read_text())
        assert report["status"] == "recovered_and_soaked" and report["iterations"] > 0
        assert report["remote_rows"] == 10 + report["iterations"]
        assert report["workload"] == "continuous_unique_events"
        if fast_clock:
            assert report["iterations"] == 125
        assert report["live_authorized"] is False
        assert report["production_certified"] is False
    finally:
        engine.close()
