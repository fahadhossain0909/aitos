"""Cross-boundary fail-closed regressions; all broker calls are offline mocks."""

import asyncio
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from aitos.capital.router import CapitalRouter
from aitos.execution.binance_executor import (
    BinanceAPIError,
    BinanceFuturesOrderExecutor,
)
from aitos.execution.ctrader_executor import CTraderOrderExecutor
from aitos.execution.intent_store import (
    ExecutionIntentStore,
    ExecutionRecoveryRequired,
    ExecutionUncertainError,
)
from aitos.execution.mt5_gateway import MT5GatewayOrderExecutor
from aitos.execution.order_executor import OrderRequest, SimulatedOrderExecutor
from aitos.execution.prop_adapters import TradeLockerOrderExecutor
from aitos.intelligence.capital_protection import CapitalReservation, Reservation
from aitos.live_trading import create_live_capital_gateway
from aitos.models.trade import TradeSide
from aitos.runtime_ownership import RuntimeOwnership


def request():
    return OrderRequest(
        "BTCUSDT", TradeSide.LONG, 1, 100, client_order_id="stable-intent"
    )


def _claim(args):
    path, scope = args
    store = ExecutionIntentStore(path, scope=scope)
    try:
        store.begin(request())
        return "claimed"
    except ExecutionRecoveryRequired:
        return "blocked"


def test_sqlite_serializes_competing_processes(tmp_path):
    path = tmp_path / "processes.sqlite3"
    ExecutionIntentStore(path, scope="scope")
    with ProcessPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_claim, [(str(path), "scope")] * 4))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("blocked") == 3


def test_pre_submit_record_survives_hard_process_exit(tmp_path):
    path = tmp_path / "hard-exit.sqlite3"
    script = """
import os, sys
from aitos.execution.intent_store import ExecutionIntentStore
from aitos.execution.order_executor import OrderRequest
from aitos.models.trade import TradeSide
ExecutionIntentStore(sys.argv[1], scope='scope').begin(
    OrderRequest('BTCUSDT', TradeSide.LONG, 1, 100, client_order_id='before-exit'))
os._exit(73)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], check=False, timeout=20
    )
    assert result.returncode == 73
    with pytest.raises(ExecutionRecoveryRequired):
        ExecutionIntentStore(path, scope="scope").assert_ready()


def test_live_runtime_ownership_blocks_other_process_and_releases(tmp_path):
    path = tmp_path / "runtime.lock"
    owner = RuntimeOwnership(path)
    owner.acquire()
    code = "from aitos.runtime_ownership import RuntimeOwnership; import sys; RuntimeOwnership(sys.argv[1]).acquire()"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode != 0
        assert "another live runtime" in result.stderr
    finally:
        owner.release()
    successor = RuntimeOwnership(path)
    successor.acquire()
    successor.release()
    successor.release()


def test_stable_live_capital_owner_can_release_after_restart():
    scope = "a" * 64
    old = create_live_capital_gateway(scope)
    old.reservation.restore_confirmed_exposure(Reservation("BTCUSDT", 100, 10))
    old.reservation.close()
    old.calibrator.close()
    new = create_live_capital_gateway(scope)
    try:
        assert new.reservation.get("BTCUSDT") == Reservation("BTCUSDT", 100, 10)
        assert new.release("BTCUSDT") is not None
    finally:
        new.reservation.close()
        new.calibrator.close()


def test_restore_cannot_steal_another_owners_capital(tmp_path):
    path = tmp_path / "capital.sqlite3"
    first = CapitalReservation(path, owner_id="first")
    second = CapitalReservation(path, owner_id="second")
    try:
        first.restore_confirmed_exposure(Reservation("BTCUSDT", 100, 10))
        with pytest.raises(ValueError, match="another owner"):
            second.restore_confirmed_exposure(Reservation("BTCUSDT", 100, 10))
        assert first.get("BTCUSDT") is not None
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    "error",
    [
        ExecutionUncertainError("BTCUSDT", "stable-intent", "timeout"),
        TimeoutError("unknown"),
    ],
)
async def test_live_router_never_fails_over_after_uncertain_submission(error):
    class Live(SimulatedOrderExecutor):
        requires_exchange_execution = True

    first = Live()
    first.submit_order = AsyncMock(side_effect=error)
    second = SimulatedOrderExecutor()
    second.submit_order = AsyncMock()
    router = CapitalRouter()
    router._venues = {"a": (first, None, None), "b": (second, None, None)}
    router.candidates = lambda *args: [
        type("Quote", (), {"venue": name}) for name in ("a", "b")
    ]
    router.compliance.evaluate = lambda *args: type("Decision", (), {"allowed": True})
    with pytest.raises(ExecutionUncertainError):
        await router.submit_best(request())
    second.submit_order.assert_not_called()


@pytest.mark.parametrize(
    "kind", [CTraderOrderExecutor, MT5GatewayOrderExecutor, TradeLockerOrderExecutor]
)
async def test_unimplemented_position_bound_reductions_fail_before_broker_access(kind):
    value = object.__new__(kind)
    assert value.requires_exchange_execution
    with pytest.raises(NotImplementedError, match="reduction"):
        await value.submit_order(replace(request(), reduce_only=True))


@pytest.mark.parametrize(
    "payload",
    [
        dict(orderId=1, status="CANCELED"),
        dict(orderId=1, status="FILLED", executedQty=True, avgPrice="100"),
        dict(orderId=1, status="FILLED", executedQty="1", avgPrice=True),
    ],
)
async def test_missing_or_boolean_execution_evidence_stays_uncertain(payload):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(return_value=payload)
    with pytest.raises(ExecutionUncertainError):
        await value.submit_order(request())


@pytest.mark.parametrize(
    "evidence", [dict(status="NEW"), dict(orderId=2, status="CANCELED"), None]
)
async def test_cancel_ack_or_wrong_identity_is_not_terminal_proof(evidence):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value.cancel_order = AsyncMock(return_value=evidence)
    with pytest.raises(ExecutionUncertainError):
        await value.cancel_resting_order("BTCUSDT", "1")


async def test_permission_error_is_not_swallowed_as_already_cancelled():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value.cancel_order = AsyncMock(
        side_effect=BinanceAPIError("Binance API error -2015: permission")
    )
    with pytest.raises(BinanceAPIError):
        await value.cancel_resting_order("BTCUSDT", "1")


async def test_ctrader_connection_wait_does_not_block_event_loop():
    import threading

    value = object.__new__(CTraderOrderExecutor)
    value._symbol_ids = {"BTCUSDT": 1}
    value._timeout = 0.5
    release = threading.Event()
    done = asyncio.Event()

    class Connection:
        def wait(self, timeout):
            release.wait(timeout)
            return False

    value._connected = Connection()

    async def submit():
        try:
            with pytest.raises(RuntimeError, match="connection timeout"):
                await value.submit_order(request())
        finally:
            done.set()

    async def heartbeat():
        await asyncio.sleep(0)
        responsive = not done.is_set()
        release.set()
        assert responsive

    await asyncio.gather(submit(), heartbeat())


async def test_private_transport_error_does_not_expose_signed_url():
    from types import SimpleNamespace
    from aitos.execution.binance_executor import BinanceTransportError

    class Fault:
        async def __aenter__(self):
            raise OSError("private URL ?signature=do-not-log-this")

        async def __aexit__(self, *args):
            return False

    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._session = SimpleNamespace(request=lambda *args, **kwargs: Fault())
    with pytest.raises(BinanceTransportError) as caught:
        await value._signed_request("GET", "/fapi/v1/order", {})
    assert "signature" not in str(caught.value)
    assert caught.value.__suppress_context__
