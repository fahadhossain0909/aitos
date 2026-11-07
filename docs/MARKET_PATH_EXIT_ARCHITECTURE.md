# Market Path + Exit Intelligence + Trade Journey Architecture

> **Status:** Phases A–G complete; Trade Journey management integrated into PR #116.  
> **Principle:** Existing production code is preserved where possible. With EIE enabled, the scanner SL is a soft thesis/risk boundary; a separately sized emergency stop remains the non-overridable account-protection boundary. Without EIE, legacy SL behavior is unchanged.

## Modules

| Module | Output | Status |
|--------|--------|--------|
| Market State Engine (MSE) | `MarketState` | ✅ A |
| Market Path Planner (MPP) | `PathPlan` | ✅ B (heuristic; calibration later) |
| Structural Risk Engine (SRE) | `StructuralStop` | ✅ C + G hierarchy |
| Exit Intelligence Engine (EIE) | `ExitDecision` | ✅ D + G hysteresis/temporal |
| Trade Thesis | `TradeThesis` / `ThesisEvaluation` | ✅ G |
| Trade Journey Engine | `TradeJourneySnapshot` | ✅ PR #116 integration |
| Position Manager (PM) | `PositionAction` | ✅ E + G + Journey |
| Market Context Provider | live OF/liquidity into lifecycle | ✅ G |
| Offline Exit Replay | static vs eie | ✅ F (simplified inputs) |

## Trade lifecycle

```text
Scanner → Kernel → Entry → Trade Journey → Exit → Journal/Replay
```

Once a position is open, the Trade Journey Engine continuously evaluates:

- original thesis health;
- expected-vs-actual market path adherence;
- momentum, liquidity and structure;
- time efficiency / stale-trade risk;
- unrealized R and excursion telemetry;
- uncertainty and deterioration.

The resulting state is one of `PROVING`, `CONFIRMED`, `EXTENDING`, `PROTECTING`, `DECAYING`, `UNCERTAIN`, or `EXITING`. The management action is one of `HOLD`, `PROTECT`, `TRAIL`, `REDUCE`, `HEDGE`, or `EXIT`.

The Journey layer does **not** replace EIE. EIE remains the authoritative exit/thesis arbiter; Journey may strengthen management or escalate a clearly decayed journey, but it never suppresses an EIE `EXIT`.

## Live decision order

1. **Emergency risk boundary** — independent, non-overridable catastrophic stop. When EIE is enabled, its default distance is 2× the initial soft-stop R distance and the position is sized against this wider boundary, so the configured account-risk amount is not silently doubled. The value is configurable through `positions.emergency_stop_r_multiple` / `AITOS_TUNING_POSITIONS_EMERGENCY_STOP_R_MULTIPLE`.
2. **Urgent EIE evaluation on soft-stop/TP contact** — a touch or breach of the scanner's initial `sl_price` forces an immediate thesis/path evaluation. HOLD or MANAGE may retain the position beyond this soft boundary; EXIT closes it. If EIE fails while the soft stop is breached, lifecycle fails closed and exits.
3. **Regular per-position EIE evaluation** — each open position has its own monitoring cadence. A busy symbol/position cannot consume a shared global allowance and starve other positions.
4. **Market/Trade Journey + PositionManager** — evaluate the original entry thesis, path, health, risk and management state on every scheduled full evaluation. The tiered monitor remains useful for priority/telemetry but never suppresses the authoritative evaluation.
5. **Dead-candle time decay** — after 16 unique completed 15m bars by default, request `time_decay_scratch` only when recorded MFE is below `0.10R` and both the complete recorded excursion and current mark remain within `±0.2%` of entry. This is a capital-efficiency scratch, not a thesis invalidation or a blanket holding-period close.
6. **Protection** — approved structural stop tightening, break-even and trailing protection can tighten the emergency exchange stop; HOLD does not move it to the soft-stop boundary.
7. **Hedge intent** — cost-aware hedge analysis remains an advisory decision until a venue/account-mode-aware hedge execution contract is implemented. A generic opposite-side market order is deliberately not treated as safe hedge execution.

Without EIE, the original static SL and exchange-side TP behavior remain authoritative. With EIE, the soft stop is a decision checkpoint and the emergency stop is the final safety boundary. Resting exchange TP orders are not placed for EIE-managed positions because they could bypass HOLD. Every configured TP level is an advisory checkpoint: touching it forces an EIE evaluation, then the level is consumed so the next target can trigger a later evaluation. No TP level automatically closes or partially reduces an EIE-managed position. Only an explicit EIE EXIT closes it, and only an explicit EIE MANAGE may reduce exposure. Without EIE, legacy static TP behavior remains unchanged.

## Dynamic spike-capture TP

`PositionManager` exposes `spike_tp_price` using a configurable ATR multiple (`spike_tp_atr_multiple`, default 10×). This is intentionally a distant, volatility-scaled capture band rather than the normal exit target. It is designed to retain exposure to rare sharp expansions while EIE controls normal exits.

## Trade health model

The deterministic baseline uses the following weighting:

- Thesis health: 30%
- Path adherence: 20%
- Momentum: 15%
- Liquidity: 15%
- Structure: 10%
- Time efficiency: 10%

The scores are deliberately explainable and bounded. They are not presented as calibrated probabilities or statistical expected value. Calibration remains a future statistical-model task.

## State/action principles

- **PROVING:** early trade; avoid premature management.
- **CONFIRMED:** thesis and market path remain supportive; protect gains as appropriate.
- **EXTENDING:** strong path progress; trail/protect rather than force a fixed TP.
- **PROTECTING:** preserve favorable risk while thesis remains usable.
- **DECAYING:** reduce exposure when health deteriorates without requiring full invalidation.
- **UNCERTAIN:** consider a cost-aware hedge when the thesis is not yet invalid.
- **EXITING:** thesis/structure failure or explicit exit intelligence requires closure.

Momentum decay alone is never treated as thesis invalidation.

## Hedge lifecycle

```text
UNCERTAIN
   ├─ thesis recovers → resume/close hedge
   ├─ uncertainty persists → manage hedge within risk/cost limits
   └─ thesis invalidates → EXIT
```

The existing hedge engine continues to enforce benefit/cost and ratio limits.

## Dead-candle / capital-efficiency guard

The default 15m position policy counts unique completed candle identities, persisted on the Trade record. A flatline scratch can trigger only after 16 completed bars (4 hours) with MFE below `0.10R` and the full recorded MAE/MFE excursion plus current mark inside `±0.2%` of entry. The paper runtime has a separate six-hour maximum-age backstop for the same verified flatline condition; it refuses to close when the current mark or excursion history is unavailable, and requires the persisted mark-receipt timestamp to be no older than five minutes. The mark timestamp is restored with the Trade record; after a restart, an old or absent mark cannot be used for an automatic close. A healthy or previously moving position is not closed merely because it is old.

The scanner has a hard `5.0/10` execution-liquidity floor. With fresh sequenced L2 depth evidence, LONG setups additionally require bid-side cancellation pressure `≤0.60`, while SHORT setups require ask-side cancellation pressure `≤0.60`. An unavailable or stale depth window is recorded as unavailable rather than being mislabelled as spoofing; it does not make a zero-confidence cancellation score look like a pass.

## Entry thesis and recovery

The entry thesis is captured immediately after the position opens using the best market context available at that moment. It records entry-time components, confirmations, invalidation conditions, expected path and structural invalidation level. The serialized thesis is stored on the trade payload and restored from ClickHouse, so a process restart does not rebuild the thesis from a later market state. Legacy trades without a serialized thesis use a clearly degraded first-evaluation fallback.

## Telemetry and replay

Every journey evaluation is serialisable through `PositionAction.to_dict()` and includes state, health, path adherence, time efficiency, reasons, hedge decision and protection outputs. Existing MAE/MFE telemetry is updated on each evaluation. Soft-stop contact, EIE action, emergency boundary and fail-closed behavior should be measured separately.

The next replay/learning layer should consume these journey snapshots to measure:

- MFE captured percentage;
- premature/late exit rate;
- time-to-thesis-failure;
- path-adherence quality;
- hedge benefit versus cost;
- protection effectiveness;
- post-exit excursion.

## Remaining research/engineering work

1. Statistical probability calibration and empirical expected-value estimation.
2. Full path graph/destination state machine with historical calibration.
3. Full L2/footprint-synchronised replay.
4. Implement and validate venue/account-mode-aware hedge order lifecycle (open, resize, unwind, restart reconciliation, idempotency and governance); until then hedge output is advisory only.
5. Calibrate the emergency-stop multiple and exit cadence against paper/replay results before enabling any live deployment.
6. ML/RL learning loop over the recorded journey telemetry.
