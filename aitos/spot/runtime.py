"""Explicit Spot runtime factory; never imported by the Futures live runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from collections.abc import Awaitable, Callable

from aitos.config.paths import get_data_dir
from aitos.config.settings import AITOSSettings, get_settings
from aitos.logging_setup import get_logger
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_protection import CapitalReservation
from aitos.intelligence.exit_intelligence.models import ExitDecision
from aitos.models.trade import Opportunity
from aitos.market_data.binance_spot_adapter import BinanceSpotCanonicalMarketDataAdapter
from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.coordinator import SpotTradingCoordinator
from aitos.spot.execution import SpotOrderExecutor
from aitos.spot.filters import SpotSymbolFilters
from aitos.spot.inventory import InventoryLedger
from aitos.spot.lead_lag import FuturesSpotLeadLagAnalyzer
from aitos.spot.market_state import SpotMarketDataConsumer
from aitos.spot.models import SpotOrder, SpotOrderRequest, SpotOrderSide, SpotOrderType
from aitos.spot.scanner import SpotOpportunityScanner
from aitos.spot.websocket import BinanceSpotWebSocket

logger = get_logger("aitos.spot.runtime")


@dataclass(slots=True)
class SpotRuntime:
    market_data: BinanceSpotCanonicalMarketDataAdapter
    executor: SpotOrderExecutor
    inventory: InventoryLedger
    scanner: SpotOpportunityScanner
    coordinator: SpotTradingCoordinator
    market_consumer: SpotMarketDataConsumer
    lead_lag: FuturesSpotLeadLagAnalyzer
    quote_asset: str = "USDT"
    max_symbols: int = 20
    min_quote_volume: Decimal = Decimal("100000")
    owns_capital_gateway: bool = False
    _market_tasks: list[asyncio.Task] = field(default_factory=list, repr=False)
    quote_volumes_24h: dict[str, Decimal] = field(default_factory=dict)
    _market_stream_health: dict[str, dict[str, object]] = field(
        default_factory=dict, repr=False
    )

    async def eligible_instruments(self):
        """Return the highest-turnover supported Spot symbols for this runtime."""
        return await self.market_data.rest.discover_universe(
            quote_asset=self.quote_asset,
            min_quote_volume=self.min_quote_volume,
            limit=self.max_symbols,
        )

    async def submit_opportunity(
        self,
        *,
        opportunity: Opportunity,
        equity_quote: float,
        quote_order_quantity: Decimal,
    ) -> SpotOrder:
        """Route a shared AITOS Strategy opportunity through Spot capital checks."""
        request = SpotOrderRequest(
            symbol=opportunity.symbol,
            side=SpotOrderSide.BUY,
            order_type=SpotOrderType.MARKET,
            quote_order_quantity=quote_order_quantity,
        )
        return await self.coordinator.submit_entry(
            equity_quote=equity_quote,
            opportunity=opportunity,
            request=request,
        )

    async def submit_exit(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        authorization: ExitDecision,
        client_order_id: str | None = None,
    ) -> SpotOrder:
        return await self.coordinator.submit_exit(
            symbol=symbol,
            quantity=quantity,
            authorization=authorization,
            client_order_id=client_order_id,
        )

    async def publish_trades(self, market_bus, symbols: list[str]) -> None:
        async for event in self.market_data.stream_trades(self._bounded(symbols)):
            await market_bus.publish(event)

    async def publish_tickers(self, market_bus, symbols: list[str]) -> None:
        async for event in self.market_data.stream_tickers(self._bounded(symbols)):
            await market_bus.publish(event)

    async def publish_klines(
        self, market_bus, symbols: list[str], timeframe: str = "1m"
    ) -> None:
        async for event in self.market_data.stream_klines(
            self._bounded(symbols), timeframe
        ):
            await market_bus.publish(event)

    async def publish_order_books(
        self, market_bus, symbols: list[str], levels: int = 20
    ) -> None:
        async for event in self.market_data.stream_order_books(
            self._bounded(symbols), levels
        ):
            await market_bus.publish(event)

    def _bounded(self, symbols: list[str]) -> list[str]:
        values = list(dict.fromkeys(s.upper() for s in symbols if s))
        if not values or len(values) > 100:
            raise ValueError("Spot subscription must contain 1 to 100 symbols")
        return values

    def unresolved_order_intents(self):
        return self.inventory.unresolved_order_intents()

    def scan_candidates(self):
        """Rank fresh Spot candidates and attach observational Futures lead-lag data."""
        ranked = self.market_consumer.rank_candidates(self.quote_volumes_24h)
        enriched = []
        for item in ranked:
            estimate = self.lead_lag.estimate(item.candidate.symbol)
            if estimate is None:
                enriched.append(item)
                continue
            candidate = replace(
                item.candidate,
                futures_spot_lead_lag_status=estimate.status,
                futures_spot_leader=estimate.leader,
                futures_spot_lag_seconds=estimate.lag_seconds,
                futures_spot_correlation=(
                    Decimal(str(estimate.correlation))
                    if estimate.correlation is not None
                    else None
                ),
                futures_spot_confidence_low=(
                    Decimal(str(estimate.confidence_low))
                    if estimate.confidence_low is not None
                    else None
                ),
                futures_spot_confidence_high=(
                    Decimal(str(estimate.confidence_high))
                    if estimate.confidence_high is not None
                    else None
                ),
                futures_spot_observations=estimate.observations,
            )
            enriched.append(
                replace(
                    item,
                    candidate=candidate,
                    reasons=item.reasons + (f"cross_market:{estimate.status}",),
                )
            )
        return enriched

    def market_data_health(self) -> dict[str, object]:
        return {
            "tasks": self.market_data_task_status(),
            "stream_health": {
                name: dict(value) for name, value in self._market_stream_health.items()
            },
            "features": self.market_consumer.health(),
            "futures_spot_lead_lag": self.lead_lag.snapshot(),
        }

    async def start_market_data(self, market_bus) -> list[asyncio.Task]:
        """Start bounded Spot subscriptions explicitly; never auto-start on import."""
        if self._market_tasks:
            raise RuntimeError("Spot market-data runtime is already started")
        instruments = await self.eligible_instruments()
        symbols = [item.symbol for item in instruments]
        if not symbols:
            raise RuntimeError("no eligible liquid Spot instruments were discovered")
        self.quote_volumes_24h = dict(self.market_data.rest.universe_quote_volumes)
        self.market_consumer.set_symbols(symbols)
        await self.market_consumer.start(market_bus)
        self.lead_lag.set_symbols(symbols)
        await self.lead_lag.start(market_bus)
        book_symbols = symbols[:5]
        kline_symbols = symbols[:5]
        self._market_stream_health.clear()
        self._market_tasks = [
            asyncio.create_task(
                self._supervise_market_stream(
                    "trades", lambda: self.publish_trades(market_bus, symbols)
                ),
                name="aitos-spot-trades",
            ),
            asyncio.create_task(
                self._supervise_market_stream(
                    "tickers", lambda: self.publish_tickers(market_bus, symbols)
                ),
                name="aitos-spot-tickers",
            ),
            asyncio.create_task(
                self._supervise_market_stream(
                    "order_books",
                    lambda: self.publish_order_books(
                        market_bus, book_symbols, levels=20
                    ),
                ),
                name="aitos-spot-order-books",
            ),
            asyncio.create_task(
                self._supervise_market_stream(
                    "klines",
                    lambda: self.publish_klines(
                        market_bus, kline_symbols, timeframe="1m"
                    ),
                ),
                name="aitos-spot-klines",
            ),
        ]
        return list(self._market_tasks)

    async def _supervise_market_stream(
        self, name: str, publish: Callable[[], Awaitable[None]]
    ) -> None:
        delay = 1.0
        health = self._market_stream_health.setdefault(
            name, {"state": "starting", "restarts": 0, "last_error": None}
        )
        while True:
            try:
                health["state"] = "running"
                await publish()
                health["state"] = "restarting"
                health["last_error"] = "stream ended unexpectedly"
            except asyncio.CancelledError:
                health["state"] = "stopped"
                raise
            except Exception as exc:
                health["state"] = "degraded"
                health["last_error"] = str(exc)[:500]
                logger.warning(
                    "Spot market-data stream failed; restarting",
                    extra={
                        "aitos_extra": {
                            "stream": name,
                            "error": str(exc),
                            "retry_delay_seconds": delay,
                        }
                    },
                )
            health["restarts"] = int(health["restarts"]) + 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                health["state"] = "stopped"
                raise
            delay = min(delay * 2.0, 30.0)

    async def stop_market_data(self) -> None:
        tasks, self._market_tasks = self._market_tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.market_consumer.stop()
        await self.lead_lag.stop()

    def market_data_task_status(self) -> dict[str, str]:
        return {
            task.get_name(): (
                "cancelled"
                if task.cancelled()
                else (
                    "failed"
                    if task.done() and task.exception() is not None
                    else "stopped" if task.done() else "running"
                )
            )
            for task in self._market_tasks
        }

    async def close(self) -> None:
        await self.stop_market_data()
        await self.market_data.close()
        self.inventory.close()
        if self.owns_capital_gateway:
            self.coordinator.capital_gateway.reservation.close()


def build_spot_runtime(
    settings: AITOSSettings | None = None,
    *,
    capital_gateway: CapitalGateway | None = None,
    database_path: str | Path | None = None,
    mainnet_approved: bool = False,
    symbol_filters: dict[str, SpotSymbolFilters] | None = None,
    futures_spot_symbol_aliases: dict[str, str] | None = None,
) -> SpotRuntime:
    """Construct Spot components only when the operator explicitly enables them.

    This factory is not called by the Futures runner. Mainnet order placement
    requires AITOS_SPOT_ENABLED, AITOS_SPOT_EXECUTION_ENABLED,
    AITOS_SPOT_TESTNET=false, AITOS_SPOT_ALLOW_MAINNET=true and the explicit
    mainnet_approved argument from an operator-controlled launch gate.
    """
    settings = settings or get_settings()
    policy = settings.spot
    if not policy.enabled:
        raise RuntimeError("Spot runtime is disabled; set AITOS_SPOT_ENABLED=true")
    if policy.execution_enabled and capital_gateway is None:
        raise RuntimeError(
            "Spot execution requires the shared AITOS CapitalGateway instance"
        )
    if not policy.testnet and policy.execution_enabled:
        if not (policy.allow_mainnet and mainnet_approved):
            raise RuntimeError(
                "Spot mainnet execution requires separate operator approval"
            )
    spot_api_key = policy.api_key.get_secret_value()
    spot_api_secret = policy.api_secret.get_secret_value()
    if policy.execution_enabled and not (spot_api_key and spot_api_secret):
        raise RuntimeError("Spot execution requires dedicated Spot API credentials")

    rest = BinanceSpotAdapter(
        api_key=spot_api_key or None,
        api_secret=spot_api_secret or None,
        testnet=policy.testnet,
    )
    market_data = BinanceSpotCanonicalMarketDataAdapter(
        rest=rest, websocket=BinanceSpotWebSocket(testnet=policy.testnet)
    )
    executor = SpotOrderExecutor(
        rest,
        enabled=policy.execution_enabled,
        allow_mainnet=policy.allow_mainnet,
        mainnet_approved=mainnet_approved,
        quote_asset=policy.quote_asset,
        symbol_filters=symbol_filters,
    )
    path = (
        Path(database_path)
        if database_path
        else get_data_dir() / "spot" / "inventory.sqlite3"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    inventory = InventoryLedger(path)
    owns_capital_gateway = capital_gateway is None
    if capital_gateway is None:
        capital_gateway = CapitalGateway(
            reservation=CapitalReservation(
                path=path.parent / "capital_reservations.sqlite3",
                owner_id="aitos-spot-runtime",
            )
        )
    scanner = SpotOpportunityScanner(
        min_quote_volume=Decimal(str(policy.min_quote_volume)),
        max_spread_bps=Decimal(str(policy.max_spread_bps)),
        max_volatility_bps=Decimal(str(policy.max_volatility_bps)),
    )
    coordinator = SpotTradingCoordinator(
        capital_gateway=capital_gateway,
        executor=executor,
        inventory=inventory,
        fee_bps=policy.fee_bps,
        slippage_bps=policy.slippage_bps,
    )
    market_consumer = SpotMarketDataConsumer(
        scanner,
        max_symbols=policy.max_symbols,
    )
    lead_lag = FuturesSpotLeadLagAnalyzer(
        symbol_aliases=futures_spot_symbol_aliases
    )
    return SpotRuntime(
        market_data,
        executor,
        inventory,
        scanner,
        coordinator,
        market_consumer,
        lead_lag,
        quote_asset=policy.quote_asset.upper(),
        max_symbols=policy.max_symbols,
        min_quote_volume=Decimal(str(policy.min_quote_volume)),
        owns_capital_gateway=owns_capital_gateway,
    )
