# AITOS AMD Intelligence

## Purpose and safety boundary

This document describes the AITOS implementation of Accumulation-Manipulation-Distribution (AMD) as a market-context classifier. AMD is not an independent entry strategy and does not claim that a specific participant intentionally hunted stops. It classifies observable price, liquidity, volume and fresh order-flow evidence.

The implementation is advisory and shadow-only. It does not change candidate ranking, entry permission, order sizing, stop placement, exchange orders or live/paper execution policy. AMD scores are heuristic feature scores, not calibrated probabilities or claims of profitability.

## Existing foundation reused

The implementation builds on the merged structure-aware Liquidity Intelligence / Stop-Hunt Defense work:

- Completed-candle swing and equal-high/equal-low liquidity pools.
- ATR-normalized sweep events and re-sweep gating.
- Rejection, acceptance and indecision classification.
- Freshness-gated order-flow evidence.
- Existing structural risk and liquidity-aware path-planning inputs.
- Candidate-linked append-only historical decision evidence.

AMD does not reimplement these features. It consumes LiquidityAnalysis, SweepEvent, SweepClassification and OrderFlowEvidence from liquidity_intelligence.py.

## Modules

### Accumulation

The accumulation feature extractor evaluates a bounded window of completed candles:

- Range width relative to true-range ATR.
- Recent versus prior ATR contraction.
- Relative volume contraction.
- Mapped liquidity pools inside the range.
- Fresh bid/ask replenishment as an absorption-context feature.

The weighted score is normalized over observed components. evidence_coverage is emitted separately so a missing feed is not silently interpreted as negative or positive evidence. A confirmed accumulation additionally requires sufficient coverage and at least one mapped internal liquidity pool.

The score is a screening feature only. A narrow range can be consolidation, distribution, a pause in a trend, or low-liquidity noise; it is not proof of institutional buying.

### Manipulation

Manipulation evidence reuses the existing sweep classifier:

- A mapped liquidity level was penetrated.
- The post-sweep state is rejected, accepted, or still undecided.
- Fresh order-flow evidence is available and meets the configured confidence floor.

A rejected sweep with fresh flow can be labelled MANIPULATION_CONFIRMED; without fresh flow it remains a candidate. An accepted break is not automatically faded. The underlying liquidity classifier remains the authority for the sweep's reversal/continuation interpretation.

### Distribution

Directional delivery is evaluated only when the existing sweep classification supplies a LONG or SHORT direction. Confirmation requires all three independent evidence families:

1. Directional structure agrees with the proposed direction.
2. Fresh CVD and OFI proxies agree with that direction.
3. Relative volume expands against the preceding five-bar window.

When any evidence family is missing, the engine emits at most a candidate. The confirmation is not a probability and requires calibration on chronological replay data.

## State and deterministic replay

AMDPhase includes neutral, candidate and confirmed accumulation/manipulation/distribution states, trend continuation, reaccumulation and redistribution labels. Directional context is carried forward only through a supplied prior checkpoint; it is not inferred from an unrelated symbol or timeframe.

AMDState is immutable and has a deterministic state_id based on symbol, timeframe, completed-candle timestamp, phase and latest liquidity event. It carries range bounds, direction, event identity, target liquidity IDs, component scores, evidence and coverage.

analyze_amd is deterministic for identical point-in-time inputs. It accepts an optional previous_state checkpoint so a durable caller can distinguish reaccumulation/redistribution from a first accumulation. The scanner restores and atomically updates per-symbol/per-timeframe checkpoints in the same Redis client already used by historical intelligence when that client is configured. Checkpoints have a 30-day TTL, normalize timestamps to UTC and reject older candle timestamps, preventing delayed replay from overwriting newer state. Future, same-bar, cross-symbol and cross-timeframe checkpoints are not used as prior context. When Redis is unavailable, the classifier remains deterministic and reconstructs from closed-candle evidence; it reports checkpoint persistence as unavailable rather than claiming durability. The current state is also written into the candidate-linked append-only evidence record.

## Multi-timeframe aggregation

analyze_amd_timeframe_stack aggregates supplied AMDState objects using AITOS role weights:

| Timeframe | Weight |
|---|---:|
| 1D | 2.0 |
| 4H | 1.8 |
| 1H | 1.0 |
| 15M | 0.8 |
| 5M | 0.6 |
| 1M | 0.4 |

Missing timeframes are listed explicitly and reduce evidence coverage. The function never fabricates a neutral state for an unavailable frame and rejects mixed-symbol inputs.

The scanner now passes the six closed-candle snapshots already returned by MultiTimeframeAnalyzer and HigherTimeframeCandleCache into AMD. The bridge computes per-frame liquidity/AMD context and aggregates the resulting states without making additional exchange requests. Only the candidate timeframe receives the current freshness-gated live flow snapshot; higher frames do not borrow lower-timeframe flow and therefore cannot receive a flow-confirmed reversal label without their own event-aligned feed. If the existing HTF analysis is disabled or incomplete, missing frames remain visible and the AMD stack is marked incomplete.

## Scanner and evidence integration

The contextual scanner bridge:

- Runs AMD only when candidate historical candles are present.
- Reuses the same freshness-gated flow snapshot used by Liquidity Intelligence.
- Attaches amd_intelligence to the AI Kernel context as evidence-only data.
- Adds phase, direction, stable state ID and decision_impact=false to the candidate funnel telemetry.
- Stores AMD output beside Liquidity Intelligence in the candidate-linked historical evidence payload.
- Does not add AMD to ranking weights or entry gates.

The evidence schema is versioned in the JSON payload. Existing evidence consumers must tolerate records without an AMD section.

## Configuration and calibration

AMDConfig provides bounded research parameters for window length, minimum history, range/ATR threshold, contraction ratio, minimum liquidity-pool count, heuristic phase thresholds, relative-volume expansion and minimum fresh-flow confidence.

These defaults are starting points for replay experiments, not universal market constants. Do not tune them on the final out-of-sample period. Calibrate by symbol, timeframe and regime only with chronological splits and purged overlapping labels.

## Required validation before any decision influence

1. Unit tests: closed-candle-only processing, deterministic IDs, accumulation candidate, both sweep directions, stale/missing flow, directional expansion, MTF missing-frame reporting and mixed-symbol rejection.
2. Replay labels: point-in-time liquidity pools, sweep timestamp, reclaim/acceptance, structure shift, CVD/OFI freshness, volume expansion and forward path.
3. Execution-aware replay: fees, funding, spread, slippage, latency, partial fills and adverse selection.
4. Ablations: baseline; liquidity only; AMD accumulation only; manipulation only; distribution only; full AMD; full AMD plus CVD/OFI/HTF.
5. Metrics: phase precision/recall, false-sweep rate, calibration error, time-to-delivery, MAE/MFE, stop-out attribution, net expectancy, PF, drawdown, Sharpe/Sortino, trade count and missed opportunity rate.
6. Chronological walk-forward and symbol/regime OOS validation, with separate LONG and SHORT reporting.
7. Shadow observation first; any advisory or controlled-paper influence requires explicit governance approval, champion/challenger evidence and rollback.
8. No live activation or automatic model promotion.

## Known limitations and next gates

- Fresh live flow evidence is currently attached only to the candidate timeframe. Higher-timeframe states use completed-candle and structural-liquidity evidence; they do not borrow lower-timeframe CVD/OFI.
- The current live order-flow adapter uses freshness-gated bounded buy-ratio and top-of-book depth proxies. It is not a fully event-aligned trade CVD plus depth-event OFI reconstruction.
- Durable checkpoint restore depends on the existing historical-intelligence Redis client being configured and healthy. Without it, AMD falls back to deterministic candle-window reconstruction and marks checkpoint persistence unavailable.
- The append-only candidate evidence path is not yet a complete trade-close outcome join. Fees/funding/slippage-aware replay and long-horizon phase outcome attribution remain required.
- No threshold is production-calibrated and no profitability claim is made.

These limitations must remain visible in release notes and rollout gates.
