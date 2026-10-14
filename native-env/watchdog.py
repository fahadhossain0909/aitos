#!/usr/bin/env python3
"""Simple watchdog that keeps paper trading running."""
import os
import subprocess
import sys
import time
from pathlib import Path

AITOS_DIR = (
    Path(os.environ.get("AITOS_HOME", Path(__file__).resolve().parents[1]))
    .expanduser()
    .resolve()
)

while True:
    # Check if paper trading is running
    result = subprocess.run(
        ["pgrep", "-f", "run_paper_trading.py"], capture_output=True
    )
    if result.returncode != 0:
        print("Paper trading not running, restarting...")
        env = os.environ.copy()
        proc = subprocess.Popen(
            [sys.executable, "run_paper_trading.py"],
            cwd=str(AITOS_DIR),
            env=env,
            stdout=open(
                Path(os.environ.get("AITOS_LOG_DIR", "/tmp")) / "aitos-paper.log", "a"
            ),
            stderr=subprocess.STDOUT,
        )
        time.sleep(5)
    else:
        print(f"Paper trading running (PID: {result.stdout.decode().strip()})")
    time.sleep(30)
