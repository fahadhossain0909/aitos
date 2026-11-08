# AITOS Continuous Intelligence & Evolution

## Purpose

This layer closes the gap between stored trading experience and evidence-backed
research. It is deliberately read-only with respect to trading behavior.

The canonical data path remains:

`journal.decision_recorded` / `journal.outcome_attributed`
-> `LearningExperienceRecorder`
-> ClickHouse `learning_experiences`
-> continual-learning worker / research analysis.

It does not create a second trade ledger, replace the RL feedback loop, or
subscribe to market-data topics on the hot path.

## Implemented capabilities

### Performance attribution

`aitos.learning.continuous_intelligence.ContinuousIntelligenceEngine`
accepts normalized Experience Store records and produces:

- strategy x regime realized-R, win-rate, profit-factor, MAE/MFE and cost slices;
- symbol x side performance slices;
- eligible and rejected experience counts;
- explicit evidence-tagged root-cause groupings, using the decision snapshot linked by `decision_id` when available.

Only closed trade-level experiences are eligible. Explicit R-unit rewards are accepted directly. Backtest quote-currency PnL is normalized by positive initial risk using the same eligibility rules as the continual-learning worker; summary rows are excluded. Paper/live quote-PnL is accepted only when the quote-PnL unit is explicit and positive initial risk is present. Unitless legacy paper/live rows, open trades and malformed/non-finite rewards are quarantined. The analyzer does not infer an
unrecorded cause from a loss.

### Root-cause diagnostics

Findings are emitted only when the outcome or its linked decision snapshot contains explicit structured
diagnostics such as `data_stale`, `regime_mismatch`, `adverse_order_flow`,
`execution_degraded`, `premature_exit`, `missed_opportunity` or
`risk_control_triggered`. A finding's confidence field is evidence coverage
(the share of analyzed trades carrying that tag), not a calibrated causal
probability. The fallback material execution-cost threshold defaults to 0.25R
and is configurable; it is a diagnostic threshold, not a trading gate.

### Counterfactual replay

Counterfactual summaries accept only scenarios explicitly marked
`source_type="historical_replay"` with an evidence ID and replay provenance.
Alternative outcomes are netted for supplied estimated costs. Model-generated
guesses are rejected. Counterfactual deltas are research diagnostics, not
realized PnL and never enter the reward stream.

### Evidence-backed research hypotheses

`ContinuousIntelligenceEngine.generate_hypotheses()` converts sufficiently
supported root-cause findings into bounded research questions and experiment
designs (for example, premature-exit replay, OFI/CVD entry ablation,
regime-specific comparison or execution-cost sensitivity). Each hypothesis
retains its supporting experience IDs and explicitly requires approval. It
does not author parameter values, modify strategy code or create a candidate
automatically; the existing model-specific `EvolutionEngine` proposer and
canonical validation pipeline remain responsible for candidate construction.

### Drift monitoring

`DriftDetector` computes baseline-quantile PSI for numeric features and a
standardized reward-mean shift. It reports `insufficient_samples` until both
sides meet the configured minimum. It emits alerts only: it does not
automatically recalibrate, retrain, change thresholds or disable strategies.

### Decision-to-outcome knowledge graph

The existing optional Neo4j `KnowledgeGraphWriter` now projects the explicit
`journal.decision_recorded` and `journal.outcome_attributed` semantic events. A closed-trade outcome with both `decision_id`
and `trade_id` creates/links an Outcome node and direct
`Decision-[:RESULTED_IN]->Outcome-[:OUTCOME_OF]->Trade` relationships.
The journal event retains its structured payload, including the linked
decision snapshot and diagnostic tags. ClickHouse remains the canonical
analytical ledger; Neo4j is a relationship/navigation layer and is not used
as the reward source or execution authority.

### Promotion governance

`ModelRegistry.promote` now requires a `PromotionApproval` bound to the exact
candidate name/version and SHA-256 digest of the passing validation result.
The approval must be recent and timezone-aware; approval IDs are single-use
across registry promotion evidence. Existing walk-forward, locked-holdout and
champion/challenger statistical gates remain mandatory.

The journal now carries the originating decision snapshot into the durable outcome record when the decision ID is available, recovering it from `DecisionJournalRepository` if the in-memory cache was lost during restart. This preserves candidate scores, component availability, weights, thresholds and rationale for later attribution; it does not invent root causes from those values.

The approval envelope is an audit and workflow control, not identity
authentication. A production UI/API must authenticate the human approver and
must only issue the envelope after an explicit approval action. No unattended
agent should construct approvals.

## Example

The analyzer can read a bounded, timezone-aware window directly through the canonical `ClickHouseExperienceStore`; this is an offline/reporting query, not part of the market-data or order-execution hot path:

```python
from aitos.learning.clickhouse_store import ClickHouseExperienceStore
from aitos.learning.continuous_intelligence import ContinuousIntelligenceEngine

store = ClickHouseExperienceStore(clickhouse_client)
report = ContinuousIntelligenceEngine().analyze_store(
    store, start_utc, end_utc, sources=("paper", "backtest"), limit=100_000
)
payload = report.to_dict()
```

For drift, provide separate baseline and current feature samples and explicitly aligned reward windows (same strategy/regime and compatible sampling horizon):

```python
report = ContinuousIntelligenceEngine().analyze(
    experience_records,
    baseline_features={"cvd": historical_cvd, "ofi": historical_ofi},
    current_features={"cvd": recent_cvd, "ofi": recent_ofi},
    baseline_rewards=historical_strategy_rewards_r,
    current_rewards=recent_strategy_rewards_r,
)
```

For counterfactuals, provide replay-produced scenarios with source and evidence
identifiers. Do not construct scenarios from a model's unverified prediction.

## Validation

Run focused tests:

```bash
pytest -q tests/learning/test_continuous_intelligence.py tests/learning/test_continuous_intelligence_store.py
pytest -q tests/test_continual_learning.py tests/test_continual_learning_integration.py
```

## Deliberate safety boundaries

- Cross-market parameter/model transfer is not enabled by this PR. It requires
  explicit asset-class and feature-schema identity plus independent target-market
  walk-forward/locked-holdout evidence; symbol-name inference is not sufficient.
- Drift alerts and research hypotheses never alter entry/exit rules, thresholds,
  position sizing, leverage or risk limits.
- Candidate promotion requires the existing validation gates and an explicit
  operator approval envelope. Production deployment remains a separate action.
- No live orders or VPS deployment are part of this implementation.

## Remaining operational certification

The analyzer and approval contract are source-level capabilities. Before
claiming end-to-end continuous intelligence is runtime-certified, the
operator must still verify against the local paper stack:

1. every eligible paper close is present once in ClickHouse with an explicit R
   unit, strategy/regime identity and decision/trade linkage;
2. actual source diagnostics populate root-cause tags consistently;
3. historical replay produces provenance-bearing counterfactual scenarios;
4. baseline/current feature windows are time-aligned and point-in-time safe;
5. the reporting job can query the real ClickHouse dataset without blocking the
   paper execution path;
6. approval is issued by an authenticated human interface and the resulting
   registry promotion/rollback is auditable;
7. Prometheus/Grafana expose freshness, sample counts, drift status, report
   failures and learning-worker health.

No strategy/risk semantics, live order behavior, VPS deployment or automatic
production promotion are changed by this subsystem.


## Experiment orchestration and rollout lifecycle

Use `ExperimentEngine` with the canonical BacktestEngine-backed evaluator to run paired baseline/ablation comparisons across regimes and fee/slippage scenarios. The evaluator must return finite `total_return`, `max_drawdown`, `sharpe`, `trades`, and `sample_count`. Reports are evidence only; they cannot tune, mutate, or promote a strategy. A supplied holdout interval is evaluated exclusively and must remain reserved from candidate/parameter selection.

`RolloutCoordinator` persists stage/canary/promotion/rollback state under a cross-process file lock and atomic replacement. It requires authenticated deployment adapters for canary, production and rollback routing. Canary threshold breaches invoke rollback; promotion requires minimum canary samples, unchanged staged validation digest, the existing statistical gates and explicit operator approval. If a first-ever champion cannot be restored after a routing failure, the state becomes `rollback_pending` for manual reconciliation.

These are orchestration primitives, not proof that the authenticated approval UI, deployment router, periodic canary metrics collector or paper/live reporting schedule is configured. Those adapters must be wired and exercised in the target runtime before production rollout is certified.


## Holdout isolation contract

Candidate-level minimum-return, drawdown, Sharpe and champion-comparison gates run only on development events strictly before the reserved final holdout interval. Walk-forward test windows and the final locked holdout are evaluated separately and are never included in those candidate-level metrics. Event timestamps must be timezone-aware. Candidate strategies are treated as frozen proposals during validation; fitting or tuning must occur outside the validator on development data only. Regime-wise ablations and cost-sensitivity are produced by the experiment runner and remain evidence reports, not automatic model-selection decisions.

Canary health requires both a cumulative sample-count threshold and a minimum number of separate healthy observation windows (default: 100 samples and 3 observations). Each observation must report cumulative samples and the same defined candidate-versus-champion metric basis; a single healthy snapshot cannot clear the canary gate.

## Frozen research input contract (2026-11-07 audit)

The candidate development run evaluates a deep copy of the supplied strategy, leaving
its frozen initial state available to the independent walk-forward/holdout copies.
Stateful strategies must encapsulate state in copyable callable objects; mutable Python
function closures, global state and external services are not isolated by `deepcopy`.
This does not verify how model parameters were fitted before the proposal was supplied.

Each experiment cell receives a deep copy of its event slice. Baseline, ablations and
cost scenarios may annotate their own nested fields without changing caller-owned
history or another cell's input. This costs offline CPU/memory proportional to the slice;
large replay optimization requires measurement and an immutable-data contract.

Cost-scenario names must be unique. Experiment identity now includes `minimum_samples`,
so a different sample acceptance policy cannot share an ID with the original plan.
IDs produced by the earlier version are historical IDs, not values to silently rewrite.
The hash still does not bind all evaluator code, model artifacts or upstream datasets;
complete provenance and a canonical evaluator adapter remain separate integration work.
