from datetime import datetime, timezone

from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.models.market import TradeSide, TradeTick


def _trade(trade_id: int, side: TradeSide) -> TradeTick:
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=50000.0,
        quantity=1.0,
        side=side,
        is_buyer_maker=side == TradeSide.SELL,
        timestamp=datetime.now(timezone.utc),
    )


def test_order_flow_features_exposes_bias_score_alias() -> None:
    features = OrderFlowEngine().ingest_many(
        [_trade(1, TradeSide.BUY), _trade(2, TradeSide.SELL)]
    )

    assert features.bias_score == features.imbalance
    assert 0.0 <= features.bias_score <= 10.0
