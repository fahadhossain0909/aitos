# High-Volume, Low-Value Data Streams in AITOS

## Executive Summary

The AITOS codebase contains several data streams that consume disproportionate resources relative to their downstream consumption. The worst offenders are in the **auxiliary runtime** (tickers, funding, OI) and the **legacy kline stream**, both of which fan out to hundreds of symbols but have few documented consumers.

---

## 1. Orderbook Snapshots — ~3.3M Published Events

### Source
- `market_data/binance_adapter.py` — `book_snapshot_event()` yields snapshots from the exchange info / REST poll path.
- `market_data/bridge_service.py` (line 160) bridges legacy book events into the canonical bus.
- `market_os_persistence.py` subscribes to `market.orderbook.*` (line 204–209) for ClickHouse persistence.
- `persistence_sink.py` (`CanonicalMarketDataPersistenceSink`) is the main consumer — but it **filters aggressively**:

### Filtering (the "3.1M filtered" signal)
```python
# persistence_sink.py lines 113–134
if event.symbol.upper() not in self._historical_books:  # only ("BTCUSDT", "LTCUSDT")
    self._filtered += 1; return
# PLUS 1-second book_interval throttle per symbol
```

- `historical_book_symbols` defaults to `("BTCUSDT", "LTCUSDT")` (line 33–34 of persistence_sink.py).
- Ingestion wires it to `DEEP_HISTORICAL_SYMBOLS = ("BTCUSDT", "LTCUSDT")` (line 31 of ingestion.py).
- **Any book_snapshot for symbols other than BTC/LTC is counted as `_filtered` and dropped before entering the queue.**
- The Redis retention for `market.book.snapshot` is capped at 5,000 entries (`retention.py` line 23) — yet the bus still has to serialize, publish, and trim every snapshot event for the full universe.

### Volume estimate
- If ~3.3M book_snapshot events were published and ~3.1M were filtered, that is **~94% filtered**.
- At an estimated ~500 bytes/event (20-level bid/ask JSON), that is **~1.5 GB of serialized JSON published into Redis** to persist only ~200K events (6%).

### Downstream consumers
- `CanonicalMarketDataPersistenceSink` — only BTC/LTC.
- `LegacyMarketDataBridge` — only for configured legacy symbols (typically a handful).
- `market_os_persistence.py` — persists to ClickHouse `order_book_events`.
- **No other documented consumer reads `market.book.snapshot` from the canonical bus.**

### Verdict: **HIGH-VOLUME, LOW-VALUE** for non-BTC/LTC snapshots.

---

## 2. Legacy Kline Stream (`_run_kline_stream` in ingestion_legacy.py)

### Source
- `ingestion_legacy.py` line 255–268: `_run_kline_stream()` calls `self._exchange.stream_klines(self._symbols, self._kline_timeframe)`.
- `self._symbols` is the full resolved USDT universe — typically **~850+ symbols** on Binance USDT-M.
- `_handle_kline()` (line 525–536) publishes `market.kline.{symbol}.{timeframe}` AND calls `repository.save_kline(kline)`.

### The problem
- The **canonical runtime** (`CanonicalMarketDataRuntime`) already has its own kline stream, bounded to `KLINE_SYMBOL_LIMIT = 5` symbols (runtime.py line 27).
- The **legacy stream runs in parallel** for the full universe.
- The scanner's staged pipeline (`staged_scanner.py`) already subscribes to kline updates via `update_live_kline_symbols(symbols)` at the TOP_5 stage (ingestion.py line 204–205), capped at 5 symbols (ingestion.py line 37: `LIVE_KLINE_SYMBOLS = 5`).

### Volume estimate
- ~850 symbols × 1 kline/minute (1m timeframe) = **~850 events/min** just from the legacy stream.
- At ~200 bytes/event, that is **~170 KB/min** — not large in bytes but:
- Each event triggers a Redis publish + a ClickHouse `save_kline` INSERT.
- That is **~122K ClickHouse INSERTs/day** for klines that are never read by any consumer except the scanner (which uses its own bounded 5-symbol subscription).

### Downstream consumers
- `_handle_kline` publishes to `market.kline.*` and saves to ClickHouse.
- **Canonical runtime kline consumer**: only `market_data-klines` task (max 5 symbols).
- `TradeLifecycle.update_price()` subscribes to `market.kline.{symbol}.{timeframe}` — but only for symbols with open positions (very few).
- `MarketAgent` subscribes to `market.kline.*` — but it only needs the scanner's output, not raw 1m klines for 850 symbols.
- `app.py` line 283 lists `"market.kline"` as a subscribed channel but the handlers are the same lifecycle/agents.

### Verdict: **HIGH-VOLUME, LOW-VALUE** for non-top-5 symbols. The legacy stream duplicates the canonical stream at 170× the symbol count.

---

## 3. Auxiliary Runtime — Tickers, Funding, Open Interest, Instruments, Liquidations

### Source
- `auxiliary_runtime.py` (line 17–154): `BinanceAuxiliaryMarketDataRuntime` starts **5 separate producers**.
- All producers fan out to `self.symbols[:50]` (line 28) — updated via `update_symbols()` to the scanner's TOP_50.
- `binance_auxiliary.py` line 19: `TOP_SYMBOL_LIMIT = 50`.

### Streams and their consumers

| Stream | Redis channel | Maxlen | Documented consumers |
|--------|--------------|--------|---------------------|
| Tickers | `market.ticker` | 10,000 | `MarketAgent` (app.py) |
| Funding | `market.funding` | 5,000 | `Scanner._last_oi` (weak) |
| Open Interest | `market.open_interest` | 10,000 | `Scanner._last_oi` |
| Liquidations | `market.liquidation` | 25,000 | None documented |
| Instruments | `market.instrument` | 2,000 | None documented |

### Volume estimate
- 50 symbols × 1 ticker/sec = **~4.3M ticker events/day**.
- 50 symbols × 1 funding/8hrs = ~150 funding events/day.
- 50 symbols × 1 OI/min (REST-polled) = **~72K OI events/day**.
- Liquidations: bursty but can be thousands during volatility.

### The waste
- **Instruments**: `INSTRUMENT_POLL_SECONDS = 300.0` — polled 5-min REST for 50 symbols. No consumer. **Pure waste.**
- **Liquidations**: published to Redis with 25K maxlen, but `allowed = {s.upper() for s in (symbols or [])}` — if no symbols are passed, all liquidations are published. No downstream consumer reads `market.liquidation` from the bus.
- **Funding**: `Scanner._last_oi` in scanner.py line 170 stores it, but it's only used opportunistically. Not a critical path.
- **Tickers**: `MarketAgent` subscribes but primarily uses them for reference. Not a high-value consumer.

### Verdict: **MODERATE-VOLUME, LOW-VALUE** for instruments and liquidations. Tickers and OI have some value but fan out to 50 symbols when only the scanner's top-5/top-2 really matter.

---

## 4. Trade Stream (legacy) — Duplicates Canonical Trades

### Source
- `ingestion_legacy.py` line 270–340: `_run_trade_stream()` uses `self._exchange.stream_trades(self._symbols)`.
- When `canonical_mode=True` (production with scanner), this is **disabled** (line 149–162).
- BUT: in non-canonical mode (backtest, testing, or direct handler mode), the legacy trade stream runs for the full universe.

### Verdict: **GUARDED** — disabled in production canonical mode, but a foot-gun for test/backtest entrypoints.

---

## 5. Live State Stream

### Source
- `ingestion_legacy.py` line 595–603: `_publish_live_state()` publishes `market.live_state.{symbol}` for every symbol with trades.
- `market_os_persistence.py` line 198–202: subscribes to `market.live_state.*`.

### Volume estimate
- For each trade processed, a full live-state snapshot is published.
- At ~1,000 trades/sec across 850 symbols, that is **~1,000 live-state events/sec** each containing order flow JSON, liquidity events, etc. (~1–2 KB each = **~10 MB/min**).

### The waste
- `market_os_persistence` is the only consumer (for ClickHouse `market_live_state`).
- The snapshot contains the full live state including liquidity events JSON — a lot of data for a single analytics table.
- Redis maxlen for `market.live_state.*` is 25,000 (docs/REDIS_RETENTION_AND_ARCHIVE.md line 20).

### Verdict: **HIGH-VOLUME, MODERATE-VALUE** — the data is useful for analytics but the volume is very high relative to consumption.

---

## Quantification Summary

| Stream | Symbols | Events/day | % Consumed | Verdict |
|--------|---------|-----------|------------|---------|
| book_snapshot (non-BTC/LTC) | ~850 | ~3.1M | ~0% (filtered) | **HIGH-WASTE** |
| legacy kline (non-top-5) | ~850 | ~122K | ~0% (duplicated) | **HIGH-WASTE** |
| auxiliary ticker | 50 | ~4.3M | Low | **MODERATE-WASTE** |
| auxiliary funding | 50 | ~150 | Low | LOW-WASTE |
| auxiliary OI | 50 | ~72K | Low | **MODERATE-WASTE** |
| auxiliary liquidation | all | bursty | ~0% | **HIGH-WASTE** |
| auxiliary instrument | 50 | ~2.9K | 0% | **PURE-WASTE** |
| legacy trade (non-canonical) | ~850 | variable | variable | GUARDED |
| live state | all with trades | ~864K | Single consumer | MODERATE |

---

## Key Files Examined

- `aitos/market_data/universe.py` — `resolve_live_universe()` returns all USDT symbols (~850+).
- `aitos/market_data/runtime.py` — `KLINE_SYMBOL_LIMIT = 5`, `KLINE_TIMEFRAME = "1m"`.
- `aitos/market_data/persistence_sink.py` — `historical_book_symbols = ("BTCUSDT", "LTCUSDT")`, aggressive filtering.
- `aitos/market_data/auxiliary_runtime.py` — 5 producers for 50 symbols, instruments have no consumer.
- `aitos/market_data/binance_auxiliary.py` — `TOP_SYMBOL_LIMIT = 50`, `INSTRUMENT_POLL_SECONDS = 300.0`.
- `aitos/market_data/retention.py` — Redis maxlen caps.
- `aitos/data/market_os_persistence.py` — subscribes to 4 market topics + 11 analytics topics.
- `aitos/data/ingestion.py` — `LIVE_KLINE_SYMBOLS = 5`, `LIVE_DEEP_NON_BTC = 2`, canonical mode.
- `aitos/data/ingestion_legacy.py` — legacy kline stream, full universe.
- `aitos/intelligence/staged_scanner.py` — ALL→50→25→10→5→2 pipeline.
- `aitos/intelligence/position_runtime.py` — `_merge_symbols()`, `MAX_DEEP_SYMBOLS = 6`.
- `run_paper_trading.py` — `KLINE_TIMEFRAME = "15m"`.

---

## Recommendations

1. **Disable legacy `_run_kline_stream` when canonical mode is active** — the canonical runtime already handles klines for the top-5. The legacy stream is redundant for 845+ symbols.

2. **Narrow `BinanceAuxiliaryMarketDataRuntime` symbol list** — cap at scanner's top-5 or top-10 instead of 50. Instruments/liquidations should be opt-in.

3. **Fix `persistence_sink` filtering** — the 3.1M filtered book snapshots are still being **published to Redis** before being filtered. Apply the filter at the **bus subscription** level (use `start_id` and narrow topics) to avoid paying publish/trim cost for 94% of events.

4. **Instrument polling** — `stream_instruments` has zero consumers. Disable by default or make it opt-in via env var.

5. **Liquidation stream** — gate by configured symbols (currently publishes all if no symbols passed) or make it opt-in.

6. **Live state throttling** — currently publishes on every trade. Add a throttle (e.g., max 1 live-state update/sec per symbol) to reduce volume by ~1000×.
