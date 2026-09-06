# Market Path + Exit Intelligence + Trade Journey Architecture

> **Status:** Phases A–G complete; Trade Journey management integrated into PR #116.  
> **Principle:** Existing production code is never removed unless strictly necessary. Static TP/SL remain emergency hard stops.

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

1. **Hard SL** — always first and exchange-side where supported.
2. **Market/Trade Journey + PositionManager** — evaluate thesis, path, health, risk and management state.
3. **Exit Intelligence Engine** — retain the existing thesis/path exit logic and hysteresis.
4. **Protection** — structural stop tightening, trailing SL and optional dynamic spike-capture TP.
5. **Hedge** — only as a cost-aware uncertainty response; a hedge is not a substitute for thesis invalidation.

Static TP/SL remain emergency protection. The main exit remains decision-based.

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

## Telemetry and replay

Every journey evaluation is serialisable through `PositionAction.to_dict()` and includes state, health, path adherence, time efficiency, reasons, hedge decision and protection outputs. Existing MAE/MFE telemetry is updated on each evaluation.

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
4. Production fail-closed behavior if required PositionManager/market context is missing.
5. ML/RL learning loop over the recorded journey telemetry.
