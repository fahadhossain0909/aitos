from datetime import datetime, timezone

from aitos.data.postgres_repository import PostgresMarketDataRepository


def test_postgres_market_adapter_declares_paper_runtime_capabilities():
    repo = PostgresMarketDataRepository("postgresql://placeholder")
    assert {
        "market_event_writes",
        "live_analytics_writes",
        "ohlcv_writes",
        "ohlcv_recent_reads",
        "orderbook_writes",
        "depth_event_writes",
        "trade_tick_writes",
        "funding_writes",
        "open_interest_writes",
        "learning_experience_writes",
    }.issubset(repo.CAPABILITIES)


def test_market_record_timestamp_normalizes_naive_datetime_to_utc():
    value = datetime(2026, 11, 8, 12, 0, 0)
    assert PostgresMarketDataRepository._as_datetime(value).tzinfo == timezone.utc


def test_market_record_timestamp_parses_iso_utc_string():
    value = PostgresMarketDataRepository._as_datetime("2026-11-08T12:00:00Z")
    assert value.isoformat() == "2026-11-08T12:00:00+00:00"


def test_market_record_payload_decoder_handles_asyncpg_jsonb_string():
    payload = PostgresMarketDataRepository._decode_payload(
        '{"symbol":"BTCUSDT","timeframe":"15m","close":65000.0}'
    )
    assert payload["symbol"] == "BTCUSDT"
    assert payload["close"] == 65000.0
