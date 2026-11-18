# AITOS Decision Language (ADL)

## Status and scope

ADL is a provider-neutral, typed probabilistic decision specification and advisory
runtime. It is inspired by the public description of probabilistic choice, score
and boolean decision models. It does not reproduce Jev internals, weights,
proprietary training or undocumented API semantics.

The first implementation is deliberately shadow-only. It does not subscribe to
market feeds, change Scanner, Strategy, Risk or Execution behavior, size
positions, or place exchange orders. It is independently callable so integration
can be introduced behind an explicit feature flag after replay and paper validation.

## Architecture

1. **MarketSnapshot** — timestamped feature snapshot with source-version metadata.
   Producers must supply point-in-time values.
2. **DecisionSpec / ADL DSL** — typed Choice, Score and Noul questions. The parser
   accepts declarations only; it does not evaluate Python, expressions, imports,
   network calls or arbitrary policy code.
3. **DecisionProvider** — provider boundary. **JevDecisionProvider** accepts an
   operator-supplied SDK/API callable. The real Jev API contract and account access
   must be verified before implementing a network transport.
4. **AgentAssessment / Aggregator** — validates distributions and discounts
   duplicated correlation groups. This is a conservative heuristic, not a
   substitute for estimating a dependence matrix.
5. **ProbabilityCalibrator** — one-vs-rest reliability buckets with minimum sample
   requirements, prior shrinkage, Brier score, log loss and expected calibration
   error.
6. **ADLRuntime** — validates freshness, required state, response schema,
   calibration maturity, disagreement, expected value and hard vetoes.
7. **Attribution contract** — idempotent decision/outcome interface. The included
   in-memory implementation is for tests and local shadow experiments only;
   durable ClickHouse/Redis persistence is a later integration adapter.

## Decision semantics

- LONG, SHORT and NO_TRADE are mutually exclusive proposed outcomes.
- ABSTAIN means the engine cannot responsibly decide due to stale/incomplete
  state, invalid provider output, cold calibration or missing economics.
- Probability distributions must contain all three trading outcomes and sum to
  one. The top directional probability is not a win probability.
- Expected value uses a separate explicit TradeEconomics.win_probability.
  Direction probability is never silently substituted for win probability.
- Hard risk vetoes are checked before model invocation and cannot be averaged
  away by model confidence.
- The runtime always sets execution_eligible=False. Existing Risk Engine and
  execution lifecycle remain authoritative.
- Shadow mode is the default. Shadow mode is not a production canary.

## DSL example

    DECISION pretrade:
    STATE:
      - trend
      - order_flow
      - liquidity
      - volatility
      - market_regime
      - risk_veto
    ASK direction: CHOICE [LONG, SHORT, NO_TRADE]
    ASK entry_quality: SCORE 1..5
    ASK genuine_reversal: NOUL "a liquidity sweep is a genuine reversal"
    OUTPUT:
      - decision
      - probabilities
      - reasons

The initial DSL is intentionally declarative. Dynamic guards and policy
expressions remain in typed Python DecisionPolicy configuration so that the
language cannot execute arbitrary code.

## Provider integration

Jev is not assumed to have a public, stable API shape beyond what the operator
can verify from current official SDK/documentation. Inject a callable into
JevDecisionProvider; the adapter passes a neutral request payload and validates
the returned ModelResponse. Do not store credentials in source or logs. Add
timeouts, circuit breaking, retry budgets and request-cost limits only after
the actual SDK semantics and latency are verified.

A native provider can implement the DecisionProvider protocol using local models.
Provider outputs remain uncalibrated until matured historical outcomes are available.

## Integration plan

### Implemented in this change
- Typed contracts, constrained DSL parser and provider protocol.
- Jev callable adapter without an invented endpoint.
- Correlation-aware assessment aggregation.
- Bucket calibration and proper scoring metrics.
- Fail-closed, advisory-only runtime.
- Idempotent in-memory attribution contract and regression tests.

### Remaining integration work
- Scanner candidate observer emitting point-in-time MarketSnapshot objects.
- Independent feature adapters for structure, liquidity, order flow, AMD,
  regime, news/sentiment, funding/OI and execution quality.
- Durable decision/outcome storage using existing learning/event-store contracts.
- Historical replay with fees, funding, slippage, latency, partial fills and
  counterfactual rejected-trade outcomes.
- Regime/symbol walk-forward calibration, drift monitoring and champion/challenger
  governance.
- Metrics for provider latency, timeout, stale snapshot, abstention,
  disagreement, calibration quality and decision outcomes.

## Safety and rollout gates

1. Unit and contract tests pass on Python 3.10–3.12.
2. Historical point-in-time replay demonstrates no leakage.
3. Compare baseline vs ADL shadow vs native/Jev provider on identical data.
4. Report sample counts, confidence intervals, calibration, EV, drawdown,
   tail risk and rejected-trade counterfactuals.
5. Validate model failure, timeout, stale state and duplicate delivery.
6. Run paper-only shadow canary with operator-approved configuration.
7. Require human approval before policy activation or strategy/risk changes.

No predictive lift, live readiness, Jev API compatibility or production
calibration is claimed by this initial framework.
