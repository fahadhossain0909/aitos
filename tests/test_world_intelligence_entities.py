from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.entity_resolution import EntityAliasRegistry
from aitos.world_intelligence.narratives import cluster_narratives
from aitos.world_intelligence.pipeline import enrich_event_batch


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(source_id, title, uri):
    source = SourceEvidence(
        source_id=source_id, source_kind="news", uri=uri,
        published_at=NOW - timedelta(minutes=2), first_seen_at=NOW - timedelta(minutes=1),
        reliability=0.9,
    )
    return IntelligenceEvent(
        event_id=source_id, event_type="corporate_action", title=title,
        entities=(), published_at=source.published_at, first_seen_at=source.first_seen_at,
        source=source, sentiment=0.2, severity=0.4, novelty=0.5, confidence=0.5,
    )


def test_alias_resolution_uses_boundaries_and_longest_matches():
    registry = EntityAliasRegistry({"BTC": ["bitcoin"], "BTC ETF": ["bitcoin etf"]})
    assert registry.resolve("Bitcoin ETF approval") == ("BTC ETF",)
    assert registry.resolve("alphabet soup") == ()
    assert registry.resolve("BTC and Bitcoin") == ("BTC",)


def test_alias_registry_rejects_conflicting_aliases():
    registry = EntityAliasRegistry({"BTC": ["bitcoin"]})
    try:
        registry.register("ETH", ["bitcoin"])
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting alias should fail")


def test_narratives_cluster_shared_entity_and_lexical_overlap_only():
    events = [
        make_event("one", "Nvidia announces new AI chip production", "https://a.example/1"),
        make_event("two", "Nvidia announces AI chip production expansion", "https://b.example/2"),
        make_event("three", "Bitcoin ETF approval news", "https://c.example/3"),
    ]
    enriched = [
        event.__class__(**{**event.__dict__, "entities": ("NVDA",)}) if False else event
        for event in events
    ]
    from dataclasses import replace
    enriched[0] = replace(enriched[0], entities=("NVDA",))
    enriched[1] = replace(enriched[1], entities=("NVDA",))
    enriched[2] = replace(enriched[2], entities=("BTC",))
    clusters = cluster_narratives(enriched, as_of=NOW)
    assert len(clusters) == 1
    assert clusters[0].event_ids == ("one", "two")
    assert clusters[0].source_ids == ("one", "two")


def test_enrichment_resolves_entities_and_marks_only_configured_primary_publishers():
    primary = make_event("sec", "Nvidia files annual report", "https://sec.example/1")
    registry = EntityAliasRegistry({"NVDA": ["nvidia"]})
    result = enrich_event_batch(
        [primary], publisher_groups={"sec": "sec.gov"}, as_of=NOW, registry=registry
    )
    assert result.events[0].entities == ("NVDA",)
    assert result.events[0].verification == "single_source"
    assert result.verification["sec"].verified is False


def test_primary_source_does_not_promote_every_event_in_a_cluster():
    from dataclasses import replace

    primary = replace(
        make_event("sec", "Nvidia announces AI chip production expansion", "https://sec.example/1"),
        source=replace(
            make_event("sec", "Nvidia announces AI chip production expansion", "https://sec.example/1").source,
            is_primary=True,
            reliability=0.99,
        ),
    )
    syndicated = make_event(
        "wire", "Nvidia announces AI chip production expansion", "https://wire.example/1"
    )
    result = enrich_event_batch(
        [primary, syndicated],
        publisher_groups={"sec": "sec.gov", "wire": "wire-service"},
        as_of=NOW,
        registry=EntityAliasRegistry({"NVDA": ["nvidia"]}),
    )
    by_source = {event.source.source_id: event for event in result.events}
    assert by_source["sec"].verification == "primary_source"
    assert by_source["wire"].verification == "unverified"
