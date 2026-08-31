from aitos.eventbus.redis_bus import EventBus
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.live_state import LiveMarketStateStore
from aitos.logging_setup import get_logger
from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

logger = get_logger("aitos.data.ingestion")

TRADE_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
TRADE_STREAM_RESTART_DELAY_SECONDS = 1.0
TRADE_STREAM_QUEUE_SIZE = 10_000
TRADE_STREAM_BATCH_SIZE = 64
TRADE_STREAM_BATCH_WAIT_SECONDS = 0.010
# Keep Redis sink fan-out below the shared connection-pool ceiling. 32 workers
# can exhaust the 64-connection pool because each sink operation may use more
# than one Redis command/connection while archive/consumers are active.
TRADE_SINK_CONCURRENCY = 16
TRADE_PERSIST_QUEUE_SIZE = 50_000
TRADE_FALLBACK_LIMIT = 500
ORDERBOOK_PERSIST_INTERVAL_SECONDS = 1.0
STREAM_RESTART_DELAY_SECONDS = 1.0


def kline_topic(symbol: str, timeframe: str) -> str:
    return f"market.kline.{symbol}.{timeframe}"


def trade_topic(symbol: str) -> str:
    return f"market.trade.{symbol}"


def orderbook_topic(symbol: str) -> str:
    return f"market.orderbook.{symbol}"


def liquidity_topic(symbol: str) -> str:
    return f"market.liquidity.{symbol}"


def orderflow_topic(symbol: str) -> str:
    return f"market.orderflow.{symbol}"


def live_state_topic(symbol: str) -> str:
    return f"market.live_state.{symbol}"


class DataIngestionService(AITOSModule):
    """Live market ingestion with bounded lossless backpressure."""

    def __init__(
        self,
        exchange: ExchangeAdapter,
        event_bus: EventBus,
        symbols: list[str],
        kline_timeframe: str = "1m",
        repository: MarketDataRepository | None = None,
        orderbook_levels: int = 20,
        liquidity_trade_window: int = 500,
        live_trade_handler: Callable[[TradeTick], Awaitable[None]] | None = None,
        live_orderbook_handler: (
            Callable[[OrderBookSnapshot], Awaitable[None]] | None
        ) = None,
    ) -> None:
