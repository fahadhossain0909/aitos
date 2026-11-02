# Entry Quality & Adverse-Selection Improvements (V1)

## Scope

This change improves entry evidence without adding conventional lagging indicators or changing the existing CHoCH CVD thresholds (LONG >= 6.5; SHORT <= 3.5). It is intentionally conservative and is not a claim that losing trades can be eliminated.

## Implemented

- REST Kline parsing derives `is_closed` from the exchange candle close timestamp. Binance REST fetches return completed candles only, and the scanner independently filters incomplete candles from adapter implementations.
- Futures universe selection now requires eligible USDT perpetual metadata (TRADING / PERPETUAL / USDT). A fixed core list is used as a baseline; other contracts must pass configurable 24h quote-volume and trade-count floors and are ranked by quote volume. If the ticker endpoint is unavailable, the resolver falls back to core contracts only rather than widening to every USDT suffix.
- Scanner universe controls are centralized under `ScannerTuning`: `core_symbols`, `min_quote_volume_usd`, `min_trade_count`, `dynamic_universe_limit`, and existing `universe_limit`. Environment overrides use the `AITOS_TUNING_SCANNER_*` prefix.
- Liquidity execution quality no longer averages in directional depth imbalance, sweep risk, or absorption risk. These are different concepts and must not improve a generic execution-quality score merely because a book is fragile or one-sided.
- Added consecutive-snapshot best-quote OFI, oriented to the proposed trade direction.
- Added an adverse-flow/no-price-progress veto when there are enough executions and a clear aggressive-side imbalance. This is a conservative warning, not proof of a hidden participant.
- Added breakout acceptance and direction-aware BTC-relative-strength features to the candidate score.
- Replaced momentum/range-chasing in the lightweight scanner shortlist with median quote-activity proxy discounted by range instability.
- Live trading now refuses to authorize a candidate if live WebSocket trade/book state is stale or unavailable. Paper mode retains REST fallback for research/diagnostics, and the source is recorded in the rationale.
- Fixed rolling CVD accounting so evicted trades are removed from the bounded rolling CVD window.
- Existing lifecycle MAE/MFE fields and entry-thesis persistence are reused. The journal outcome event now carries the original entry component snapshot plus P&L, exit reason and MAE/MFE, and the learning recorder persists those fields together so entry evidence can be joined to realized outcomes.

## Community-practice evidence considered

Trader discussions repeatedly raise the risk of reading static depth as genuine liquidity, the need to distinguish aggressive execution from price progress, and the value of waiting for a break to hold/retest rather than entering on the first wick. These are treated as hypotheses and qualitative experience, not as statistically validated trading rules:

- Reddit discussion on order-book persistence, CVD timeframes and OI context: https://www.reddit.com/r/Trading/comments/1wlidty/how_do_you_actually_analyze_order_book_oi_buysell/
- Reddit discussion of absorption versus exhaustion: https://www.reddit.com/r/OrderFlow_Trading/comments/1nc3r3r/delta_cvd_help_absorption_vs_exhaustion/
- Practitioner discussion of sweep versus accepted breakout: https://learn.mindpillar.com/blog/liquidity-sweep-or-real-breakout-an-order-flow-confirmation-framework
- 2026 working paper on BTC perpetual passive-buy adverse-selection and flow-adjusted absorption: https://papers.ssrn.com/sol3/Delivery.cfm/6693260.pdf?abstractid=6693260&mirid=1

Anecdotes and working papers are not proof of profitability. The implementation therefore exposes evidence and conservative vetoes, while performance must be evaluated with paper trading and out-of-sample replay.

## Configuration defaults

- `AITOS_TUNING_SCANNER_CORE_SYMBOLS`: comma-separated trusted core contracts.
- `AITOS_TUNING_SCANNER_MIN_QUOTE_VOLUME_USD`: default 10,000,000 USDT 24h quote volume.
- `AITOS_TUNING_SCANNER_MIN_TRADE_COUNT`: default 10,000 24h trades.
- `AITOS_TUNING_SCANNER_DYNAMIC_UNIVERSE_LIMIT`: default 35 additional contracts.
- `AITOS_TUNING_SCANNER_UNIVERSE_LIMIT`: existing overall scanner subscription cap.

These defaults should be reviewed against the observed AITOS paper-trading universe and order sizes before production use.

## Important limitations / next validation

- Best-quote OFI uses successive scanner snapshots. It is not full-depth, event-by-event OFI. A dedicated depth-delta accumulator with persistence/cancel-replenishment metrics is a future enhancement.
- Breakout acceptance is a ranking feature, not a mandatory retest gate. This avoids suppressing all early trend entries before sufficient replay evidence exists.
- Absorption veto uses recent executed trades and endpoint price progress; it cannot identify iceberg orders or prove institutional intent.
- Universe volume floors are configurable defaults, not universal constants. Validate thresholds against actual AITOS order size, spread, slippage and funding costs.
- Existing MAE/MFE telemetry should be joined to entry-time component snapshots to compare immediate adverse markout, net expectancy, profit factor, MAE/MFE and missed-trade rate.
- Run unit tests, then historical replay / paper trading. Do not enable live deployment based on unit tests alone.
