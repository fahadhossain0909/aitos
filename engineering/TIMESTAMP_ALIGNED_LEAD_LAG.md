# Timestamp-Aligned Cross-Market Lead/Lag

## Purpose

The universal market engine now exposes a timestamp-aware lead/lag estimator alongside the legacy sample-index estimator. The new estimator is intended for research and shadow analytics; it does not generate entries, change sizing, or authorize execution.

## Method

- Each accepted price observation is retained with its timezone-aware event timestamp in a bounded per-instrument deque.
- Observations are assigned to UTC epoch buckets using an explicit positive `bucket_seconds` parameter.
- The latest observation in each bucket is used as that bucket's price.
- A return is emitted only when both the current bucket and its immediately preceding bucket exist. Missing buckets are not interpolated and returns do not bridge gaps.
- The estimator correlates the leader's return in bucket `t` with the follower's return in bucket `t + lag_buckets`.
- The selected lag maximizes absolute Pearson correlation among lags with at least the configured minimum number of aligned observations. The signed correlation is retained.

## Safety and interpretation

- Events without timezone-aware timestamps remain available to the legacy sample-index path but are excluded from timestamp-aligned analysis.
- Out-of-order timestamps are not appended to the timed series.
- Bucket width is a research parameter; it must match the event cadence and market microstructure being studied.
- A high correlation is not evidence of causality. Shared market drivers, non-stationarity, stale prices, venue outages, and multiple-testing bias can create spurious lead/lag.
- The current implementation is a bounded in-memory estimator. It is not a durable historical study, a point-in-time corrected backtest, or a production alpha signal.
- Cross-market signal activation remains behind separate calibration, out-of-sample, regime, and governance gates.

## Verification

Regression tests cover a synthetic two-bucket follower delay, missing-bucket handling, and invalid lag/bucket inputs. Existing sample-index behavior remains available for compatibility.
