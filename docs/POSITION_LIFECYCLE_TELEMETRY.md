# Position Lifecycle Telemetry

## Purpose

Extend AITOS observability from market-data ingestion through scanner decisions to
the full open-position lifecycle. This instrumentation is observational and must
not change position sizing, exit thresholds, order execution, stop handling, or
the strategy's adaptive exit behavior.

## Event contract

All position events use a shared structured payload in aitos_extra. When available,
trade_id is the canonical position identifier, position_id is its explicit alias,
and trace_id=position:<trade_id> correlates events across the process.
Every event also receives an event ID and UTC timestamp.

Key event families:

- position.evaluation.completed: evaluation duration, price, monitor tier,
  priority score, reasons, action, and market-context availability.
- position.monitor.heartbeat: update-price cycle duration, open-position
  counts before/after, and each affected position's state.
- position.close.attempt, position.close.completed, and
  position.close.failed: close reason, requested price, outcome, duration,
  and exception category.
- position.monitor.stale_candidate: emitted when an observed position has
  not received a fresh price/evaluation for a configured diagnostic window.
- Existing lifecycle submit, subscription, exit-decision, order, and fill events
  remain authoritative sources for their respective stages.

## Safety and performance

- Telemetry failures are swallowed; telemetry cannot veto or delay a trade action
  by raising into the trading path.
- Events are emitted per position evaluation and close attempt, not per raw
  market tick at the global ingestion boundary.
- Payloads are bounded and JSON-friendly. Do not log credentials, API keys, or
  full order-book snapshots.
- Absence of an evaluation event is not interpreted as HOLD.
- A close is only considered confirmed by the existing lifecycle/exchange state;
  a close request or order submission alone is not a successful close.
- Hard stops and existing adaptive Exit Intelligence behavior remain unchanged.

## Validation

Run the focused test suite:

    pytest -q tests/test_position_lifecycle_telemetry.py tests/test_position_runtime.py

Then run the repository's normal CI and paper-trading regression suite. Verify
that logs correlate a position from evaluation through close, including failed
and partial close paths, and compare runtime overhead before enabling high-volume
debug logging.


## Forensic report

The CLI report groups events by position and highlights positions without a recent
evaluation or with no evaluation evidence. It consumes the existing JSON-lines
logs and does not query or mutate trading state:

    python scripts/position_lifecycle_telemetry_report.py aitos.log --stale-after-seconds 120

Use '-' instead of a logfile path to read stdin. A stale report is a diagnostic
signal, not a command to close a position; verify current exchange state and the
live monitor before taking action.
