import pytest

from aitos.world_intelligence.financial_nlp import analyze_financial_headline


def test_financial_nlp_detects_negative_security_event():
    result = analyze_financial_headline("Exchange reports hack and stolen funds")
    assert result.event_type == "security"
    assert result.sentiment < 0
    assert result.severity == 0.9
    assert result.impact_horizon == "immediate"
    assert result.confidence <= 0.55


def test_financial_nlp_detects_positive_earnings_language():
    result = analyze_financial_headline(
        "Company beats estimates and raises guidance after record profit"
    )
    assert result.event_type == "earnings"
    assert result.sentiment > 0
    assert "beats estimates" in result.matched_terms


def test_financial_nlp_handles_simple_negation_conservatively():
    result = analyze_financial_headline("Company denies fraud allegations")
    assert result.sentiment == 0.0
    assert result.confidence == 0.05


def test_financial_nlp_does_not_escalate_negated_security_event():
    result = analyze_financial_headline("No hack detected by exchange security team")
    assert result.severity == 0.1
    assert result.sentiment == 0.0


def test_financial_nlp_unknown_headline_is_neutral_and_low_confidence():
    result = analyze_financial_headline("Company announces a new office")
    assert result.sentiment == 0.0
    assert result.confidence == 0.05
    assert result.event_type == "general_news"


def test_financial_nlp_rejects_empty_or_oversized_text():
    with pytest.raises(ValueError):
        analyze_financial_headline(" ")
    with pytest.raises(ValueError):
        analyze_financial_headline("x" * 20_001)
