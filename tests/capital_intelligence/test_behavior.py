from datetime import datetime, timezone
from aitos.capital_intelligence.behavior import assess_capital_behavior
from aitos.capital_intelligence.contracts import CapitalEvent

def item(event_id,sign):
    now=datetime.now(timezone.utc)
    return CapitalEvent(event_id,"test","onchain","crypto","chain","USDC","transfer",now,now,100,"USDC",100,actor_id="wallet:a",confidence=0.9,metadata={"flow_sign":sign})

def test_unknown_direction_is_not_buy_or_sell():
    result=assess_capital_behavior("wallet:a",[item("1",0)])
    assert result.accumulation_score==0 and result.distribution_score==0

def test_explicit_direction_drives_descriptive_score():
    result=assess_capital_behavior("wallet:a",[item("1",1),item("2",0)])
    assert result.accumulation_score==0.5
    assert result.classification=="mixed_or_unclassified"
