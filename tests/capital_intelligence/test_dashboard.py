from scripts.run_capital_intelligence import _entity_dashboard_html


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
