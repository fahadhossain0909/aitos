from aitos.storage.reconciliation import compare_fingerprints, compare_records


def test_reconciliation_reports_missing_records_in_both_directions():
    shared = {"record_type": "DECISION", "entity_id": "d1", "payload": {"x": 1}}
    source = [
        shared,
        shared,
        {"record_type": "OUTCOME", "entity_id": "d1", "payload": {"pnl": 2}},
    ]
    target = [
        shared,
        {"record_type": "TRADE_LINK", "entity_id": "d1", "payload": {"trade_id": "t1"}},
    ]

    result = compare_records(source, target)

    assert result.source_count == 3
    assert result.target_count == 2
    assert result.matched_count == 1
    assert len(result.source_only) == 2
    assert len(result.target_only) == 1
    assert len(result.source_duplicate_fingerprints) == 1
    assert not result.clean


def test_reconciliation_can_ignore_only_explicit_volatile_fields():
    source = [
        {
            "record_type": "TRADE_SNAPSHOT",
            "entity_id": "t1",
            "payload": {"trade_id": "t1", "state": "OPEN", "recorded_at": "2026-01-01"},
        }
    ]
    target = [
        {
            "record_type": "TRADE_SNAPSHOT",
            "entity_id": "t1",
            "payload": {"trade_id": "t1", "state": "OPEN", "recorded_at": "2026-01-02"},
        }
    ]

    result = compare_records(
        source, target, excluded_payload_fields=frozenset({"recorded_at"})
    )

    assert result.clean
    assert result.matched_count == 1



def test_matching_duplicates_are_still_a_reconciliation_anomaly():
    duplicate = "same-fingerprint"

    result = compare_fingerprints([duplicate, duplicate], [duplicate, duplicate])

    assert result.source_count == result.target_count == 2
    assert result.matched_count == 2
    assert result.source_duplicate_fingerprints == (duplicate,)
    assert result.target_duplicate_fingerprints == (duplicate,)
    assert not result.clean
