"""PortfolioAgent — tracks open positions, suggests rebalancing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.contracts import Event, EventResponse


class PortfolioAgent(BaseAgent):
    """Tracks open positions and portfolio state, suggests rebalancing.

    Listens for ``portfolio.position_update`` and ``trade.closed`` events,
    maintains portfolio state in short-term memory, and contributes
    rebalancing/neutral decisions to the fusion engine.
    """

    def __init__(self, event_bus: Any, consensus_weight: float = 1.0) -> None:
        super().__init__(
            agent_id="portfolio-agent",
            event_bus=event_bus,
            consensus_weight=consensus_weight,
        )
        self._positions: dict[str, dict[str, Any]] = {}
        self._total_pnl: float = 0.0
        self._total_trades: int = 0
        self._winning_trades: int = 0

    async def on_initialize(self, config: dict[str, Any]) -> None:
        self.memory.remember_long_term("max_positions", config.get("max_positions", 5))
        self.memory.remember_long_term(
            "rebalance_threshold", config.get("rebalance_threshold", 0.3)
        )
        self.memory.remember_long_term(
            "target_per_position", config.get("target_per_position", 0.2)
        )

    async def on_handle_event(self, event: Event) -> EventResponse | None:
        """Process portfolio-related events."""
        if event.topic.startswith("portfolio.position_update"):
            symbol = event.payload.get("symbol")
            if symbol:
                self._positions[symbol] = {
                    "side": event.payload.get("side", "unknown"),
                    "size": event.payload.get("size", 0.0),
                    "entry_price": event.payload.get("entry_price", 0.0),
                    "unrealized_pnl": event.payload.get("unrealized_pnl", 0.0),
                    "timestamp": event.payload.get("timestamp", ""),
                }
                self.memory.remember_short_term(
                    {
                        "type": "position_update",
                        "symbol": symbol,
                        "side": event.payload.get("side"),
                        "size": event.payload.get("size"),
                    }
                )
        elif event.topic.startswith("trade.closed"):
            pnl = event.payload.get("realized_pnl", 0.0)
            if isinstance(pnl, (int, float)):
                self._total_pnl += float(pnl)
                self._total_trades += 1
                if float(pnl) > 0:
                    self._winning_trades += 1
                self.memory.remember_short_term(
                    {
                        "type": "trade_closed",
                        "symbol": event.payload.get("symbol"),
                        "pnl": float(pnl),
                    }
                )
        elif event.topic.startswith("trade.opened"):
            symbol = event.payload.get("symbol")
            if symbol and symbol not in self._positions:
                self.memory.remember_short_term(
                    {
                        "type": "trade_opened",
                        "symbol": symbol,
                        "side": event.payload.get("side"),
                    }
                )
        return None

    async def on_tick(self) -> None:
        """Periodic portfolio evaluation."""

    async def on_emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    def _compute_exposure(self) -> float:
        """Compute total portfolio exposure as fraction."""
        if not self._positions:
            return 0.0
        return sum(abs(p.get("size", 0.0)) for p in self._positions.values())

    def _compute_concentration(self) -> float:
        """Compute max single-position concentration."""
        if not self._positions:
            return 0.0
        exposure = self._compute_exposure()
        if exposure == 0:
            return 0.0
        max_size = max(abs(p.get("size", 0.0)) for p in self._positions.values())
        return max_size / exposure

    def _check_rebalance_needed(self) -> bool:
        """Determine if portfolio rebalancing is needed."""
        target = self.memory.recall_long_term("target_per_position", 0.2)
        threshold = self.memory.recall_long_term("rebalance_threshold", 0.3)
        max_positions = self.memory.recall_long_term("max_positions", 5)

        if len(self._positions) >= max_positions:
            return True
        concentration = self._compute_concentration()
        return concentration > (target + threshold)

    async def contribute_decision(self, context: dict[str, Any]) -> AgentDecision:
        """Produce a portfolio-aware decision for the fusion engine."""
        num_positions = len(self._positions)
        exposure = self._compute_exposure()
        concentration = self._compute_concentration()
        rebalance_needed = self._check_rebalance_needed()

        win_rate = (
            (self._winning_trades / self._total_trades)
            if self._total_trades > 0
            else 0.0
        )

        if rebalance_needed:
            direction = "neutral"
            confidence = 0.85
            rationale = f"Rebalance needed: {num_positions} positions, concentration={concentration:.1%}"
        elif num_positions == 0:
            direction = context.get("direction", "neutral")
            confidence = 0.9
            rationale = "No open positions. Full capacity for new entries."
        else:
            direction = "neutral"
            confidence = 0.6
            rationale = (
                f"Portfolio stable: {num_positions} positions, exposure={exposure:.1%}"
            )

        evidence = [
            f"open_positions={num_positions}",
            f"total_exposure={exposure:.1%}",
            f"concentration={concentration:.1%}",
            f"total_pnl={self._total_pnl:.2f}",
            f"win_rate={win_rate:.1%}",
        ]
        if self._positions:
            symbols = list(self._positions.keys())[:5]
            evidence.append(f"positions={','.join(symbols)}")

        return AgentDecision(
            agent_id=self.module_id,
            confidence=confidence,
            direction=direction,
            rationale=rationale,
            evidence=evidence,
            metadata={
                "num_positions": num_positions,
                "exposure": exposure,
                "concentration": concentration,
                "total_pnl": self._total_pnl,
                "win_rate": win_rate,
                "rebalance_needed": rebalance_needed,
            },
        )
