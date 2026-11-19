"""Explicit Spot runtime factory; never imported by the Futures live runner."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from aitos.config.paths import get_data_dir
from aitos.config.settings import AITOSSettings, get_settings
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.market_data.binance_spot_adapter import BinanceSpotCanonicalMarketDataAdapter
from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.coordinator import SpotTradingCoordinator
from aitos.spot.execution import SpotOrderExecutor
from aitos.spot.filters import SpotSymbolFilters
from aitos.spot.inventory import InventoryLedger
from aitos.spot.scanner import SpotOpportunityScanner
from aitos.spot.websocket import BinanceSpotWebSocket


@dataclass(slots=True)
class SpotRuntime:
    market_data: BinanceSpotCanonicalMarketDataAdapter
    executor: SpotOrderExecutor
    inventory: InventoryLedger
    scanner: SpotOpportunityScanner
    coordinator: SpotTradingCoordinator

    async def close(self) -> None:
        await self.market_data.close()
        self.inventory.close()


def build_spot_runtime(
    settings: AITOSSettings | None = None,
    *,
    capital_gateway: CapitalGateway | None = None,
    database_path: str | Path | None = None,
    mainnet_approved: bool = False,
    symbol_filters: dict[str, SpotSymbolFilters] | None = None,
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
    if not policy.testnet and policy.execution_enabled:
        if not (policy.allow_mainnet and mainnet_approved):
            raise RuntimeError("Spot mainnet execution requires separate operator approval")
    if policy.execution_enabled and not (
        settings.binance.api_key and settings.binance.api_secret
    ):
        raise RuntimeError("Spot execution requires Binance API credentials")

    rest = BinanceSpotAdapter(
        api_key=settings.binance.api_key or None,
        api_secret=settings.binance.api_secret or None,
        testnet=policy.testnet,
    )
    market_data = BinanceSpotCanonicalMarketDataAdapter(
        rest=rest, websocket=BinanceSpotWebSocket(testnet=policy.testnet)
    )
    executor = SpotOrderExecutor(
        rest,
        enabled=policy.execution_enabled,
        allow_mainnet=bool(policy.allow_mainnet and mainnet_approved),
        symbol_filters=symbol_filters,
    )
    path = Path(database_path) if database_path else get_data_dir() / "spot" / "inventory.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    inventory = InventoryLedger(path)
    scanner = SpotOpportunityScanner(
        min_quote_volume=Decimal(str(policy.min_quote_volume)),
        max_spread_bps=__import__("decimal").Decimal(str(policy.max_spread_bps)),
        max_volatility_bps=__import__("decimal").Decimal(str(policy.max_volatility_bps)),
    )
    coordinator = SpotTradingCoordinator(
        capital_gateway=capital_gateway or CapitalGateway(),
        executor=executor,
        inventory=inventory,
        fee_bps=policy.fee_bps,
        slippage_bps=policy.slippage_bps,
    )
    return SpotRuntime(market_data, executor, inventory, scanner, coordinator)
