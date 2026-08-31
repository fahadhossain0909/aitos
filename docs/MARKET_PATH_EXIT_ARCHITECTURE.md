# Market Path + Exit Intelligence Architecture

> **Status:** Phases A–F complete (deterministic, explainable)  
> **Branch:** `feat/market-path-exit-architecture`  
> **Principle:** Existing production code is never removed unless strictly necessary. Static TP/SL remain as emergency hard stops.

## 1. Motivation

Entry was a rich multi-signal decision problem; Exit was treated mainly as risk-control (fixed R-multiples). This architecture restores symmetry:

```
Entry thesis → Market Path (where can price go?) → Exit Intelligence (is the thesis still alive?)
```

## 2. Modules

| Module | Output | Status |
|--------|--------|--------|
| Market State Engine (MSE) | `MarketState` | ✅ A |
| Market Path Planner (MPP) | `PathPlan` | ✅ B |
| Structural Risk Engine (SRE) | `StructuralStop` | ✅ C |
| Exit Intelligence Engine (EIE) | `ExitDecision` | ✅ D |
| Position Manager (PM) | `PositionAction` → TradeLifecycle | ✅ E |
| Offline Exit Replay | static vs eie comparison | ✅ F |
| Execution Guard (existing) | hard / exchange-side SL | unchanged |

## 3. Live decision order (`update_price`)

1. **Hard SL** — always first, never skipped  
2. **PositionManager** (if injected) → EXIT / MANAGE / HOLD  
3. Static TP / breakeven / trailing (preserved; HOLD may defer full TP exit)

## 4. Package layout

```
aitos/intelligence/market_state/     # A
aitos/intelligence/path_planner/     # B
aitos/intelligence/structural_risk/  # C
aitos/intelligence/exit_intelligence/# D
aitos/trading/position_manager.py    # E
aitos/trading/lifecycle.py           # additive wiring
aitos/evaluation/exit_replay.py      # F
aitos/evaluation/cli.py              # F CLI
```

## 5. Offline evaluation (Phase F)

```bash
# Synthetic demo
python -m aitos.evaluation.cli --demo

# CSV bars (timestamp,open,high,low,close[,volume])
python -m aitos.evaluation.cli --bars data.csv --side LONG --entry 79000 --sl 78500 --tp 80000 --json
```

`compare_policies(scenario, bars)` returns side-by-side PnL, R-multiple, hold bars, exit reasons and `eie_better`.

## 6. Design rules (enforced in code)

- TP is a destination, not an automatic exit command when EIE=HOLD  
- SL = structural invalidation first; hard SL is the safety net  
- Momentum slowdown alone never forces EXIT  
- All scoring deterministic + auditable reasons  
- No existing production exit path removed

## 7. Safety

- Human approval for production live orders still required  
- Paper + offline replay before any live promotion of EIE policy  
- Does not claim higher win-rate; aims for higher expected value per trade

---

*Architecture phases A–F delivered on `feat/market-path-exit-architecture`. Next steps: paper-trading soak, metric dashboards, optional calibrated probability models on top of the same contracts.*
