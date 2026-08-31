# Market Path + Exit Intelligence Architecture

> **Status:** Phases A–D implemented (deterministic, explainable)  
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
| **Position Manager (PM)** | Executes HOLD / REDUCE / TRAIL / EXIT | actions to lifecycle | ⏳ Phase E |
| **Execution Guard** | Emergency hard stop + exchange-side protection (unchanged) | hard SL | existing |

### Decision flow

```
MARKET DATA
     │
     ▼
Order-Flow / Structure / Liquidity / Auction / Vol / Funding / OI
     │
     ▼
MARKET STATE ENGINE  ─────────────────────►  ENTRY ENGINE (existing)
     │                                              │
     ├── MARKET PATH PLANNER                        │
     │         │                                    │
     │         ▼                                    ▼
     │    possible destinations                  ENTRY
     │         │                                    │
     └── STRUCTURAL RISK ENGINE  ────────────────────────────┘
               │
               ▼
        EXIT INTELLIGENCE ENGINE
               │
               ▼
        HOLD  /  MANAGE  /  EXIT
               │
               ▼
        POSITION MANAGER  →  TradeLifecycle (existing)
```

## 3. Key Design Rules

1. **TP is a destination, never an automatic exit command.**  
   The Path Planner surfaces targets; EIE decides whether to take them.

2. **SL is structural invalidation first, risk budget second.**  
   ```
   Risk budget  →  Structural SL distance  →  Position size
   ```
   (never the reverse)

3. **Three-level exit decision**
   - **HOLD** — thesis intact, expected remaining edge positive
   - **MANAGE** — warning signals, tighten stop / partial reduce / wait
   - **EXIT** — multiple independent evidences that thesis is broken

4. **“Momentum slowing” alone is never sufficient for EXIT.**  
   Healthy slowdown (structure intact, OF supportive, liquidity target alive) → HOLD.  
   Dangerous slowdown (OF reversal + absorption + structure break + target probability collapse) → EXIT.

5. **First versions are deterministic + fully explainable.**  
   Every score component is auditable. ML/RL layers may sit on top later once sufficient labelled experience exists.

6. **Existing static / exchange-side SL & hard emergency stops stay.**  
   They protect against software failure, websocket loss, flash crashes, etc.

## 4. Package layout (implemented)

```
aitos/intelligence/
├── market_state/          # Phase A
│   ├── models.py          # MarketState + enums
│   └── engine.py          # MarketStateEngine
├── path_planner/          # Phase B
│   ├── models.py          # PathDestination, PathPlan
│   └── planner.py         # MarketPathPlanner
├── structural_risk/       # Phase C
│   ├── models.py          # StructuralStop
│   └── engine.py          # StructuralRiskEngine
└── exit_intelligence/     # Phase D
    ├── models.py          # ExitAction, ExitReason, ExitDecision
    └── engine.py          # ExitIntelligenceEngine
```

## 5. Expected Remaining Edge (ERE)

Once a position is open, EIE computes:

```
ERE = Σ (p_i * upside_i) - Σ (q_j * downside_j) - transaction_cost
```

- ERE ≫ 0 → HOLD
- ERE ≈ 0 → MANAGE
- ERE < 0  → EXIT (when combined with high exit_score)

This replaces static 1R/2R rules with a continuously updated, probabilistic edge estimate.

## 6. Implementation Roadmap

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **A** | Market State Engine | ✅ Done + tests |
| **B** | Market Path Planner | ✅ Done + tests |
| **C** | Structural Risk Engine | ✅ Done + tests |
| **D** | Exit Intelligence Engine | ✅ Done + tests |
| **E** | Position Manager integration into TradeLifecycle (additive) | Next |
| **F** | Offline evaluation harness (replay old trades under new policy) | After E |

**No Deep RL in the first versions.** Deterministic scoring first; RL only after we can audit the feature-based decisions.

## 7. Integration Contract with Existing Code

- `TradeLifecycle.update_price` continues to honour the emergency hard SL and exchange-side stops.
- New path (Phase E): when an open trade exists, EIE is consulted *before* the static TP check.  
  If EIE says HOLD, a static TP hit becomes a *partial-reduce candidate* rather than forced full exit (configurable).
- All new modules are pure / side-effect free; callers publish events on the existing Redis Streams bus so XAI / journal / learning can consume them without coupling.
- Position sizing already lives in `risk/position_sizing.py`; SRE supplies the structural distance that sizing should use.

## 8. Research Anchors

- Multi-level Order-Flow Imbalance (MLOFI) carries measurable short-horizon predictive information (Cont et al., Xu/Gould/Howison, Kolm et al.).
- Volume Profile HVN ≈ acceptance / stall zones; LVN ≈ faster travel zones (auction-market theory).
- Structural invalidation (BOS / CHoCH / protected swing) is the objective definition of “thesis broken” used by professional discretionary frameworks.

These are used as *feature sources*, never as black-box oracles.

## 9. Safety & Non-Goals

- This architecture does **not** remove the requirement for human approval on production live orders.
- It does **not** claim higher win-rate; it claims higher *expected value per trade* by letting winners run when the path remains open and cutting when the thesis is objectively dead.
- Paper-trading and offline replay (Phase F) are mandatory before any live promotion.

---

*Next concrete step: Phase E — additive wiring of ExitDecision into TradeLifecycle without removing existing SL/TP paths.*
