from datetime import date

from aitos.data.binance_historical import daily_archives, trade_archive


def test_binance_trade_archive_is_public_and_dataset_mapped():
    item = trade_archive("BTCUSDT", "futures_um", date(2026, 1, 1))
    assert item.dataset == "trades"
    assert item.filename == "BTCUSDT-trades-2026-01-01.zip"
    assert item.url.startswith("https://data.binance.vision/")
    assert "BTCUSDT-trades-2026-01-01.zip" in item.url


def test_daily_archives_have_distinct_dataset_roles():
    items = daily_archives("BTCUSDT", "futures_um", date(2026, 1, 1))
    assert {item.dataset for item in items} == {"trades", "orderbook_updates"}
