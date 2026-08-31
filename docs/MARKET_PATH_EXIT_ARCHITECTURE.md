# Market Path + Exit Intelligence Architecture

> **Status:** Design + Phase-A scaffolding  
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

| Module | Responsibility | Primary Output |
|--------|----------------|----------------|
| **Market State Engine (MSE)** | Canonical, explainable snapshot of current market condition | `MarketState` |
| **Market Path Planner (MPP)** | Ranked probable price destinations + probabilities | `PathPlan` |
| **Structural Risk Engine (SRE)** | Price level that objectively invalidates the trade thesis → structural SL | `StructuralStop` |
| **Exit Intelligence Engine (EIE)** | HOLD / MANAGE / EXIT decision with scored reasons | `ExitDecision` |
| **Position Manager (PM)** | Executes HOLD / REDUCE / TRAIL / EXIT | actions to lifecycle |
| **Execution Guard** | Emergency hard stop + exchange-side protection (unchanged) | hard SL |

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

## 4. Market State (canonical representation)

```python
@dataclass(frozen=True)
class MarketState:
    symbol: str
    timestamp: datetime
    mid_price: float

    regime: str                 # TRENDING_UP | TRENDING_DOWN | RANGE | TRANSITION
    trend_strength: float       # 0–1
    volatility_regime: str      # CONTRACTING | NORMAL | EXPANDING
    auction_state: str          # ACCEPTANCE_ABOVE_VALUE | …
    order_flow_bias: str        # BUYER_DOMINANT | SELLER_DOMINANT | NEUTRAL
    liquidity_map: str          # UPSIDE_LIQUIDITY_HIGH | …
    momentum: str               # STRONG | MODERATING | WEAK
    structure: str              # BULLISH | BEARISH | RANGE
    reversal_risk: float        # 0–1

    # raw feature bag for downstream modules (explainable)
    features: dict[str, float]
```

## 5. Path Plan

```python
@dataclass(frozen=True)
class PathDestination:
    price: float
    probability: float          # 0–1
    distance: float
    market_structure_type: str  # prior_high | LVN | HVN | liquidity_pool | …
    liquidity_type: str
    expected_horizon: str       # scalp | intraday | swing
    confidence: float

@dataclass(frozen=True)
class PathPlan:
    symbol: str
    current_price: float
    upside: tuple[PathDestination, ...]
    downside: tuple[PathDestination, ...]
    as_of: datetime
```

## 6. Expected Remaining Edge (ERE)

Once a position is open, EIE can compute:

```
ERE = Σ (p_i * upside_i) - Σ (q_j * downside_j) - transaction_cost
```

- ERE ≫ 0 → HOLD
- ERE ≈ 0 → MANAGE
- ERE < 0  → EXIT

This replaces static 1R/2R rules with a continuously updated, probabilistic edge estimate.

## 7. Implementation Roadmap (strict order)

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| **A** | Market State Engine (canonical state + feature aggregation) | existing live_state, order_flow, liquidity, indicators, auction |
| **B** | Market Path Planner (destination ranking) | A + volume profile / structure helpers |
| **C** | Structural Risk Engine (thesis-invalidation SL) | A + structure / liquidity |
| **D** | Exit Intelligence Engine (HOLD/MANAGE/EXIT + reasons) | A+B+C |
| **E** | Position Manager integration into TradeLifecycle | D (additive, no removal of existing SL/TP paths) |
| **F** | Offline evaluation harness (replay old trades under new policy) | E |

**No Deep RL in the first versions.** Deterministic scoring first; RL only after we can audit the feature-based decisions.

## 8. Integration Contract with Existing Code

- `TradeLifecycle.update_price` continues to honour the emergency hard SL and exchange-side stops.
- New path: when an open trade exists, EIE is consulted *before* the static TP check.  
  If EIE says HOLD, a static TP hit becomes a *partial-reduce candidate* rather than forced full exit (configurable).
- All new modules publish events on the existing Redis Streams bus (`decision.market_state`, `decision.path_plan`, `decision.exit` …) so XAI / journal / learning can consume them without coupling.
- Position sizing already lives in `risk/position_sizing.py`; SRE will supply the structural distance that sizing uses.

## 9. Research Anchors

- Multi-level Order-Flow Imbalance (MLOFI) carries measurable short-horizon predictive information (Cont et al., Xu/Gould/Howison, Kolm et al.).
- Volume Profile HVN ≈ acceptance / stall zones; LVN ≈ faster travel zones (auction-market theory).
- Structural invalidation (BOS / CHoCH / protected swing) is the objective definition of “thesis broken” used by professional discretionary frameworks.

These are used as *feature sources*, never as black-box oracles.

## 10. Safety & Non-Goals

- This architecture does **not** remove the requirement for human approval on production live orders.
- It does **not** claim higher win-rate; it claims higher *expected value per trade* by letting winners run when the path remains open and cutting when the thesis is objectively dead.
- Paper-trading and offline replay (Phase F) are mandatory before any live promotion.

---

*Next concrete step: Phase A implementation under `aitos/intelligence/market_state/`.*
