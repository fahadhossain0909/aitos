from datetime import datetime, timezone
import pytest
from aitos.capital_intelligence.contracts import CapitalEvent, EntityEvidence

def event(**kwargs):
    now=datetime.now(timezone.utc)
    values=dict(event_id="e1",source_id="test",source_kind="trade",market="crypto",venue="binance",instrument="BTCUSDT",event_type="large_trade",event_time=now,observed_time=now,quantity=1,unit="BTC",usd_value=50000,actor_id="entity-a")
    values.update(kwargs)
    return CapitalEvent(**values)

def test_capital_event_has_stable_identity_and_utc_serialization():
    item=event()
    assert item.deduplication_key==item.deduplication_key
    assert item.to_dict()["event_time"].endswith("+00:00")

def test_capital_event_rejects_naive_timestamp_and_nan():
    with pytest.raises(ValueError): event(event_time=datetime.now())
    with pytest.raises(ValueError): event(quantity=float("nan"))

def test_relationship_confidence_does_not_imply_verified_ownership():
    link=EntityEvidence("wallet:a","wallet:b","funded_by","onchain","tx:1",datetime.now(timezone.utc),0.99)
    assert link.status=="candidate"
