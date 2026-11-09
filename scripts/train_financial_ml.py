#!/usr/bin/env python3
"""Run leakage-aware Financial ML walk-forward training; never auto-promote."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from aitos.config.paths import get_model_data_dir
from aitos.statistics.financial_ml_pipeline import FinancialMLPipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=get_model_data_dir() / "financial_ml.sqlite3",
    )
    args = parser.parse_args()
    pipeline = FinancialMLPipeline(args.database, mode="shadow", enforce=False)
    try:
        report = pipeline.train_walk_forward()
        print(json.dumps(asdict(report), sort_keys=True, indent=2))
        return 0 if report.passed_oos else 1 if report.trained else 2
    finally:
        pipeline.close()


if __name__ == "__main__":
    raise SystemExit(main())
