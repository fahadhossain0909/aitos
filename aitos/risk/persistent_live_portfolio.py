"""Persistent live portfolio equity/peak tracking backed by ClickHouse."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import clickhouse_connect

from aitos.risk.models import PortfolioState, PositionExposure


CREATE_DRAWDOWN_TRACKING = """
CREATE TABLE IF NOT EXISTS drawdown_tracking (
    time DateTime64(3, 'UTC'),
    account String,
    asset String,
    equity_usd Float64,
    peak_equity_usd Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (account, asset, time)
"""


class PersistentDrawdownStore:
    """Small durable store for the live account equity high-water mark."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str,
        account: str = "default",
        asset: str = "USDT",
    ) -> None:
        self._conn_params = dict(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )
        self._account = account
        self._asset = asset
        self._client = None

    async def initialize(self) -> None:
        if self._client is not None:
            return
        self._client = await clickhouse_connect.get_async_client(**self._conn_params)
        await self._client.command(CREATE_DRAWDOWN_TRACKING)

    async def load_peak_equity(self) -> Optional[float]:
        if self._client is None:
            raise RuntimeError("PersistentDrawdownStore.initialize() must be called first")
        result = await self._client.query(
            """
            SELECT max(peak_equity_usd) AS peak_equity_usd
            FROM drawdown_tracking
            WHERE account = {account:String} AND asset = {asset:String}
            """,
            parameters={"account": self._account, "asset": self._asset},
        )
        if not result.result_rows or result.result_rows[0][0] is None:
            return None
        return float(result.result_rows[0][0])

    async def record(self, equity_usd: float, peak_equity_usd: float) -> None:
        if self._client is None:
            raise RuntimeError("PersistentDrawdownStore.initialize() must be called first")
        await self._client.insert(
            "drawdown_tracking",
            [[
                datetime.now(timezone.utc),
                self._account,
                self._asset,
                float(equity_usd),
                float(peak_equity_usd),
            ]],
            column_names=["time", "account", "asset", "equity_usd", "peak_equity_usd"],
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


class PersistentLivePortfolioTracker:
    """Live tracker whose high-water mark survives process restarts."""

    def __init__(self, order_executor, store: PersistentDrawdownStore, asset: str = "USDT"):
        self._order_executor = order_executor
        self._store = store
        self._asset = asset
        self._peak_equity_usd: Optional[float] = None
        self._last_known_equity_usd = 0.0

    @property
    def peak_equity_usd(self) -> Optional[float]:
        return self._peak_equity_usd

    async def initialize(self) -> None:
        self._peak_equity_usd = await self._store.load_peak_equity()

    async def refresh_equity(self) -> float:
        equity = await self._order_executor.get_account_balance(self._asset)
        self._last_known_equity_usd = equity
        self._peak_equity_usd = (
            equity
            if self._peak_equity_usd is None
            else max(self._peak_equity_usd, equity)
        )
        await self._store.record(equity, self._peak_equity_usd)
        return equity

    def build_portfolio_state(self, trade_lifecycle) -> PortfolioState:
        open_trades = trade_lifecycle.get_open_trades()
        positions = tuple(
            PositionExposure(
                symbol=t.symbol,
                notional_usd=t.position_size_usd,
                leverage=t.leverage,
            )
            for t in open_trades
        )
        regime_counts: Dict[str, int] = {}
        for trade in open_trades:
            regime_counts[trade.regime] = regime_counts.get(trade.regime, 0) + 1
        dominant_regime = (
            max(regime_counts, key=regime_counts.get) if regime_counts else "unknown"
        )
        return PortfolioState(
            equity_usd=self._last_known_equity_usd,
            peak_equity_usd=self._peak_equity_usd or self._last_known_equity_usd,
            positions=positions,
            regime=dominant_regime,
        )
