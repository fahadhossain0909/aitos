import pytest

from scripts.run_capital_intelligence import (
    _configured_entity_labels,
    _entity_dashboard_html,
)


def test_entity_dashboard_is_read_only_and_does_not_persist_bearer_token():
    page = _entity_dashboard_html().decode("utf-8")
    assert "Capital Flow &amp; Entity Explorer" in page
    assert 'type="password"' in page
    assert "sessionStorage" not in page
    assert "localStorage" not in page
    assert "/v1/entities?limit=100&offset=0" in page
    assert "/v1/entities/'+encodeURIComponent(entity.chain)" in page
    assert "beneficial_owner_verified" in page
    assert "not a trading signal" in page.lower()
    assert "innerHTML" not in page


def test_entity_dashboard_uses_text_content_for_untrusted_entity_fields():
    page = _entity_dashboard_html().decode("utf-8")
    assert "td.textContent=String(value??'—')" in page
    assert "detail.textContent=JSON.stringify" in page


def test_operator_entity_labels_are_chain_scoped_and_time_bounded():
    labels = _configured_entity_labels(
        [
            {
                "chain": "1",
                "address": "0x" + "a" * 40,
                "label": "Exchange hot wallet",
                "category": "exchange",
                "source": "operator-review-ticket-123",
                "verified": True,
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": "2027-01-01T00:00:00Z",
            }
        ]
    )
    assert len(labels) == 1
    assert labels[0].entity_id == "1:" + "0x" + "a" * 40
    assert labels[0].category == "exchange"
    assert labels[0].verified is True
    assert labels[0].valid_from.tzinfo is not None
    assert labels[0].valid_until.tzinfo is not None


def test_operator_entity_labels_reject_unverified_schema_and_naive_dates():
    with pytest.raises(ValueError, match="invalid CFEI operator entity label"):
        _configured_entity_labels(
            [
                {
                    "chain": "1",
                    "address": "wallet",
                    "label": "Exchange",
                    "category": "exchange",
                    "source": "operator",
                    "verified": "true",
                    "valid_from": "2026-01-01T00:00:00Z",
                }
            ]
        )
    with pytest.raises(ValueError, match="timezone"):
        _configured_entity_labels(
            [
                {
                    "chain": "1",
                    "address": "wallet",
                    "label": "Exchange",
                    "category": "exchange",
                    "source": "operator",
                    "valid_from": "2026-01-01T00:00:00",
                }
            ]
        )
