"""OrderExecutor facade that routes each order through CapitalRouter."""

from __future__ import annotations

from aitos.capital.router import CapitalRouter
from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult


class RoutedOrderExecutor(OrderExecutor):
    """Keep TradeLifecycle unaware of exchange/prop-firm routing."""

    def __init__(self, router: CapitalRouter, projected_loss_fn=None) -> None:
        self._router = router
        self._projected_loss_fn = projected_loss_fn or (lambda _: 0.0)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        _, result = await self._router.submit_best(
            request, projected_loss=float(self._projected_loss_fn(request))
        )
        return result

    @property
    def supports_exchange_side_stops(self) -> bool:
        return self._router.supports_exchange_side_stops

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """Delegate to the router so callers (e.g. ``PersistentLivePortfolioTracker
        .refresh_equity()``) don't hit an ``AttributeError`` the moment
        multi-venue capital routing is wired into a live equity-tracking
        runner -- ``OrderExecutor`` has no ``get_account_balance`` in its
        base contract, and this class previously had no fallback for it at
        all (unlike ``IdempotentOrderExecutor``'s ``__getattr__``)."""
        return await self._router.get_account_balance(asset)
