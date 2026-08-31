# Market Path + Exit Intelligence Architecture

> **Status:** Phases A–E implemented (deterministic, explainable)  
> **Branch:** `feat/market-path-exit-architecture`  
> **Principle:** Existing production code is never removed unless strictly necessary. Static TP/SL remain as emergency hard stops.

## 1. Motivation — Architectural Imbalance

Historically AITOS treated:

- **Entry** as a rich multi-signal *decision* problem (scanner, kernel, order-flow, liquidity, RL confidence …)
- **Exit** primarily as a *risk-control* problem (fixed R-multiples, trailing ATR, hard SL/TP)

This creates an asymmetry. Once a position is open the system largely stops reasoning about *why* the trade was taken and whether the original thesis is still valid.

The new architecture restores symmetry:

```
Entry thesis  →  Market Path (where can price go?)  →  Exit Intelligence (is the thesis still alive?)
```

## 2. Core Modules

| Module | Responsibility | Primary Output | Status |
|--------|----------------|----------------|--------|
| **Market State Engine (MSE)** | Canonical, explainable snapshot of current market condition | `MarketState` | ✅ Phase A |
| **Market Path Planner (MPP)** | Ranked probable price destinations + probabilities | `PathPlan` | ✅ Phase B |
| **Structural Risk Engine (SRE)** | Price level that objectively invalidates the trade thesis → structural SL | `StructuralStop` | ✅ Phase C |
| **Exit Intelligence Engine (EIE)** | HOLD / MANAGE / EXIT decision with scored reasons | `ExitDecision` | ✅ Phase D |
| **Position Manager (PM)** | Executes HOLD / REDUCE / TRAIL / EXIT via TradeLifecycle | `PositionAction` | ✅ Phase E |
| **Execution Guard** | Emergency hard stop + exchange-side protection (unchanged) | hard SL | existing |

### Decision flow (live)

```
update_price(trade, price, optional intel context)
        │
        ▼
   Hard SL hit?  ──yes──► EXIT (authoritative, never skipped)
        │ no
        ▼
   PositionManager present?
        │ yes
        ▼
   MSE → MPP → SRE → EIE
        │
        ├── EXIT   → close with explainable reason
        ├── MANAGE → partial reduce + optional structural-stop tighten
        └── HOLD   → continue; static full-TP may be deferred
        │
        ▼
   Static TP / breakeven / trailing (preserved paths)
```

## 3. Key Design Rules

1. **TP is a destination, never an automatic exit command** when Exit Intelligence is enabled and action is HOLD.
2. **SL is structural invalidation first**; hard SL remains the safety net.
3. **Three-level exit decision**: HOLD / MANAGE / EXIT.
4. **Momentum slowdown alone never forces EXIT.**
5. **First versions are deterministic + fully explainable.**
6. **Existing static / exchange-side SL & emergency stops stay.**

## 4. Package layout

```
aitos/intelligence/
├── market_state/          # Phase A
├── path_planner/          # Phase B
├── structural_risk/       # Phase C
└── exit_intelligence/     # Phase D

aitos/trading/
├── position_manager.py    # Phase E orchestrator
└── lifecycle.py           # additive wiring (optional injection)
```

## 5. Integration contract (Phase E)

- `TradeLifecycle(..., position_manager=PositionManager())` enables the stack.
- Without injection, behaviour is **identical** to pre-Phase-E code.
- Hard SL is always evaluated first.
- `decision.exit` events are published for XAI / journal / learning.
- `update_price` accepts optional intelligence context (`order_flow`, `volume_profile`, swings, ATR, …).

## 6. Implementation Roadmap

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **A** | Market State Engine | ✅ |
| **B** | Market Path Planner | ✅ |
| **C** | Structural Risk Engine | ✅ |
| **D** | Exit Intelligence Engine | ✅ |
| **E** | Position Manager + TradeLifecycle wiring | ✅ |
| **F** | Offline evaluation harness (replay) | Next |

## 7. Safety & Non-Goals

- Does **not** remove human-approval requirements for production live orders.
- Does **not** claim higher win-rate; aims for higher expected value per trade.
- Paper-trading and offline replay (Phase F) remain mandatory before live promotion.

---

*Next: Phase F — offline evaluation harness to replay historical trades under the new policy vs static 1R/2R.*
