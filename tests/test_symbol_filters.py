from aitos.exchange.symbol_filters import SymbolFilters, parse_exchange_info

SAMPLE_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "quantityPrecision": 3,
            "pricePrecision": 1,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        },
        {
            "symbol": "1000SHIBUSDT",
            "quantityPrecision": 0,
            "pricePrecision": 6,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
                {"filterType": "LOT_SIZE", "stepSize": "1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        },
    ]
}


def test_round_quantity_rounds_down_to_step():
    filters = SymbolFilters(
        symbol="BTCUSDT",
        step_size=0.001,
        tick_size=0.1,
        min_notional=5.0,
        quantity_precision=3,
        price_precision=1,
    )
    assert filters.round_quantity(1.23456) == 1.234
    assert filters.round_quantity(1.999999) == 1.999


def test_round_quantity_handles_small_step_sizes_without_float_error():
    filters = SymbolFilters(
        symbol="1000SHIBUSDT",
        step_size=1.0,
        tick_size=0.000001,
        min_notional=5.0,
        quantity_precision=0,
        price_precision=6,
    )
    assert filters.round_quantity(1234.9) == 1234.0


def test_round_price_rounds_down_to_tick():
    filters = SymbolFilters(
        symbol="BTCUSDT",
        step_size=0.001,
        tick_size=0.1,
        min_notional=5.0,
        quantity_precision=3,
        price_precision=1,
    )
    assert filters.round_price(100.37) == 100.3


def test_round_step_with_zero_step_returns_original_value():
    filters = SymbolFilters(
        symbol="X",
        step_size=0.0,
        tick_size=0.0,
        min_notional=0.0,
        quantity_precision=0,
        price_precision=0,
    )
    assert filters.round_quantity(1.23456) == 1.23456
    assert filters.round_price(100.789) == 100.789


def test_meets_min_notional_true_and_false():
    filters = SymbolFilters(
        symbol="BTCUSDT",
        step_size=0.001,
        tick_size=0.1,
        min_notional=10.0,
        quantity_precision=3,
        price_precision=1,
    )
    assert filters.meets_min_notional(price=100.0, quantity=0.2) is True  # 20.0 >= 10.0
    assert filters.meets_min_notional(price=100.0, quantity=0.05) is False  # 5.0 < 10.0


def test_parse_exchange_info_extracts_all_symbols():
    parsed = parse_exchange_info(SAMPLE_EXCHANGE_INFO)
    assert set(parsed.keys()) == {"BTCUSDT", "1000SHIBUSDT"}


def test_parse_exchange_info_extracts_correct_filter_values():
    parsed = parse_exchange_info(SAMPLE_EXCHANGE_INFO)
    btc = parsed["BTCUSDT"]
    assert btc.step_size == 0.001
    assert btc.tick_size == 0.10
    assert btc.min_notional == 5.0
    assert btc.quantity_precision == 3
    assert btc.price_precision == 1


def test_parse_exchange_info_handles_missing_filters_gracefully():
    raw = {
        "symbols": [
            {
                "symbol": "NEWCOIN",
                "quantityPrecision": 0,
                "pricePrecision": 0,
                "filters": [],
            }
        ]
    }
    parsed = parse_exchange_info(raw)
    assert parsed["NEWCOIN"].step_size == 0.0
    assert parsed["NEWCOIN"].tick_size == 0.0
    assert parsed["NEWCOIN"].min_notional == 0.0
