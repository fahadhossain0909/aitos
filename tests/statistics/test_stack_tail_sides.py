from aitos.statistics.engine import AStatEngine
from aitos.statistics.models import AStatObservation
from aitos.statistics.stack import ContractStatisticalStack


def asymmetric_returns():
    return [-0.0005 - (index % 4) * 0.0001 for index in range(100)] + [
        0.004 + index * 0.0005 for index in range(20)
    ]


def test_statistical_stack_estimates_both_loss_tails():
    result = ContractStatisticalStack().evaluate("BTCUSDT", asymmetric_returns())
    assert result.upside_evt is not None
    assert result.upside_evt.expected_shortfall > result.evt.expected_shortfall


def test_a_stat_exposes_upside_tail_probability_for_short_risk():
    returns = asymmetric_returns()
    result = AStatEngine().evaluate(
        AStatObservation(
            symbol="BTCUSDT",
            horizon="15m",
            sample_size=len(returns),
            features={"returns": returns},
        )
    )
    assert result.advanced is not None
    assert result.upside_tail_probability > 0.0
    assert (
        result.upside_expected_shortfall
        == result.advanced.upside_evt.expected_shortfall
    )
