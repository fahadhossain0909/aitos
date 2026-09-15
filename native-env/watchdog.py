#!/usr/bin/env python3
"""Simple watchdog that keeps paper trading running."""
import os
import subprocess
import sys
import time

AITOS_DIR = "/home/fahad/aitos"

while True:
    # Check if paper trading is running
    result = subprocess.run(["pgrep", "-f", "run_paper_trading.py"], capture_output=True)
    if result.returncode != 0:
        print("Paper trading not running, restarting...")
        env = os.environ.copy()
        proc = subprocess.Popen(
            [sys.executable, "run_paper_trading.py"],
            cwd=AITOS_DIR,
            env=env,
            stdout=open("/tmp/aitos-paper.log", "a"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(5)
    else:
        print(f"Paper trading running (PID: {result.stdout.decode().strip()})")
    time.sleep(30)
