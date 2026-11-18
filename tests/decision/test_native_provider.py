from datetime import datetime, timezone

import pytest

from aitos.decision.dsl import parse_decision_spec
from aitos.decision.providers import NativeEvidenceProvider
from aitos.decision.types import MarketSnapshot


def test_native_provider_returns_conservative_uncalibrated_prior():
    spec = parse_decision_spec(
        "DECISION pretrade:\nSTATE:\n - direction\n - composite_score\n"
        "ASK side: CHOICE [LONG, SHORT, NO_TRADE]"
    )
    snapshot = MarketSnapshot(
        "BTCUSDT",
        datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        {"direction": "LONG", "composite_score": 80.0, "reversal_probability": 0.4},
        snapshot_id="candidate-1",
    )
    response = NativeEvidenceProvider().evaluate(snapshot, spec)
    assert set(response.probabilities) == {"LONG", "SHORT", "NO_TRADE"}
    assert sum(response.probabilities.values()) == pytest.approx(1.0)
    assert response.probabilities["LONG"] > response.probabilities["SHORT"]
    assert response.probabilities["NO_TRADE"] > 0
    assert "not empirically calibrated" in response.rationale[0]


def test_native_provider_rejects_missing_or_invalid_direction():
    spec = parse_decision_spec(
        "DECISION pretrade:\nSTATE:\n - direction\n - composite_score\nASK side: CHOICE [LONG, SHORT, NO_TRADE]"
    )
    snapshot = MarketSnapshot(
        "BTCUSDT",
        datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        {"direction": "FLAT", "composite_score": 80.0},
    )
    with pytest.raises(ValueError, match="direction"):
        NativeEvidenceProvider().evaluate(snapshot, spec)


def test_native_provider_supplies_typed_score_and_neutral_noul_prior():
    spec = parse_decision_spec(
        "DECISION pretrade:\\nSTATE:\\n - direction\\n - composite_score\\n"
        "ASK direction: CHOICE [LONG, SHORT, NO_TRADE]\\n"
        "ASK entry_quality: SCORE 1..5\\n"
        'ASK reversal: NOUL "a liquidity sweep is a genuine reversal"'
    )
    snapshot = MarketSnapshot(
        "BTCUSDT",
        datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        {"direction": "LONG", "composite_score": 80.0},
    )
    response = NativeEvidenceProvider().evaluate(snapshot, spec)
    assert 1.0 <= response.scores["entry_quality"] <= 5.0
    assert response.nouls["reversal"] == 0.5
