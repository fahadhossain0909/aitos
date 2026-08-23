import pytest

from aitos.journal.performance_evaluator import DecisionPerformanceEvaluator


class FakeRepository:
    def __init__(self, records):
        self.records = records

    async def get_records(self, decision_id):
        return self.records.get(decision_id, [])


@pytest.mark.asyncio
async def test_evaluator_aggregates_outcomes_and_slices():
    repository = FakeRepository(
        {
            "d1": [
                {"record_type": "DECISION", "decision_id": "d1"},
                {"record_type": "TRADE_LINK", "decision_id": "d1", "trade_id": "t1"},
                {
                    "record_type": "OUTCOME",
                    "decision_id": "d1",
                    "trade_id": "t1",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "strategy_id": "s1",
                    "regime": "trend",
                    "exit_reason": "tp_triggered",
                    "pnl": 100.0,
                    "r_multiple": 2.0,
                },
                {
                    "record_type": "OUTCOME",
                    "decision_id": "d1",
                    "trade_id": "t2",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "strategy_id": "s1",
                    "regime": "trend",
                    "exit_reason": "sl_triggered",
                    "pnl": -50.0,
                    "r_multiple": -1.0,
                },
            ]
        }
    )
    evaluator = DecisionPerformanceEvaluator(repository)
    await evaluator.initialize({})

    report = await evaluator.evaluate_decision("d1")

    assert report.decision_count == 1
    assert report.linked_trade_count == 1
    assert report.outcome_count == 2
    assert report.total_pnl == 50.0
    assert report.average_pnl == 25.0
    assert report.average_r_multiple == 0.5
    assert report.win_rate == 0.5
    regime = next(s for s in report.slices if s.key == "regime")
    assert regime.value == "trend"
    assert regime.trades == 2
    assert regime.win_rate == 0.5


@pytest.mark.asyncio
async def test_evaluator_handles_missing_outcomes():
    repository = FakeRepository(
        {"d2": [{"record_type": "DECISION", "decision_id": "d2"}]}
    )
    evaluator = DecisionPerformanceEvaluator(repository)
    await evaluator.initialize({})

    report = await evaluator.evaluate_decision("d2")

    assert report.decision_count == 1
    assert report.outcome_count == 0
    assert report.total_pnl == 0.0
    assert report.win_rate == 0.0
