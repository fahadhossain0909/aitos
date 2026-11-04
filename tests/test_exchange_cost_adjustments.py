import asyncio

import pytest

from aitos.exchange.cost_adjustments import (
    BinanceCostAdjustmentPublisher,
    UnattributedCostAdjustment,
)


class EventBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def test_binance_income_rows_publish_stable_fee_and_funding_events():
    bus = EventBus()

    async def resolve(row):
        return "trade-123"

    publisher = BinanceCostAdjustmentPublisher(bus, resolve)
    rows = [
        {
            "incomeType": "COMMISSION",
            "income": "-0.12",
            "tranId": 1001,
            "asset": "USDT",
            "symbol": "BTCUSDT",
            "time": 100,
        },
        {
            "incomeType": "FUNDING_FEE",
            "income": "0.04",
            "tranId": 1002,
            "asset": "USDT",
            "symbol": "BTCUSDT",
            "time": 200,
        },
        {"incomeType": "REALIZED_PNL", "income": "4", "tranId": 1003, "asset": "USDT"},
    ]

    assert asyncio.run(publisher.publish_income_rows(rows)) == 2
    assert [event.payload["kind"] for event in bus.events] == ["fee", "funding"]
    assert bus.events[0].payload["amount_usd"] == 0.12
    assert bus.events[1].payload["amount_usd"] == 0.04
    assert bus.events[0].payload["adjustment_id"] == "binance:income:1001"


def test_binance_income_publisher_refuses_unattributed_rows():
    bus = EventBus()

    async def resolve(row):
        return None

    publisher = BinanceCostAdjustmentPublisher(bus, resolve)
    row = {
        "incomeType": "FUNDING_FEE",
        "income": "-0.04",
        "tranId": 1002,
        "asset": "USDT",
        "symbol": "BTCUSDT",
        "time": 200,
    }

    with pytest.raises(UnattributedCostAdjustment):
        asyncio.run(publisher.publish_income_rows([row]))
    assert bus.events == []


def test_binance_income_publisher_rejects_non_usdt_and_non_finite_amounts():
    bus = EventBus()

    async def resolve(row):
        return "trade-123"

    publisher = BinanceCostAdjustmentPublisher(bus, resolve)
    with pytest.raises(ValueError):
        asyncio.run(
            publisher.publish_income_rows(
                [
                    {
                        "incomeType": "COMMISSION",
                        "income": "-0.1",
                        "tranId": 1,
                        "asset": "BTC",
                    }
                ]
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            publisher.publish_income_rows(
                [
                    {
                        "incomeType": "COMMISSION",
                        "income": "nan",
                        "tranId": 2,
                        "asset": "USDT",
                    }
                ]
            )
        )
