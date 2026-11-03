# AITOS Historical Strategy Backtester

The repository now contains a reusable historical backtesting path that is independent of any single strategy.

## Architecture

```text
GitHub Actions
    |
    | checkout selected ref
    v
Strategy Backtest CLI
    |
    | SSH tunnel (secrets only)
    v
VPS ClickHouse
    |
    | historical events
    v
Deterministic replay
    |
    +--> baseline / control
    +--> strategy variant
    |
    v
Canonical JSON contract + artifact
```

## Workflows

- `Strategy Historical Backtest` — one selected historical window.
- `Strategy Historical Backtest Suite` — the same strategy against 24h, 72h, 168h and 720h windows.

The suite is intended for strategy proposals before merging strategy code into `main`.

## Contract for a new strategy

A strategy backtest module should be importable as a Python module and accept the canonical CLI arguments:

```text
--symbol --side --start --end --timeframe
--host --port --user --password --database
```

It must emit JSON containing:

```text
symbol, side, start, end
baseline: {final_cash, net_pnl, max_drawdown, mae, mfe}
hedged:   {final_cash, net_pnl, max_drawdown, mae, mfe}
```

Strategy-specific fields may be added. The workflow never stores ClickHouse credentials in source or artifacts.

## Why this is reusable

Historical data remains on the VPS. GitHub Actions only opens a short-lived SSH tunnel and executes the selected strategy replay against ClickHouse. This avoids repeatedly exporting large datasets into GitHub while preserving a reproducible artifact for review.

Before merging a new strategy, reviewers should compare the artifact across multiple windows and require explicit evidence for PnL, drawdown, MAE/MFE, costs, and expectancy. A positive result in one short window is not sufficient for production approval.
