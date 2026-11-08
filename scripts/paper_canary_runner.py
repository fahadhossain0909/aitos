"""Signal-forwarding launcher for the GitHub-hosted paper canary.

The launcher owns the child process and persists its real exit status because
GitHub Actions steps run in separate shells and cannot wait() on a prior step's
background child.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="paper-canary.log")
    parser.add_argument("--exit-code", default="paper-canary.exit-code")
    parser.add_argument("--child-pid", default="paper-canary.child.pid")
    args = parser.parse_args()

    child: subprocess.Popen | None = None

    def forward_signal(signum: int, _frame) -> None:
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)

    exit_path = Path(args.exit_code)
    exit_path.unlink(missing_ok=True)
    with Path(args.log).open("w", encoding="utf-8") as log_file:
        child = subprocess.Popen(
            [sys.executable, "run_paper_trading.py"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        Path(args.child_pid).write_text(f"{child.pid}\\n", encoding="ascii")
        return_code = child.wait()

    exit_path.write_text(f"{return_code}\\n", encoding="ascii")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
