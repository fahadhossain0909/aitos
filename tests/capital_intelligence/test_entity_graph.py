from datetime import datetime,timezone,timedelta
from aitos.capital_intelligence.contracts import EntityEvidence,EntityLabel
from aitos.capital_intelligence.entity_graph import EntityEvidenceGraph

def test_candidate_relationship_does_not_cluster():
    graph=EntityEvidenceGraph(); now=datetime.now(timezone.utc)
    graph.add_evidence(EntityEvidence("wallet:a","wallet:b","funded_by","onchain","tx:1",now,0.99))
    assert graph.candidate_cluster("wallet:a")==set()

def test_verified_direct_relationship_only():
    graph=EntityEvidenceGraph(); now=datetime.now(timezone.utc)
    graph.add_evidence(EntityEvidence("wallet:a","wallet:b","operator_confirmed_link","signed_attestation","ref:1",now,1.0,"verified"))
    graph.add_evidence(EntityEvidence("wallet:b","wallet:c","funded_by","onchain","tx:2",now,1.0,"verified"))
    assert graph.candidate_cluster("wallet:a")=={"wallet:b"}
    assert "wallet:c" not in graph.candidate_cluster("wallet:a")

def test_label_validity_window_is_enforced():
    now=datetime.now(timezone.utc)
    EntityLabel("wallet:a","exchange hot wallet","custody","official disclosure",True,now,now+timedelta(days=1))
