# GitHub Codespaces test workflow

This environment is for tests and paper-mode development only. Do not configure exchange keys, do not run live trading, and do not reuse production data.

## 1. Create the Codespace

Open the repository's `audit/verify-main-runtime-reliability` branch in GitHub and choose **Code → Codespaces → Create codespace on branch**. The `.devcontainer/devcontainer.json` installs the Python dependencies and Docker-in-Docker feature.

## 2. Run the unit suite

    python -m pytest -q

The normal test fixtures use fakeredis and should not need real credentials.

## 3. Start isolated integration services (only if needed)

These services use disposable test credentials and are defined separately from the project's regular runtime compose stack:

    docker compose -f .devcontainer/compose.test.yml up -d
    docker compose -f .devcontainer/compose.test.yml ps

Set test-only connection settings in the current terminal before running integration tests:

    export TESTING=true
    export REDIS_HOST=127.0.0.1
    export REDIS_PORT=6379
    export REDIS_PASSWORD=codespaces-test-only
    export CLICKHOUSE_HOST=127.0.0.1
    export CLICKHOUSE_PORT=8123
    export CLICKHOUSE_USER=aitos_test
    export CLICKHOUSE_PASSWORD=aitos_test_password
    export CLICKHOUSE_DATABASE=aitos
    export NEO4J_URI=bolt://127.0.0.1:7687
    export NEO4J_USER=neo4j
    export NEO4J_PASSWORD=codespaces-test-password
    python -m pytest -q

Do not run `run_live_trading.py` in Codespaces. The isolated service containers are for test data only. Tear them down when finished:

    docker compose -f .devcontainer/compose.test.yml down -v

## 4. What this setup does not prove

A green unit/integration suite does not prove exchange execution correctness, live-market freshness under load, or production risk behavior. Those require controlled testnet/replay tests and explicit human approval. No exchange API keys are required for this test workflow.
