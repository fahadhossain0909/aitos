from __future__ import annotations

import pytest

from aitos.capital.models import (
    AccountMode,
    AccountSnapshot,
    PropFirmProfile,
    PropRuleSet,
)
from aitos.capital.routed_executor import RoutedOrderExecutor
from aitos.capital.router import CapitalRouter


def _profile() -> PropFirmProfile:
    return PropFirmProfile(
        provider="test-prop",
        platform="api",
        account_mode=AccountMode.SIMULATED_FUNDED,
        live_api=False,
        rules=PropRuleSet(),
    )


def _account(account_id: str) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=account_id,
        provider="test-prop",
        equity=100_000,
        balance=100_000,
        day_start_equity=100_000,
        high_water_mark=100_000,
    )


class _BalanceExecutor:
    def __init__(self, balance: float) -> None:
        self._balance = balance

    async def get_account_balance(self, asset: str = "USDT") -> float:
        return self._balance


class _NoBalanceExecutor:
    """Simulates an executor type that never implements get_account_balance
    (e.g. a paper/simulated executor), matching real OrderExecutor subclasses
    that don't override the (nonexistent) base method."""


class _FailingBalanceExecutor:
    async def get_account_balance(self, asset: str = "USDT") -> float:
        raise RuntimeError("venue API down")


@pytest.mark.asyncio
async def test_router_sums_balances_across_venues():
    router = CapitalRouter()
    router.register("venue-a", _BalanceExecutor(1000.0), _profile(), _account("a"))
    router.register("venue-b", _BalanceExecutor(2500.0), _profile(), _account("b"))

    total = await router.get_account_balance("USDT")

    assert total == 3500.0


@pytest.mark.asyncio
async def test_router_skips_venues_without_balance_support():
    router = CapitalRouter()
    router.register("venue-a", _BalanceExecutor(1000.0), _profile(), _account("a"))
    router.register("venue-b", _NoBalanceExecutor(), _profile(), _account("b"))

    total = await router.get_account_balance("USDT")

    assert total == 1000.0


@pytest.mark.asyncio
async def test_router_survives_one_venue_failing():
    router = CapitalRouter()
    router.register("venue-a", _BalanceExecutor(1000.0), _profile(), _account("a"))
    router.register("venue-b", _FailingBalanceExecutor(), _profile(), _account("b"))

    total = await router.get_account_balance("USDT")

    assert total == 1000.0


@pytest.mark.asyncio
async def test_routed_order_executor_no_longer_crashes_on_get_account_balance():
    router = CapitalRouter()
    router.register("venue-a", _BalanceExecutor(4200.0), _profile(), _account("a"))
    routed = RoutedOrderExecutor(router)

    # Before the fix this raised AttributeError -- RoutedOrderExecutor had
    # no get_account_balance and no __getattr__ fallback to the router.
    balance = await routed.get_account_balance("USDT")

    assert balance == 4200.0
