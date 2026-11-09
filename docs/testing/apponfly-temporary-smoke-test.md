# AppOnFly temporary AITOS smoke testing

This is an **on-demand, short-lived workstation workflow**, not an AITOS hosting or production deployment design.

## Verified feasibility

A temporary AppOnFly anonymous trial was launched on 2026-11-08. The public AITOS repository was shallow-cloned and a focused offline test passed:

- Windows Server 2025 Standard (64-bit)
- Git 2.56.0.windows.4
- Python 3.12.10 (`py -3.12`)
- Observed resources: 1 logical CPU, about 1.34 GiB RAM, about 36.6 GiB free disk
- Result: `1 passed, 2 warnings in 0.06s`
- Test: `tests/backtest/test_queue_lifecycle.py`

The ordinary pytest invocation initially failed because the repository root `tests/conftest.py` imports `pytest_asyncio`, which was not installed in the intentionally minimal environment. The successful focused invocation isolated the backtest test directory:

    python -m pytest -q --confcutdir=tests/backtest tests/backtest/test_queue_lifecycle.py

This proves that a **small, selected offline test** can run on the temporary workstation. It does not prove that the complete AITOS suite, Docker stack, learning services, market-data runtime, or Binance Futures Testnet workflow will fit or complete within the trial.

## Run it on a fresh AppOnFly trial

1. Open the official trial page: <https://app.apponfly.com/trial>.
2. Start the anonymous Windows desktop if the free trial is offered. Do not enter payment details or credentials.
3. Open PowerShell in the desktop.
4. Run the following command to fetch and execute the versioned helper from the repository:

       $script = "https://raw.githubusercontent.com/fahadhossain0909/aitos/feat/quantitative-intelligence-engine/scripts/apponfly_aitos_smoke.ps1"
       Invoke-RestMethod $script | Invoke-Expression

5. The script checks Git and Python 3.12, installs either with `winget` if missing, shallow-clones the public repository's `main` branch, creates a virtual environment, installs only `pytest`, and runs the focused offline smoke test.
6. Review the printed log path under `%TEMP%`. Save/copy the log before the temporary session expires if it is needed outside the session.

To use a different branch, download the script and invoke it with parameters instead of piping it directly to `Invoke-Expression`:

       Invoke-RestMethod $script -OutFile "$env:TEMP\apponfly_aitos_smoke.ps1"
       & "$env:TEMP\apponfly_aitos_smoke.ps1" -Ref "main"

For another existing branch, replace `main` with that branch name. The default test path can be overridden with `-TestPath`, but use a test that is known to be offline and lightweight.

## Safety and scope

- This script does not read or request Binance API keys, exchange credentials, or other secrets.
- It does not place live or testnet orders.
- It does not start Docker Compose, persistent services, or a public server.
- It installs only Git/Python (when missing) and `pytest`; it does not install the full AITOS dependency set.
- Trial availability, resource limits, and session duration can change. The observed official trial page showed a 30-minute session.
- If Git/Python installation is blocked, a dependency is missing, or the selected test fails, the script records the failure and exits non-zero. Do not interpret an environment/setup failure as a code regression without inspecting the log.
- The helper requires an interactive human/browser step to open the anonymous AppOnFly trial. No supported AppOnFly API for unattended trial provisioning was verified; therefore this is a reusable **test-runner script**, not a fully unattended trial-launch bot.

## Next validation tier

Only after this focused test is consistently repeatable should the workflow be expanded to install `requirements-backtest.txt` and run a broader offline backtest suite. Treat real-time Binance Futures Testnet connectivity as a separate, explicitly authorized test tier; never add live trading or unattended order placement to this workflow.
