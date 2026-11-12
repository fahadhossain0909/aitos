"""Tests for durable candidate-linked liquidity evidence snapshots."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitos.intelligence.contextual_scanner_bridge import (
    _persist_liquidity_evidence,
)


class _EvidenceService:
    def __init__(self):
        self.rows = []

    async def persist(self, candidate_id, evidence):
        self.rows.append((candidate_id, evidence))
        return "evidence-id-1"


class _Analysis:
    def to_dict(self):
        return {"levels": [], "sweeps": [], "mode": "shadow"}


@pytest.mark.asyncio
async def test_liquidity_evidence_persists_with_candidate_and_candle_identity():
    close_time = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
    service = _EvidenceService()
    scanner = SimpleNamespace(
        _aitos_historical_intelligence=service,
        _timeframe="15m",
    )
    candidate = SimpleNamespace(
        _aitos_candidate_id="candidate-1",
        symbol="btcusdt",
        _aitos_klines=(SimpleNamespace(close_time=close_time, timeframe="15m"),),
    )
    evidence_id = await _persist_liquidity_evidence(scanner, candidate, _Analysis())
    assert evidence_id == "evidence-id-1"
    candidate_id, evidence = service.rows[0]
    assert candidate_id == "candidate-1"
    assert evidence["symbol"] == "BTCUSDT"
    assert evidence["timeframe"] == "15m"
    assert evidence["captured_at"] == close_time.isoformat()
    assert evidence["liquidity_intelligence"]["mode"] == "shadow"


@pytest.mark.asyncio
async def test_liquidity_evidence_skips_when_candidate_identity_is_missing():
    scanner = SimpleNamespace(
        _aitos_historical_intelligence=_EvidenceService(),
        _timeframe="15m",
    )
    candidate = SimpleNamespace(
        symbol="BTCUSDT",
        _aitos_klines=(SimpleNamespace(close_time=datetime.now(timezone.utc)),),
    )
    assert await _persist_liquidity_evidence(scanner, candidate, _Analysis()) is None
