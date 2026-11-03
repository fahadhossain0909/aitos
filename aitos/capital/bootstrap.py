"""Bootstrap helpers for enabling multi-capital execution without changing strategies."""

from __future__ import annotations

from aitos.capital.routed_executor import RoutedOrderExecutor
from aitos.capital.router import CapitalRouter
from aitos.execution.order_executor import OrderExecutor


def wrap_executor(
    executor: OrderExecutor,
    router: CapitalRouter | None,
) -> OrderExecutor:
    """Return a routed executor when capital routing is configured.

    Keeping this decision at application bootstrap means the existing
    TradeLifecycle and strategy interfaces remain unchanged.
    """
    if router is None:
        return executor
    return RoutedOrderExecutor(router)
