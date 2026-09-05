"""Unified per-contract statistical stack."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence

from .evt import POTGPD
from .garch import GARCH11
from .hierarchical_bayes import HierarchicalBayes
from .hmm import MarkovSwitchingModel
from .models import StatisticalModelResult


class ContractStatisticalStack:
    """Run HMM -> GARCH -> EVT -> hierarchical Bayes on the same instrument history."""

    def __init__(self, max_history: int = 4096) -> None:
        self.max_history = max_history
        self._returns: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_history)
        )
        self._global: deque[float] = deque(maxlen=max_history * 2)
        self.hmm = MarkovSwitchingModel(states=3)
        self.garch = GARCH11()
        self.evt = POTGPD()
        self.bayes = HierarchicalBayes()

    def update(self, symbol: str, returns: Sequence[float] | float) -> None:
        values = (returns,) if isinstance(returns, (int, float)) else returns
        for value in values:
            value = float(value)
            if math.isfinite(value):
                self._returns[symbol].append(value)
                self._global.append(value)

    def evaluate(
        self, symbol: str, returns: Sequence[float] = ()
    ) -> StatisticalModelResult:
        if returns:
            self.update(symbol, returns)
        xs = tuple(self._returns[symbol])
        hmm = self.hmm.fit_predict(xs)
        garch = self.garch.fit_forecast(xs)
        evt = self.evt.fit(xs)
        bayes = self.bayes.fit(xs, tuple(self._global))
        # HMM state mean supplies the directional component; Bayes supplies shrinkage.
        hmm_mean = sum(p * m for p, m in zip(hmm.probabilities, hmm.state_means))
        expected_return = 0.5 * hmm_mean + 0.5 * bayes.contract_mean
        vol = max(garch.volatility, 1e-8)
        z = expected_return / vol
        downside = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
        tail = evt.tail_probability
        confidence = min(
            1.0,
            0.25 * math.log1p(len(xs)) / math.log(1001.0)
            + 0.75 * bayes.posterior_strength,
        )
        return StatisticalModelResult(
            symbol=symbol,
            hmm=hmm,
            garch=garch,
            evt=evt,
            bayesian=bayes,
            expected_return=expected_return,
            downside_probability=max(0.0, min(1.0, downside)),
            tail_probability=max(0.0, min(1.0, tail)),
            confidence=max(0.0, min(1.0, confidence)),
        )

    def evaluate_many(
        self, observations: Mapping[str, Sequence[float]]
    ) -> dict[str, StatisticalModelResult]:
        return {
            symbol: self.evaluate(symbol, values)
            for symbol, values in observations.items()
        }
