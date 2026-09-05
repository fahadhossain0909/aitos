# AITROS Concept Integration in AITOS

## Objective

AIDOS remains a trading-first system. AITROS concepts are adopted as architectural
principles, not as a second agent framework.

## Canonical lifecycle

```text
Market Evidence
      |
      v
Knowledge / Context
      |
      v
Decision / Scenario Analysis
      |
      v
Policy + Risk Gate  -----> NO TRADE (fail closed)
      |
      v
ExecutionIntent
      |
      v
Venue / Broker Adapter
      |
      v
Trade Outcome / Journey
      |
      v
Learning + Evaluation
      |
      +----> versioned knowledge / policy / replay record
```

## Rules

1. **Evidence before action.** Decisions must carry their evidence hash and
   rationale.
2. **Knowledge is explicit.** Market regime, state, cross-market context and
   prior context are first-class data, not hidden agent memory.
3. **AI is advisory, policy is authoritative.** An AI/ML/LLM scorer can propose
   a direction, but stale data, confidence floors and risk controls can veto it.
4. **Fail closed.** Missing or stale evidence produces `no_trade`; execution
   adapters never receive a live order from an unapproved decision.
5. **Execution is an intent boundary.** Intelligence never calls an exchange
   or broker API directly.
6. **Learning is separated from promotion.** Trade outcomes may update research
   data, but live policy changes require the existing governed/shadow promotion
   process.
7. **Replayability is mandatory.** A decision can be serialised and replayed
   from its knowledge snapshot for regression, attribution and audit.
8. **Market agnosticism remains an invariant.** Crypto is the first production
   market; asset-specific behavior belongs in adapters and market contracts.

## Components already present in PR #116

- Canonical market-data contracts and event identities.
- Cross-market intelligence and global market regime/state.
- Contextual Decision Engine and historical analogues.
- Trade Journey Engine with MAE/MFE telemetry.
- Decision journal, evidence attribution/shadow evaluation and adaptive policy.
- Policy governance with explicit shadow-approved promotion and rollback.
- Universal portfolio/risk gate and ExecutionIntent boundary.

## New governed autonomy primitive

`aitos.intelligence.autonomy_pipeline` supplies a small deterministic contract
for the lifecycle above. It intentionally does not replace the existing
Contextual Decision Engine, Trade Journey, portfolio risk, or journal modules.
It provides the common envelope in which those components can exchange
serialisable evidence, knowledge, decisions, policy results, intents and
outcomes.

### Integration sequence

- Scanner/intelligence populates `KnowledgeSnapshot`.
- Existing contextual/scenario logic supplies the decision function.
- Existing capital/risk controls supply the risk approval signal.
- `FailClosedPolicy` performs the final safety validation.
- The resulting `ExecutionIntent` crosses to the existing execution adapter.
- Trade Journey/journal records the outcome as `LearningOutcome`.
- Existing shadow evaluation and `PolicyGovernance` remain the only path to a
  promoted live policy.

This keeps AITROS-derived governance additive and avoids duplicating the
production trading engines already established by PR #116.
