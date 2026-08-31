# Market Path + Exit Intelligence Architecture

> **Status:** Phases A–F complete; Phase G (v2) in progress on `feat/exit-intelligence-v2`  
> **Principle:** Existing production code is never removed unless strictly necessary. Static TP/SL remain as emergency hard stops.

## Modules

| Module | Output | Status |
|--------|--------|--------|
| Market State Engine (MSE) | `MarketState` | ✅ A |
| Market Path Planner (MPP) | `PathPlan` | ✅ B (heuristic; calibration later) |
| Structural Risk Engine (SRE) | `StructuralStop` | ✅ C + G hierarchy |
| Exit Intelligence Engine (EIE) | `ExitDecision` | ✅ D + G hysteresis/temporal |
| Trade Thesis | `TradeThesis` / `ThesisEvaluation` | ✅ G |
| Position Manager (PM) | `PositionAction` | ✅ E + G |
| Market Context Provider | live OF/liquidity into lifecycle | ✅ G |
| Offline Exit Replay | static vs eie | ✅ F (simplified inputs) |

## Live decision order

1. **Hard SL** — always first  
2. **PositionManager** → EXIT / MANAGE / HOLD  
3. Static TP / breakeven / trailing (HOLD may defer full TP)

`handle_event` injects live market context when a `MarketContextProvider` is attached.

## Phase G

### Trade Thesis
Machine-readable entry rationale. EIE asks: *is the original thesis still consistent?*

### Structural hierarchy
```
structure_break > protected_swing > major_swing > value_area > liquidity > micro_swing > fallback
```

### Exit hysteresis
- Structure break / thesis INVALIDATED → EXIT immediately  
- Soft high exit-score needs N consecutive observations (`exit_confirm_ticks`, default 2)

### Temporal memory
Per-symbol momentum ring buffer; mild `momentum_decaying` reason.

### ERE (honest)
Heuristic scores soft-normalised; not calibrated statistical EV.

## Still open

1. Path graph + destination state machine  
2. Probability calibration  
3. Richer MANAGE actions (WAIT / PROTECT / TRAIL)  
4. Full L2/footprint synchronized replay  
5. Production fail-closed if PM required but missing  
