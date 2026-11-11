"""Local intent identity and async ownership; no exchange IO."""

import asyncio
from dataclasses import replace

import pytest

from aitos.execution.order_executor import OrderRequest, SimulatedOrderExecutor
from aitos.models.trade import TradeSide
from aitos.trading.persistent_state import IdempotentOrderExecutor


def request(**overrides):
    values = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        reference_price=100.0,
        client_order_id="intent-a",
    )
    values.update(overrides)
    return OrderRequest(**values)


class BlockingExecutor(SimulatedOrderExecutor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def submit_order(self, value):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().submit_order(value)


@pytest.mark.parametrize("cancel_owner", [True, False])
async def test_cancelling_one_waiter_does_not_cancel_shared_submission(cancel_owner):
    inner = BlockingExecutor()
    wrapper = IdempotentOrderExecutor(inner)
    owner = asyncio.create_task(wrapper.submit_order(request()))
    await asyncio.wait_for(inner.started.wait(), 1)
    waiter = asyncio.create_task(wrapper.submit_order(request()))
    await asyncio.sleep(0)
    cancelled, survivor = (owner, waiter) if cancel_owner else (waiter, owner)
    try:
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not inner.cancelled
        inner.release.set()
        result = await asyncio.wait_for(survivor, 1)
        repeated = await wrapper.submit_order(request())
        assert repeated == result
        assert inner.calls == 1
    finally:
        inner.release.set()
        for task in (owner, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, waiter, return_exceptions=True)


async def test_submission_finishes_and_caches_even_when_only_waiter_cancelled():
    inner = BlockingExecutor()
    wrapper = IdempotentOrderExecutor(inner)
    owner = asyncio.create_task(wrapper.submit_order(request()))
    await asyncio.wait_for(inner.started.wait(), 1)
    try:
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert "intent-a" in wrapper._inflight
        inner.release.set()
        for _ in range(100):
            if "intent-a" in wrapper._completed:
                break
            await asyncio.sleep(0)
        assert "intent-a" in wrapper._completed
        assert "intent-a" not in wrapper._inflight
        await wrapper.submit_order(request())
        assert inner.calls == 1
    finally:
        inner.release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 2.0},
        {"reduce_only": True},
        {"symbol": "ETHUSDT"},
        {"reference_price": 101.0},
        {"side": TradeSide.SHORT},
    ],
)
async def test_reused_client_id_with_different_request_is_rejected(changes):
    wrapper = IdempotentOrderExecutor(SimulatedOrderExecutor())
    await wrapper.submit_order(request())
    with pytest.raises(ValueError, match="different request"):
        await wrapper.submit_order(replace(request(), **changes))


async def test_inflight_id_conflict_is_rejected_before_second_submit():
    inner = BlockingExecutor()
    wrapper = IdempotentOrderExecutor(inner)
    owner = asyncio.create_task(wrapper.submit_order(request()))
    await asyncio.wait_for(inner.started.wait(), 1)
    try:
        with pytest.raises(ValueError, match="different request"):
            await wrapper.submit_order(request(quantity=2.0))
        assert inner.calls == 1
    finally:
        inner.release.set()
        await owner


async def test_fallback_identity_includes_reduce_only_and_full_float_precision():
    wrapper = IdempotentOrderExecutor(SimulatedOrderExecutor())
    base = request(client_order_id=None, quantity=1.0000000000001)
    variants = [
        base,
        replace(base, reduce_only=True),
        replace(base, quantity=1.0000000000002),
    ]
    results = [await wrapper.submit_order(value) for value in variants]
    assert len({value.order_id for value in results}) == 3


async def test_distinct_explicit_intents_with_identical_economics_remain_distinct():
    wrapper = IdempotentOrderExecutor(SimulatedOrderExecutor())
    first = await wrapper.submit_order(request())
    second = await wrapper.submit_order(request(client_order_id="intent-b"))
    assert first.order_id != second.order_id


async def test_lifecycle_provides_stable_logical_entry_id(event_bus, risk_engine):
    from aitos.models.trade import Opportunity
    from aitos.risk.models import PortfolioState
    from aitos.trading.lifecycle import TradeLifecycle

    class RecordingExecutor(SimulatedOrderExecutor):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def submit_order(self, value):
            self.requests.append(value)
            return await super().submit_order(value)

    executor = RecordingExecutor()
    lifecycle = TradeLifecycle(event_bus, risk_engine, order_executor=executor)
    opportunity = Opportunity(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="audit",
        rationale="offline intent identity",
    )
    portfolio = PortfolioState(equity_usd=10000.0, peak_equity_usd=10000.0)
    first = await lifecycle._validate_and_open(opportunity, portfolio)
    second = await lifecycle._validate_and_open(opportunity, portfolio)
    await lifecycle._validate_and_open(
        replace(opportunity, opportunity_id="distinct-opportunity"), portfolio
    )
    ids = [value.client_order_id for value in executor.requests]
    assert all(value and len(value) <= 36 for value in ids)
    assert ids[0] == ids[1]
    assert ids[0] == f"aitos-entry-{first.trade_id}"
    assert ids[1] == f"aitos-entry-{second.trade_id}"
    assert ids[2] != ids[0]


async def test_orphaned_submission_failure_is_retrieved_and_not_cached():
    """Detached task ownership includes failures, not just successful caching."""
    import gc

    class FailingOnce(BlockingExecutor):
        async def submit_order(self, value):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            if self.calls == 1:
                raise RuntimeError("isolated submit failure")
            return await SimulatedOrderExecutor.submit_order(self, value)

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    inner = FailingOnce()
    wrapper = IdempotentOrderExecutor(inner)
    owner = asyncio.create_task(wrapper.submit_order(request()))
    try:
        await asyncio.wait_for(inner.started.wait(), 1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        inner.release.set()

        async def wait_for_settlement():
            while wrapper._inflight:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_settlement(), 1)
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
        assert not wrapper._completed
        # Same identity may retry; this is an in-memory contract, NOT proof
        # that retrying an exchange timeout is safe without reconciliation.
        assert (await wrapper.submit_order(request())).success
        assert inner.calls == 2
        with pytest.raises(ValueError):
            await wrapper.submit_order(request(quantity=2))
    finally:
        inner.release.set()
        loop.set_exception_handler(previous)


@pytest.mark.parametrize("field", ["quantity", "reference_price"])
@pytest.mark.parametrize(
    "value", [0, -1, float("nan"), float("inf"), float("-inf"), True, "1.0", None]
)
async def test_invalid_economics_fail_before_identity_registration_or_submission(
    field, value
):
    inner = BlockingExecutor()
    inner.release.set()
    wrapper = IdempotentOrderExecutor(inner)
    with pytest.raises(ValueError, match=field):
        await wrapper.submit_order(request(**{field: value}))
    assert inner.calls == 0
    assert not wrapper._requests
    # Rejection cannot poison the valid logical intent with the same ID.
    assert (await wrapper.submit_order(request())).success


class RecoveryStore:
    def __init__(self):
        self.saved = []

    async def save_order_result(self, *args):
        self.saved.append(args)


class LookupExecutor(SimulatedOrderExecutor):
    def __init__(self, evidence):
        super().__init__()
        self.evidence = evidence

    async def get_order_by_client_id(self, symbol, client_order_id):
        return self.evidence


def recovery_evidence(**overrides):
    value = dict(
        symbol="BTCUSDT",
        side="BUY",
        clientOrderId="intent-a",
        orderId=123,
        status="PARTIALLY_FILLED",
        executedQty="0.5",
        avgPrice="101",
    )
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"clientOrderId": "another-intent"},
        {"executedQty": "1.01"},
        {"executedQty": "-0.01"},
        {"executedQty": "NaN"},
        {"executedQty": "Infinity"},
        {"avgPrice": "0"},
        {"avgPrice": "Infinity"},
        {"orderId": ""},
    ],
)
async def test_ambiguous_recovery_rejects_unbound_or_invalid_fill_evidence(overrides):
    from aitos.trading.persistent_state import AmbiguousOrderSubmissionError

    store = RecoveryStore()
    wrapper = IdempotentOrderExecutor(
        LookupExecutor(recovery_evidence(**overrides)), store
    )
    with pytest.raises(AmbiguousOrderSubmissionError):
        await wrapper._recover_ambiguous("intent-a", request())
    # Invalid evidence must leave the durable SUBMITTING record untouched.
    assert store.saved == []


async def test_ambiguous_recovery_persists_only_identity_bound_partial_fill():
    store = RecoveryStore()
    wrapper = IdempotentOrderExecutor(LookupExecutor(recovery_evidence()), store)
    result = await wrapper._recover_ambiguous("intent-a", request())
    assert result.success and result.filled_quantity == 0.5
    assert result.fill_price == 101
    assert len(store.saved) == 1
    assert store.saved[0][0:2] == ("intent-a", "BTCUSDT")


class DurableReplayStore:
    def __init__(self, record):
        self.record = record

    async def load_order_record(self, client_order_id):
        return self.record

    async def save_order_intent(self, *args):
        raise AssertionError("existing durable identity must not be resubmitted")

    async def save_order_result(self, *args):
        raise AssertionError("conflicting evidence must not be overwritten")


async def test_durable_ack_cannot_be_replayed_for_changed_request_after_restart():
    from aitos.trading.persistent_state import AmbiguousOrderSubmissionError

    original = request()
    stored = IdempotentOrderExecutor._request_payload(original)
    stored["quantity"] = 0.5
    record = {
        "symbol": "BTCUSDT",
        "state": "ACKNOWLEDGED",
        "request": stored,
        "result": {
            "order_id": "old-order",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "filled_quantity": 0.5,
            "fill_price": 101,
            "success": True,
        },
    }
    inner = BlockingExecutor()
    wrapper = IdempotentOrderExecutor(inner, DurableReplayStore(record))
    with pytest.raises(
        AmbiguousOrderSubmissionError, match="different durable request"
    ):
        await wrapper._submit_with_recovery("intent-a", original)
    assert inner.calls == 0
    assert wrapper._blocked_client_order_id == "intent-a"


@pytest.mark.parametrize(
    "result",
    [
        {
            "order_id": "old-order",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "filled_quantity": 2,
            "fill_price": 101,
            "success": True,
        },
        {
            "order_id": "",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "filled_quantity": 1,
            "fill_price": 101,
            "success": True,
        },
        {
            "order_id": "old-order",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "filled_quantity": 1,
            "fill_price": float("nan"),
            "success": True,
        },
        {},
    ],
)
async def test_corrupt_acknowledged_result_never_releases_durable_recovery(result):
    from aitos.trading.persistent_state import AmbiguousOrderSubmissionError

    original = request()
    record = {
        "symbol": "BTCUSDT",
        "state": "ACKNOWLEDGED",
        "request": IdempotentOrderExecutor._request_payload(original),
        "result": result,
    }
    inner = BlockingExecutor()
    wrapper = IdempotentOrderExecutor(inner, DurableReplayStore(record))
    with pytest.raises(AmbiguousOrderSubmissionError, match="invalid durable terminal"):
        await wrapper._submit_with_recovery("intent-a", original)
    assert inner.calls == 0
    assert wrapper._blocked_client_order_id == "intent-a"
