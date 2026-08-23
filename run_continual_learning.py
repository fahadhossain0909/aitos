"""Run the durable continual-learning worker."""

from __future__ import annotations

import argparse
import os

from aitos.learning.worker import ContinualLearningWorker


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AITOS continual learning")
    parser.add_argument(
        "--once", action="store_true", help="process available experiences once"
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("LEARNING_POLL_SECONDS", "60")),
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=int(os.getenv("LEARNING_LOOKBACK_HOURS", "168")),
    )
    args = parser.parse_args()

    worker = ContinualLearningWorker(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        user=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DB", os.getenv("CLICKHOUSE_DATABASE", "aitos")),
        state_path=os.getenv(
            "LEARNING_STATE_PATH", "models/online_rl/worker_state.json"
        ),
        model_path=os.getenv("LEARNING_MODEL_PATH", "models/online_rl/deep_value.pkl"),
        lookback_hours=args.lookback_hours,
        poll_seconds=args.poll_seconds,
    )
    if args.once:
        worker.run_once()
        worker.close()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
