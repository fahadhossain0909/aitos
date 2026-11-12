# AMD Research Validation and Execution Replay

## Status and decision boundary

The AMD classifier remains shadow-only. This document defines the reusable research and execution-measurement layer; it does not assert a profitable edge or authorize strategy activation. Unit tests validate contracts on synthetic data, not market performance.

## Event-time order-flow evidence

`build_event_aligned_order_flow` accepts:
- Trade prints with exchange event timestamps, unique trade IDs, price, quantity and an explicit aggressor side.
- Timestamped post-update L1 book states with best bid/ask prices and displayed quantities.

It deduplicates trade IDs, rejects future/out-of-window events, excludes unknown aggressor sides, computes signed-notional CVD and best-quote OFI, and fails freshness closed when event counts or staleness limits are not met. The output's bid/ask refill fields are displayed-size increases at unchanged best prices only. They do not prove hidden liquidity, cancellation/replenishment intent, queue position or absorption.

The live bridge uses `build_live_event_order_flow` with AITOS `TradeTick` objects and the existing `LocalOrderBook` depth-event history. It aggregates exchange-time signed trade notional and per-update full-depth `ofi_raw` / `ofi_scale` measurements. It validates the `previous_update_id` → `final_update_id` chain and carries an explicit history-gap timestamp from the live cache. A sequence break or in-window cache reset prevents fresh flow from being marked confirmatory until the affected window ages out. This is materially stronger than a static book-imbalance proxy, while remaining bounded in-memory event history rather than a durable archive.

The existing Binance parser already maps `isBuyerMaker` to `TradeTick.side`, and the live scanner cache already retains deduplicated trades plus a bounded history of LocalOrderBook depth-event records. The AMD bridge now consumes those legacy live contracts directly through `build_live_event_order_flow`; it does not rely on the canonical `TradeEvent` envelope for aggressor-side semantics. The cache marks depth-chain breaks and AMD fails freshness closed while a break remains inside the analysis window. The history is bounded to the cache's recent in-memory window and is not a substitute for a durable, replayable event archive. The live integration therefore provides event-aligned CVD and sequence-checked full-depth OFI, but historical validation still requires archived event-time inputs.

## Point-in-time outcome labels

`evaluate_amd_forward_outcome` computes directional close return, MFE and MAE for a fixed forward candle horizon. It marks incomplete horizons explicitly and does not guess intrabar ordering when both stop and target could have traded within the same candle. These are counterfactual candle markouts, not exchange fills or net trade returns.

`replay_amd_historical_observations` consumes the existing ClickHouse replay dataset contract, requires raw sequenced depth-event replay plus a pre-window book bootstrap, rebuilds each AMD state at a completed-candle boundary, and attaches only later candles as counterfactual labels. It rejects snapshot-only datasets and never uses candles that close after the decision timestamp. This is the historical data path to use when a configured ClickHouse repository is available.

## Execution-aware replay

`replay_amd_execution` composes the existing timestamped L1 execution replay for entry and exit:
- Submission-to-market latency.
- Spread and adverse slippage.
- Taker fees on executed quantity.
- Partial fills constrained by displayed top-of-book size.
- Funding over the stated holding interval.

Entry and exit fills are matched FIFO so partial exit PnL is calculated only on matched quantities. Entry costs are allocated pro-rata to matched inventory; unmatched inventory makes the result incomplete. The existing L1 replay is not a full L2 market-impact, passive queue, stop-gap or liquidation model. For those scenarios use the existing `depth_replay.py` L2 tools and venue-calibrated impact assumptions. Do not treat zero impact coefficients as evidence that impact is zero.

## Walk-forward and cohort reporting

`purged_walk_forward_folds` creates expanding chronological train/test folds with an explicit embargo. The caller must choose embargo length at least as large as the maximum label horizon and ensure the source rows are already deduplicated, sorted and point-in-time. The helper rejects non-chronological event timestamps.

`summarize_amd_cohorts` reports observations, resolved/unresolved outcomes, mean net return, win rate and profit factor by experiment variant, symbol, regime, AMD phase and LONG/SHORT direction. It never imputes missing outcomes. Supply `net_return_pct` only after fees, funding, spread, slippage and execution outcomes have been reconciled.

`replay_amd_historical_observations` returns phase/state observations plus strictly later counterfactual candle labels; `summarize_amd_forward_cohorts` reports those gross forward markouts separately and never labels them net PnL. `ClickHouseHistoricalAblationRunner.run_amd_phase_replay` is the async ClickHouse entry point for loading and running this point-in-time phase replay; its result includes both per-bar observations and the gross forward-cohort report.

## Required ablation matrix

Run identical chronological splits and execution assumptions for:
1. `baseline`: existing AITOS behavior with AMD excluded from decision inputs.
2. `liquidity_only`: existing Liquidity Intelligence shadow evidence.
3. `amd_accumulation`: accumulation phase features only.
4. `amd_manipulation`: manipulation/sweep phase features only.
5. `amd_distribution`: distribution/delivery phase features only.
6. `amd_full`: all AMD phases.
7. `amd_full_event_flow`: AMD with validated event-time CVD and L1/L2 OFI.

Report all cohorts separately for LONG and SHORT, symbol, timeframe, volatility/liquidity regime and venue. Include sample count, unresolved fraction, phase precision/recall against independently defined labels, false-sweep rate, expectancy, profit factor, maximum drawdown, Sharpe/Sortino, MAE/MFE, time-to-delivery, stop-out attribution, turnover and cost sensitivity. Include confidence intervals and multiple-testing correction; do not tune thresholds on the final holdout.

## Acceptance gates before any decision influence

- A complete point-in-time historical dataset with exchange event time, ingestion time, unique trade IDs, aggressor side, sequenced depth deltas/snapshots, candles, funding and instrument filters.
- No look-ahead, survivorship or symbol-selection leakage; chronological purged walk-forward and a final untouched holdout.
- Calibrated phase labels and separate LONG/SHORT results across symbols and regimes.
- Execution-aware L1/L2 replay with conservative fees, funding, spread, slippage, latency, partial fills, stop gaps and exchange constraints.
- Baseline and ablation results with adequate sample sizes, uncertainty intervals and no unexplained material regression.
- Independent review and explicit champion/challenger approval.
- Shadow and paper canary evidence before any live decision influence.

No historical exchange dataset or production venue-calibrated cost model is fabricated by this code change. If those data are not present in the research environment, the corresponding validation gate remains open.