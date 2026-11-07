# Entry Quality & Adverse-Selection Improvements (V1)

## Scope

This change improves entry evidence without adding conventional lagging indicators or changing the existing CHoCH CVD thresholds (LONG >= 6.5; SHORT <= 3.5). It is intentionally conservative and is not a claim that losing trades can be eliminated.

## Implemented

- REST Kline parsing derives `is_closed` from the exchange candle close timestamp. Binance REST fetches return completed candles only, and the scanner independently filters incomplete candles from adapter implementations.
- Futures universe selection now requires eligible USDT perpetual metadata (TRADING / PERPETUAL / USDT). A fixed core list is used as a baseline; other contracts must pass configurable 24h quote-volume and trade-count floors and are ranked by quote volume. If the ticker endpoint is unavailable, the resolver falls back to core contracts only rather than widening to every USDT suffix.
- Scanner universe controls are centralized under `ScannerTuning`: `core_symbols`, `min_quote_volume_usd`, `min_trade_count`, `dynamic_universe_limit`, and existing `universe_limit`. Environment overrides use the `AITOS_TUNING_SCANNER_*` prefix.
- Liquidity execution quality no longer averages in directional depth imbalance, sweep risk, or absorption risk. These are different concepts and must not improve a generic execution-quality score merely because a book is fragile or one-sided.
- Added sequenced full-depth event OFI across every changed price level, distance-weighted from the mid, and aggregated over a rolling live event window.
- Added same-price liquidity reduction/re-add tracking over a 5-second horizon to expose inferred cancellation pressure and replenishment.
- Added bounded trade-event matching for depth reductions: public aggTrade executions are matched by symbol, aggressor side, exact price level and a 500-millisecond exchange-time window. Matched quantity and matched raw-trade cursor IDs are reported per side; only the unmatched remainder becomes a cancellation proxy after the matching window closes. Pending quantity remains separate, and sequence discontinuities reset the matcher.
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
- `AITOS_TUNING_SCANNER_MIN_CONTRACT_AGE_DAYS`: default 30 days for non-core listings; core contracts are exempt from the age floor but still pass live 24h liquidity checks.
- `AITOS_TUNING_SCANNER_DYNAMIC_UNIVERSE_LIMIT`: default 35 additional contracts.
- `AITOS_TUNING_SCANNER_UNIVERSE_LIMIT`: existing overall scanner subscription cap.
- `AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY`: hard execution-quality floor, default `5.0/10`.
- `AITOS_TUNING_SCANNER_MAX_DIRECTIONAL_CANCEL_PRESSURE`: default `0.60`; LONG checks fresh bid-side cancellation pressure, SHORT checks fresh ask-side pressure. Stale/unavailable L2 evidence is not interpreted as spoofing and does not independently veto an otherwise liquid setup.

The existing 24h universe gate also requires quote volume (default 10,000,000 USDT) and trade count (default 10,000) and enforces contract metadata/age for dynamic listings. The scanner-level liquidity floor is a separate near-touch spread/depth quality test; it does not replace 24h activity screening.

These defaults should be reviewed against the observed AITOS paper-trading universe and order sizes before production use.

## Important limitations / next validation

- Full-depth OFI now consumes each sequenced Binance diff-depth event and every changed price level. The score is depth-normalized and distance-weighted, then aggregated over the latest 200 live events. REST-only fallback has no event history and therefore marks this component unavailable.
- Binance diff-depth is L2 data: it does not identify the reason for a level reduction. Trade matching is an attribution estimate, not exchange-confirmed causality: trades may be missed, event timestamps are not a shared sequence, and a reduction can combine executions and cancellations. Matching uses same symbol/price/aggressor side within 500 milliseconds; unmatched quantity is finalized as a cancellation proxy, never a confirmed cancel. Same-price additions within five seconds are replenishment observations, not proof of participant intent. Spoofing classification is not claimed.
- Breakout acceptance is a ranking feature, not a mandatory retest gate. This avoids suppressing all early trend entries before sufficient replay evidence exists.
- Absorption veto uses recent executed trades and endpoint price progress; it cannot identify iceberg orders or prove institutional intent.
- Universe volume floors are configurable defaults, not universal constants. Validate thresholds against actual AITOS order size, spread, slippage and funding costs.
- Existing MAE/MFE telemetry should be joined to entry-time component snapshots to compare immediate adverse markout, net expectancy, profit factor, MAE/MFE and missed-trade rate.
- Run unit tests, then historical replay / paper trading. Do not enable live deployment based on unit tests alone.


## Dynamic Candidate Rotation & Re-evaluation

- Lightweight market screening feeds a larger candidate pool, independent of the legacy one-shot expensive-analysis limit.
- Full analysis runs in bounded batches. Each batch passes through AI Kernel and trade-lifecycle gates before the next batch is analysed, allowing early stop after filling the target accepted-opportunity count.
- Kernel and lifecycle-quality rejections receive temporary, reason-specific cooldowns. Original opportunity scores and universe membership are never modified.
- Lightweight fingerprints combine latest close, short return and median quote activity. Material fingerprint changes allow early re-evaluation; `clear_candidate_cooldown(symbol)` supports explicit event-driven rechecks.
- Portfolio/capital constraints do not create symbol-level market cooldowns. Circuit-breaker, drawdown and daily-loss rejections halt refill for the current cycle.
- Cooldowns are bounded and process-local. Restarting clears this compute optimization state; it never persists as a trading restriction.
- Scanner health and `market.candidate_rotation` events expose batch progress and cooldown counters.

### Configuration

- `candidate_pool_limit`: lightweight-ranked symbols eligible for deep analysis.
- `candidate_batch_size`: symbols analysed before downstream gate evaluation/refill.
- `max_candidate_analyses_per_cycle`: hard per-cycle deep-analysis budget.
- `rejection_cooldown_seconds` / `kernel_rejection_cooldown_seconds`: temporary unchanged-state suppression.
- `market_change_recheck_pct`: lightweight fingerprint change threshold that releases cooldown early.

Low-resource, balanced and high-performance profiles use progressively larger pools and budgets. These are compute defaults, not trading-risk settings. Validate latency, missed-opportunity rate, false-rejection rate and paper-trading expectancy before live use.


## Quantitative Opportunity Management (V2)

- Every downstream candidate rejection is persisted in a bounded SQLite journal under the configured model-data directory. The journal records symbol, side, entry reference, raw composite score, rejection reason, regime and component evidence.
- As live trade ticks arrive, the journal records directional markouts at 1m, 5m and 15m. Each horizon includes signed return, adverse excursion (MAE) and favorable excursion (MFE). The state and completed markouts survive process restarts; this is measurement only and does not train or alter a strategy automatically.
- Markouts are based on the first observed tick at or after each horizon, not an interpolated candle close. If no tick arrives, that horizon remains pending rather than fabricating a value.
- Ranking hysteresis provides a configurable, bounded ordering advantage to incumbents within the current top-N. It never changes raw component scores, composite score, confidence, eligibility threshold or risk checks. A sufficiently stronger challenger displaces an incumbent.
- Hysteresis is an ordering-stability control, not a predictive edge. Compare churn, missed-opportunity rate, rejection precision, markout distribution and paper expectancy with hysteresis enabled/disabled before live use.
- SQLite uses WAL and a bounded retention count. It is a local operational journal; export/centralize it before multi-process or multi-host deployments. Each process must not independently write the same SQLite file over a network filesystem.



## Higher-Timeframe Trend Confirmation (HTF)

- Directional LTF entries from CVD / order flow now require a completed-bar HTF trend confirmation by default. The default mapping is 1m→5m, 3m/5m→15m, 15m→1h, 30m→2h, 1h→4h, 2h/4h/6h/8h/12h→1d and 1d→1w.
- HTF trend strength is measured with ADX (default minimum 25); direction is derived from +DI/-DI with a minimum separation of 5 points. Conflicting HTF market structure vetoes the setup. A ranging, weak, ambiguous, unavailable or insufficient HTF dataset fails closed.
- An HTF-aligned setup receives a 10/10 confirmation component in the opportunity and RL context. The filter does not modify the established LTF CHoCH CVD thresholds.
- HTF bars marked incomplete by the exchange adapter are removed before analysis. If HTF retrieval fails, the scanner rejects the entry rather than silently falling back to LTF-only confirmation.
- Settings are centralized in ScannerTuning: `htf_filter_enabled`, `htf_timeframe` (`auto` or explicit exchange timeframe), `htf_lookback`, `htf_min_adx`, and `htf_min_di_gap`. Environment overrides use `AITOS_TUNING_SCANNER_HTF_*`.
- This is a conservative entry-quality gate, not proof of profitability. Validate trade count, countertrend rejection rate, immediate adverse markout, missed-trend entries, expectancy and profit factor through historical replay and paper trading before any live activation.
