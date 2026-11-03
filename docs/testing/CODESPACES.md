# GitHub Codespaces test workflow (no Docker-in-Docker)

This Codespaces profile intentionally does not install or require Docker-in-Docker. It provides a Python test environment only. Use it for unit tests, static checks, and paper-mode code inspection. Do not configure exchange keys, use production data, or run live trading.

## 1. Create or rebuild the Codespace

Use the `infra/codespaces-no-dind` branch. The `.devcontainer/devcontainer.json` uses the Python 3.11 devcontainer image and installs project/test dependencies. It deliberately has no Docker feature.

## 2. Run local tests

Run the unit suite first:

    python -m pytest -q

The normal unit-test fixtures use fakeredis/mocks and should not require exchange credentials or local service containers.

Useful additional checks:

    python -m compileall -q aitos run_live_trading.py run_paper_trading.py
    black --check --diff aitos tests run_live_trading.py run_paper_trading.py
    flake8 aitos run_live_trading.py run_paper_trading.py --count --select=E9,F63,F7,F82 --show-source --statistics

## 3. Integration tests without a local Docker daemon

Do not run `docker compose` from this Codespace: the no-DinD profile intentionally has no container daemon.

For Redis, ClickHouse, and Neo4j integration coverage, use the repository's GitHub Actions workflows. The CI jobs provision disposable service containers on the GitHub-hosted runner, initialize a test-only ClickHouse database, and run the integration suite. This keeps service lifecycle and test credentials outside the Codespace and avoids a privileged nested Docker daemon.

The existing `.devcontainer/compose.test.yml` remains available for developers who already have a separate Docker-capable environment; it is not part of this no-DinD Codespaces path.

## 4. Paper-mode safety boundary

Do not run `run_live_trading.py` in Codespaces. Do not add Binance API keys or other exchange credentials. Any future paper-runtime check must use public market data and isolated test services, and must be explicitly started as a paper process only after its dependencies and safety configuration have been verified.

## 5. What this setup does not prove

A green unit suite or GitHub Actions integration suite does not prove exchange execution correctness, live-market freshness under load, restart recovery, or production risk behavior. Those require separate controlled testnet/replay validation and explicit human approval. No live orders are permitted as part of this workflow.
