# ProjectAlpha Continual Learning Architecture

ProjectAlpha uses **execution isolation with shared learning**. Backtest, paper, and live execution are separate operational stages, but their durable experiences feed the same learning/evaluation lifecycle.

## Data flow

```text
Historical / Paper / Live
          |
          v
      ClickHouse
          |
          +--> Backtest / Replay
          |
          +--> Experience Store
                    |
                    +--> Online RL feedback (paper/live)
                    +--> Historical learning worker (backtest)
                    +--> Evolution proposals
                    +--> Evaluation
                    +--> Model registry
```

ClickHouse is the long-lived source of market history and learning evidence. Redis remains the real-time event/cache layer; Neo4j remains the relationship/knowledge-graph layer.

## Experience records

`learning_experiences` is append-only. A paper/live decision is recorded when `journal.decision_recorded` is emitted, and the later outcome is recorded when `journal.outcome_attributed` is emitted. The records retain source, symbol, decision, confidence, features, market state, risk state, strategy/model versions, and identifiers linking the evidence.

Backtests can persist both a completed-run summary **and realized trade outcomes** with `--persist-learning`. Realized outcomes include the historical event timestamp, reward/P&L, price, and numeric event features so they can be replayed by the learning worker.

## Historical data for backtests

The canonical backtest CLI supports both files and ClickHouse:

```bash
# Local JSONL/Parquet
python3 -m aitos.backtest.cli --source file --data /data/events.jsonl

# Persisted ClickHouse OHLCV
python3 -m aitos.backtest.cli \
  --source clickhouse \
  --symbol BTCUSDT \
  --table ohlcv \
  --timeframe 15m \
  --start 2026-01-01T00:00:00+00:00 \
  --end 2026-06-01T00:00:00+00:00 \
  --persist-learning
```

`trades` and `orderbook` are also supported. The source only reads historical rows; it never mutates production trading state.

## Evolution and validation

Evolution models **propose** changes; they do not deploy them. A candidate must be evaluated with the same canonical `BacktestEngine` used by the rest of the project. The validation layer checks return, drawdown, Sharpe, trade count, and improvement against the current champion.

For leakage-resistant evaluation, `aitos.learning.walk_forward.WalkForwardValidator` provides sequential unseen test windows plus an optional locked holdout. The holdout must not be used to tune a candidate.

```text
Experience
   -> Evolution proposal
   -> Candidate strategy/model
   -> Canonical BacktestEngine
   -> Walk-forward / locked holdout
   -> Paper/shadow validation
   -> Promotion gate
```

No automatic production promotion is enabled by the continual-learning foundation.

## Continual model learning

The existing `RLFeedbackLoop` updates the configured trainable scorer after every closed paper/live trade. This is an online learning path, not a one-shot training job. The repository also contains the neural `DeepValueRLScorer`; it is a value-function approximator trained online from realized R-multiple outcomes, not a claim of full actor-critic/policy-gradient RL.

The `aitos-learning` Compose service runs `run_continual_learning.py` continuously. It replays persisted **backtest** outcomes from ClickHouse into the same durable neural scorer. Paper/live outcomes are deliberately not replayed by this worker because `RLFeedbackLoop` already trains from those events in real time; this avoids duplicate updates after a restart.

Model artifacts are persisted on the shared `/models` volume. Candidate/champion/archived states make promotion and rollback auditable.

## VPS operation

Backtest remains an opt-in Compose profile:

```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.cli \
  --source clickhouse \
  --symbol BTCUSDT \
  --table ohlcv \
  --timeframe 15m
```

Paper trading does not need to be stopped for this command. The backtest container is execution-isolated and historical input is read-only.

The learning worker starts with the normal stack and is resource-limited so it does not compete aggressively with paper trading.

## Production safety boundary

Continual learning is allowed to collect experience, train candidate models, and propose strategy/weight changes. Production deployment remains a separate governance step. A model must pass the configured validation pipeline before it can become a champion; this foundation does not silently replace a live strategy.


## Durable worker ownership and replay semantics

- Historical backtest learning is owned by the dedicated `aitos-learning` process. The paper-trading process records paper experiences and publishes outcomes; it does not start a second historical worker.
- Worker ownership is protected by a process-level lock adjacent to the durable cursor. Starting a second worker against the same state directory fails closed.
- Historical scans use a stable keyset cursor (`ingested_at, experience_id`) and do not apply a rolling seven-day cutoff. The configured batch size bounds memory, not the total learnable history.
- The Deep RL checkpoint records applied experience IDs in the same atomic checkpoint replacement as the model update. If a process crashes after the model write but before cursor persistence, replay is a no-op rather than a duplicate gradient update.
- Invalid feature payloads are counted and skipped while the cursor advances, preventing one malformed row from starving all later experiences. The neutral `rl_confidence=5.0` input is enforced in both training and inference to prevent self-referential target leakage.
- Keep the worker state file and model checkpoint on the same persistent model volume. Back up both together; do not run multiple independent model writers against separate copies of either file.

- `learning_experiences.ingested_at` is written at repository insertion time (with a migration default for existing rows), so newly imported old-dated backtests are still discovered after the worker cursor has advanced.

- Backtest summaries are excluded from trade-level RL training. Only `decision=backtest_trade`, `outcome=closed` rows are eligible. The backtest CLI marks trades without a positive risk amount as `closed_unscaled`; they remain available for analytics but are not trained as if quote-currency PnL were an R-multiple.
- Backtest rewards are normalized to R-multiples when a positive `risk_amount_usd` / `initial_risk_usd` is supplied in the replay event fields. Legacy rows are normalized from their persisted risk metadata when possible; otherwise they are skipped. Leakage fields such as realized PnL and risk amount are excluded from the model feature vector.
