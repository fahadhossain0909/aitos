"""Durable ClickHouse persistence for multi-capital/prop accounts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

CREATE_PROP_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS prop_accounts (
    recorded_at DateTime64(3, 'UTC'), account_id String, provider LowCardinality(String),
    platform LowCardinality(String), account_mode LowCardinality(String), initial_balance Float64,
    currency LowCardinality(String), api_enabled UInt8, automation_enabled UInt8, metadata_json String
) ENGINE = ReplacingMergeTree(recorded_at) ORDER BY (provider, account_id)
"""

CREATE_PROP_RULE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS prop_rule_snapshots (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    daily_loss_limit Nullable(Float64), max_drawdown Nullable(Float64),
    daily_loss_uses_equity UInt8, max_drawdown_uses_equity UInt8, api_allowed UInt8,
    automation_allowed UInt8, news_trading_allowed UInt8, overnight_allowed UInt8,
    weekend_allowed UInt8, max_order_notional Nullable(Float64)
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at)
"""

CREATE_PROP_EQUITY_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS prop_equity_snapshots (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    balance Float64, equity Float64, day_start_equity Float64, high_water_mark Float64,
    daily_loss Float64, drawdown Float64, realized_pnl_today Float64, unrealized_pnl Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at)
"""

CREATE_PROP_ORDERS = """
CREATE TABLE IF NOT EXISTS prop_orders (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    venue LowCardinality(String), client_order_id String, order_id String, symbol LowCardinality(String),
    side LowCardinality(String), order_type LowCardinality(String), quantity Float64,
    reference_price Float64, fill_price Nullable(Float64), filled_quantity Nullable(Float64),
    success UInt8, error Nullable(String), metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, order_id)
"""

CREATE_PROP_COMPLIANCE_EVENTS = """
CREATE TABLE IF NOT EXISTS prop_compliance_events (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    venue LowCardinality(String), allowed UInt8, reason String, symbol LowCardinality(String),
    side LowCardinality(String), quantity Float64, reference_price Float64, projected_loss Float64,
    correlation_id String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, correlation_id)
"""

CREATE_PROP_PAYOUTS = """
CREATE TABLE IF NOT EXISTS prop_payouts (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    payout_id String, amount Float64, currency LowCardinality(String), status LowCardinality(String),
    metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, payout_id)
"""


class PropCapitalRepository:
    """Append-only audit store for prop/funded capital lifecycle."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        username: str = "default",
        password: str = "",
        database: str = "aitos",
    ) -> None:
        self._params = dict(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )
        self._client = None

    async def initialize(self) -> None:
        self._client = await clickhouse_connect.get_async_client(**self._params)
        for ddl in (
            CREATE_PROP_ACCOUNTS,
            CREATE_PROP_RULE_SNAPSHOTS,
            CREATE_PROP_EQUITY_SNAPSHOTS,
            CREATE_PROP_ORDERS,
            CREATE_PROP_COMPLIANCE_EVENTS,
            CREATE_PROP_PAYOUTS,
        ):
            await self._client.command(ddl)

    async def save_account(
        self,
        *,
        account_id: str,
        provider: str,
        platform: str,
        account_mode: str,
        initial_balance: float,
        currency: str,
        api_enabled: bool,
        automation_enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._require()
        await self._client.insert(
            "prop_accounts",
            [
                [
                    datetime.now(timezone.utc),
                    account_id,
                    provider,
                    platform,
                    account_mode,
                    initial_balance,
                    currency,
                    int(api_enabled),
                    int(automation_enabled),
                    json.dumps(metadata or {}, sort_keys=True),
                ]
            ],
            column_names=[
                "recorded_at",
                "account_id",
                "provider",
                "platform",
                "account_mode",
                "initial_balance",
                "currency",
                "api_enabled",
                "automation_enabled",
                "metadata_json",
            ],
        )

    async def save_rule(self, profile: Any) -> None:
        """Persist the effective prop rule set used by the compliance firewall."""
        self._require()
        rules = profile.rules
        await self._client.insert(
            "prop_rule_snapshots",
            [
                [
                    datetime.now(timezone.utc),
                    profile.provider,
                    profile.account_id,
                    rules.daily_loss_limit,
                    rules.max_drawdown,
                    int(rules.daily_loss_uses_equity),
                    int(rules.max_drawdown_uses_equity),
                    int(rules.api_allowed),
                    int(rules.automation_allowed),
                    int(rules.news_trading_allowed),
                    int(rules.overnight_allowed),
                    int(rules.weekend_allowed),
                    rules.max_order_notional,
                ]
            ],
            column_names=[
                "recorded_at",
                "provider",
                "account_id",
                "daily_loss_limit",
                "max_drawdown",
                "daily_loss_uses_equity",
                "max_drawdown_uses_equity",
                "api_allowed",
                "automation_allowed",
                "news_trading_allowed",
                "overnight_allowed",
                "weekend_allowed",
                "max_order_notional",
            ],
        )

    async def save_equity(self, snapshot: Any) -> None:
        self._require()
        await self._client.insert(
            "prop_equity_snapshots",
            [
                [
                    datetime.now(timezone.utc),
                    snapshot.provider,
                    snapshot.account_id,
                    snapshot.balance,
                    snapshot.equity,
                    snapshot.day_start_equity,
                    snapshot.high_water_mark,
                    snapshot.daily_loss,
                    snapshot.drawdown,
                    snapshot.realized_pnl_today,
                    snapshot.unrealized_pnl,
                ]
            ],
            column_names=[
                "recorded_at",
                "provider",
                "account_id",
                "balance",
                "equity",
                "day_start_equity",
                "high_water_mark",
                "daily_loss",
                "drawdown",
                "realized_pnl_today",
                "unrealized_pnl",
            ],
        )

    async def save_order(
        self,
        *,
        provider: str,
        account_id: str,
        venue: str,
        request: Any,
        result: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._require()
        await self._client.insert(
            "prop_orders",
            [
                [
                    datetime.now(timezone.utc),
                    provider,
                    account_id,
                    venue,
                    request.client_order_id or "",
                    result.order_id,
                    request.symbol,
                    request.side.value,
                    request.order_type,
                    request.quantity,
                    request.reference_price,
                    result.fill_price,
                    result.filled_quantity,
                    int(result.success),
                    result.error,
                    json.dumps(metadata or {}, sort_keys=True),
                ]
            ],
            column_names=[
                "recorded_at",
                "provider",
                "account_id",
                "venue",
                "client_order_id",
                "order_id",
                "symbol",
                "side",
                "order_type",
                "quantity",
                "reference_price",
                "fill_price",
                "filled_quantity",
                "success",
                "error",
                "metadata_json",
            ],
        )

    async def save_compliance(
        self,
        *,
        provider: str,
        account_id: str,
        venue: str,
        decision: Any,
        request: Any,
        projected_loss: float,
        correlation_id: str,
    ) -> None:
        self._require()
        await self._client.insert(
            "prop_compliance_events",
            [
                [
                    datetime.now(timezone.utc),
                    provider,
                    account_id,
                    venue,
                    int(decision.allowed),
                    decision.reason,
                    request.symbol,
                    request.side.value,
                    request.quantity,
                    request.reference_price,
                    projected_loss,
                    correlation_id,
                ]
            ],
            column_names=[
                "recorded_at",
                "provider",
                "account_id",
                "venue",
                "allowed",
                "reason",
                "symbol",
                "side",
                "quantity",
                "reference_price",
                "projected_loss",
                "correlation_id",
            ],
        )

    async def save_payout(
        self,
        *,
        provider: str,
        account_id: str,
        payout_id: str,
        amount: float,
        currency: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._require()
        await self._client.insert(
            "prop_payouts",
            [
                [
                    datetime.now(timezone.utc),
                    provider,
                    account_id,
                    payout_id,
                    amount,
                    currency,
                    status,
                    json.dumps(metadata or {}, sort_keys=True),
                ]
            ],
            column_names=[
                "recorded_at",
                "provider",
                "account_id",
                "payout_id",
                "amount",
                "currency",
                "status",
                "metadata_json",
            ],
        )

    def _require(self) -> None:
        if self._client is None:
            raise RuntimeError(
                "PropCapitalRepository.initialize() must be called first"
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
