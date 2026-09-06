# AITOS Contextual Market Intelligence v1

## Design principle

AITOS does not grow by accumulating indicators. It grows by adding independent information sources and relationships between them.

**Feature != Evidence != Scenario != Decision.**

A feature is a measurable observation. Evidence is a feature assessed for direction, reliability and contextual relevance. A scenario is a competing hypothesis about what may happen next. The decision layer chooses Long, Short, or **No Trade** after evidence and contradictions are evaluated.

## Core information layers

| Layer | Role | Priority |
|---|---|---|
| Market Structure / Regime | State and structural direction | Core |
| Liquidity | Resting liquidity, sweeps, absorption | Core |
| Order Flow | Aggression and execution pressure | Core |
| CVD / Delta / Footprint | Directional flow and microstructure | Core |
| VWAP | Current price location / execution context | Core |
| Cross-market intelligence | Lead/lag, propagation, divergence | Core |
| Volume Profile | Acceptance/distribution by price | High |
| Volatility Regime | Compression/normal/expansion/extreme | High |
| Positioning | OI, funding, basis | High for derivatives |
| Forced Flow | Liquidation / forced-position pressure | High for crypto |
| Anchored VWAP | Event-relative price location | Medium-high |
| Price Imbalance | Quantified FVG-like displacement zone | Medium-high |
| Origin Zone | Evidence-backed displacement origin | Medium |

## Positioning adapter

`PositioningContext` is venue- and asset-class-neutral. Crypto derivatives can populate open interest, funding, basis and liquidation observations. Other markets can populate equivalent positioning observations later without changing strategy or decision code.

Funding is explicitly a crowding/context feature, not a standalone direction signal. Liquidation pressure is forced-flow evidence, not a buy/sell trigger.

## ICT/SMC translation

AITOS does not implement ICT/SMC labels as independent hard signals.

- Liquidity Sweep -> Liquidity Engine
- BOS / CHoCH -> Market Structure
- Displacement -> Flow + Volatility + Structure
- FVG -> Price Imbalance Zone
- Order Block -> Displacement Origin Zone
- Accumulation / Distribution -> Auction + Regime + Flow
- Premium / Discount -> Price Location

An origin zone earns quality only when displacement, flow, liquidity and structure evidence agree. No institutional-order narrative is assumed.

## Decision hierarchy

```text
Market State
  -> State Transition
  -> Liquidity + Flow + Positioning
  -> Price Location
  -> Cross-Market Confirmation
  -> Historical Analogue / Structural Analogy
  -> Scenario Engine
  -> Evidence + Reliability + Relevance
  -> Contradiction Engine
  -> Confidence / Regime Compatibility
  -> Invalidation
  -> Long / Short / NO TRADE
```

## No-trade policy

No Trade is a valid result, not a failure state. Strongly mixed evidence, extreme volatility, failed structural analogies, weak market acceptance, or insufficient confidence should suppress directional action.

LLMs and agents may contextualize, compare and explain structured evidence. They must not replace deterministic market-data calculations for price, volume, order-flow, probability or similarity metrics.

## Historical context

Historical analogue search must use only completed prior windows for matching and only forward observations after each matched window for outcome statistics. This prevents look-ahead leakage. The result is contextual evidence and must not be treated as a guaranteed forecast.
