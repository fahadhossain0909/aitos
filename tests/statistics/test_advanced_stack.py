from __future__ import annotations

import math

from aitos.statistics import ContractStatisticalStack, GARCH11, HierarchicalBayes, MarkovSwitchingModel, POTGPD


def _returns() -> list[float]:
    return [0.002 * math.sin(i / 3.0) + (0.001 if i % 7 else -0.004) for i in range(160)]


def test_all_models_produce_finite_outputs() -> None:
    xs = _returns()
    hmm = MarkovSwitchingModel().fit_predict(xs)
    garch = GARCH11().fit_forecast(xs)
    evt = POTGPD().fit(xs)
    bayes = HierarchicalBayes().fit(xs, xs * 2)
    assert math.isclose(sum(hmm.probabilities), 1.0, rel_tol=1e-6)
    assert garch.volatility > 0
    assert 0 <= evt.tail_probability <= 1
    assert 0 <= bayes.predictive_probability_positive <= 1


def test_same_contract_stack_keeps_models_isolated_by_symbol() -> None:
    stack = ContractStatisticalStack()
    stack.update("BTCUSDT", _returns())
    stack.update("ETHUSDT", [-x for x in _returns()])
    btc = stack.evaluate("BTCUSDT")
    eth = stack.evaluate("ETHUSDT")
    assert btc.symbol == "BTCUSDT"
    assert eth.symbol == "ETHUSDT"
    assert btc.expected_return != eth.expected_return
    assert btc.garch.volatility >= 0
    assert btc.evt.expected_shortfall >= 0
