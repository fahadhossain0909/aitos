"""Tabular bandit persistence and structure-aware direction."""

from pathlib import Path

import pytest

from aitos.intelligence.rl_policy import TabularBanditRLScorer
from aitos.intelligence.scanner import determine_direction
from aitos.models.trade import TradeSide


@pytest.mark.asyncio
async def test_tabular_bandit_round_trips_state(tmp_path: Path):
    path = tmp_path / "tabular_bandit.pkl"
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1, state_path=str(path))
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=1.5
    )
    scorer.save_state()

    restored = TabularBanditRLScorer(min_samples_for_confidence=1, state_path=str(path))
    assert restored.load_state() is True
    assert restored.sample_count("BTCUSDT", "trending", "LONG") == 1
    score = await restored.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert score > 5.0


def test_tabular_bandit_update_and_persist_merges(tmp_path: Path):
    path = tmp_path / "tabular_bandit.pkl"
    first = TabularBanditRLScorer(min_samples_for_confidence=1, state_path=str(path))
    first.update_and_persist(
        "ETHUSDT", {"regime": "ranging", "direction": "SHORT"}, 1.0
    )
    second = TabularBanditRLScorer(min_samples_for_confidence=1, state_path=str(path))
    second.update_and_persist(
        "ETHUSDT", {"regime": "ranging", "direction": "SHORT"}, 3.0
    )
    assert second.sample_count("ETHUSDT", "ranging", "SHORT") == 2
    assert second._means[("ETHUSDT", "ranging", "SHORT")] == pytest.approx(2.0)


def test_determine_direction_blocks_cvd_against_live_bias():
    assert determine_direction("none", 7.0, structure_bias="bearish") is None
    assert determine_direction("none", 3.0, structure_bias="bullish") is None
    assert determine_direction("none", 7.0, structure_bias="neutral") == TradeSide.LONG


def test_determine_direction_choch_requires_confirming_flow():
    assert (
        determine_direction(
            "bullish_bos", 6.0, structure_bias="bearish", structure_event="choch"
        )
        == TradeSide.LONG
    )
    assert (
        determine_direction(
            "bullish_bos", 3.0, structure_bias="bearish", structure_event="choch"
        )
        is None
    )
