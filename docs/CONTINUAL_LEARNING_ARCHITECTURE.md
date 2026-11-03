# ProjectAlpha Continual-Learning Architecture

## Goal

Backtest, paper trading, and live trading are separate execution stages, but they
share one learning lifecycle. Historical and operational data becomes reusable
experience for training, evaluation, research, and future replay.

```text
Historical/ClickHouse data ──┐
Paper decisions/outcomes ────┼──> Experience Store ──> Continual Learners
Live decisions/outcomes ─────┘                              │
                                                           ▼
                                                    Candidate model
                                                           │
                                                           ▼
                                               Canonical Backtest Engine
                                                           │
                                                           ▼
                                                Validation / Walk-forward
                                                           │
                                                     Paper / Shadow
                                                           │
                                                     Promotion gate
                                                           ▼
                                                       Champion
```

## Components added in this phase

- `aitos.learning.experience.ExperienceRecord`: canonical decision/outcome
  contract for `backtest`, `paper`, and `live`.
- `aitos.learning.clickhouse_store.ClickHouseExperienceStore`: append-only
  ClickHouse persistence for long-lived learning experience.
- `aitos.learning.evolution.EvolutionEngine`: bounded, auditable change
  proposals. It does not deploy changes.
- `aitos.learning.validation.CandidateValidator`: evaluates candidates using
  the existing canonical `BacktestEngine` rather than creating a second
  simulator.
- `aitos.learning.model_registry.ModelRegistry`: versioned candidate/champion
  registry with explicit promotion.
- `aitos.learning.pipeline.ContinualLearningPipeline`: connects evolution
  proposals to canonical backtest and validation.

## Important design rules

1. **One canonical simulator.** Deep RL policies, strategy changes, risk-model
   candidates, and other candidates are evaluated through the same backtest
   engine and execution assumptions.
2. **Learning is continuous; deployment is gated.** New experiences may
   continuously accumulate and trigger training, but a candidate does not become
   the production champion automatically.
3. **Experience is persistent.** ClickHouse experience is not a disposable log;
   it is intended for future backtests, training, error analysis, walk-forward
   evaluation, and research.
4. **Execution isolation is preserved.** Backtest remains an opt-in Compose
   service and cannot mutate the paper portfolio merely by running.
5. **Model provenance is mandatory.** Every candidate records its parent version,
   proposal/training-data identifier, metrics, and rationale.
6. **Out-of-sample evaluation is required before promotion.** The current
   validator provides the first deterministic gate; walk-forward/locked-holdout
   orchestration should be the next layer before autonomous promotion is enabled.

## What is intentionally not autonomous yet

This phase does **not** allow a learner to deploy a candidate directly to live
trading. The safe lifecycle is:

`experience -> learn -> propose -> backtest -> validate -> paper/shadow -> human/production gate -> champion`.

That separation lets the learning system evolve continuously without allowing a
single bad training episode or data-quality incident to rewrite production
trading logic.
