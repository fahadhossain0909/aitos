"""MarketAgent — analyzes market conditions from WS data, produces BUY/SELL signals."""

from __future__ import annotations

import statistics
from collections.abc import AsyncIterator
from typing import Any

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.contracts import Event, EventResponse


class MarketAgent(BaseAgent):
    """Analyzes live market data (klines, trades) to produce directional signals.

    Listens for ``market.kline.*`` and ``market.trade.*`` events, tracks price
    momentum/volatility in short-term memory, and contributes a long/short/neutral
    decision to the fusion engine.
    """

    def __init__(self, event_bus: Any, consensus_weight: float = 1.0) -> None:
        super().__init__(
            agent_id="market-agent",
            event_bus=event_bus,
            consensus_weight=consensus_weight,
        )
        self._price_history: dict[str, list[float]] = {}
        self._volume_history: dict[str, list[float]] = {}
        self._last_signal: dict[str, str] = {}

    async def on_initialize(self, config: dict[str, Any]) -> None:
        self.memory.remember_long_term(
            "lookback_period", config.get("lookback_period", 20)
        )
        self.memory.remember_long_term(
            "momentum_threshold", config.get("momentum_threshold", 0.02)
        )

    async def on_handle_event(self, event: Event) -> EventResponse | None:
        """Process market data events to update internal state."""
        if event.topic.startswith("market.kline"):
            symbol = event.payload.get("symbol")
            close = event.payload.get("close")
            volume = event.payload.get("volume")
            if symbol and close is not None:
                if symbol not in self._price_history:
                    self._price_history[symbol] = []
                self._price_history[symbol].append(float(close))
                lookback = self.memory.recall_long_term("lookback_period", 20)
                if len(self._price_history[symbol]) > lookback:
                    self._price_history[symbol] = self._price_history[symbol][
                        -lookback:
                    ]
                if volume is not None:
                    if symbol not in self._volume_history:
                        self._volume_history[symbol] = []
                    self._volume_history[symbol].append(float(volume))
                    if len(self._volume_history[symbol]) > lookback:
                        self._volume_history[symbol] = self._volume_history[symbol][
                            -lookback:
                        ]
                self.memory.remember_short_term(
                    {
                        "type": "kline",
                        "symbol": symbol,
                        "close": close,
                        "volume": volume,
                    }
                )
        elif event.topic.startswith("market.trade"):
            symbol = event.payload.get("symbol")
            price = event.payload.get("price")
            if symbol and price is not None:
                self.memory.remember_short_term(
                    {
                        "type": "trade",
                        "symbol": symbol,
                        "price": price,
                    }
                )
        return None

    async def on_tick(self) -> None:
        """Periodic evaluation — can be called by the kernel's tick loop."""
        for symbol in self._price_history:
            prices = self._price_history[symbol]
            if len(prices) >= 2:
                signal = self._compute_signal(symbol, prices)
                self._last_signal[symbol] = signal

    async def on_emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    def _compute_signal(self, symbol: str, prices: list[float]) -> str:
        """Compute directional signal from price history."""
        lookback = min(len(prices), self.memory.recall_long_term("lookback_period", 20))
        recent = prices[-lookback:]
        if len(recent) < 2:
            return "neutral"
        momentum = (recent[-1] - recent[0]) / recent[0] if recent[0] != 0 else 0.0
        threshold = self.memory.recall_long_term("momentum_threshold", 0.02)
        if momentum > threshold:
            return "long"
        elif momentum < -threshold:
            return "short"
        return "neutral"

    def _compute_confidence(self, prices: list[float]) -> float:
        """Compute confidence based on trend strength and volatility."""
        if len(prices) < 2:
            return 0.0
        lookback = min(len(prices), self.memory.recall_long_term("lookback_period", 20))
        recent = prices[-lookback:]
        if len(recent) < 2:
            return 0.0
        momentum = abs((recent[-1] - recent[0]) / recent[0]) if recent[0] != 0 else 0.0
        returns = [
            (recent[i] - recent[i - 1]) / recent[i - 1]
            for i in range(1, len(recent))
            if recent[i - 1] != 0
        ]
        volatility = statistics.stdev(returns) if len(returns) >= 2 else 0.01
        confidence = min(momentum / (volatility + 0.001), 1.0)
        return round(confidence, 4)

    async def contribute_decision(self, context: dict[str, Any]) -> AgentDecision:
        """Produce a directional decision for the fusion engine."""
        symbol = context.get("symbol")
        if not symbol or symbol not in self._price_history:
            return AgentDecision(
                agent_id=self.module_id,
                confidence=0.0,
                direction="neutral",
                rationale="No price history available for symbol.",
            )
        prices = self._price_history[symbol]
        direction = self._compute_signal(symbol, prices)
        confidence = self._compute_confidence(prices)
        evidence = [
            f"lookback={len(prices)} prices",
            f"last_price={prices[-1]:.4f}" if prices else "no prices",
        ]
        if self._volume_history.get(symbol):
            evidence.append(
                f"avg_volume={statistics.mean(self._volume_history[symbol]):.2f}"
            )
        return AgentDecision(
            agent_id=self.module_id,
            confidence=confidence,
            direction=direction,
            rationale=f"Market momentum signal for {symbol}: {direction} (confidence={confidence:.2f})",
            evidence=evidence,
            metadata={"last_signal": self._last_signal.get(symbol, "unknown")},
        )
