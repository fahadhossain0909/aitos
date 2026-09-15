# AITOS Environment Status

## Native Environment (No Docker)

### Services Running Natively

| Service | Binary | Port | Protocol | Status |
|---------|--------|------|----------|--------|
| Redis | `/usr/bin/redis-server` | 6379 | TCP | ✅ UP |
| ClickHouse | `/usr/bin/clickhouse-server` | 8123 (HTTP) / 9000 (Native) | HTTP/Native | ✅ UP |
| Neo4j | `/usr/bin/neo4j` | 7687 (Bolt) / 7474 (HTTP) | Bolt/HTTP | ✅ UP |

### Data Directories
- Redis: `/mnt/aitos-data/eventbus/redis/`
- ClickHouse: `/mnt/aitos-data/databases/clickhouse/`
- Neo4j: `/mnt/aitos-data/databases/neo4j/`

### Secrets
All credentials stored in `/home/fahad/aitos/.env`:
- `REDIS_PASSWORD`
- `CLICKHOUSE_PASSWORD`
- `NEO4J_PASSWORD`
- `BINANCE_API_KEY`

### Installation Method
All services installed via `apt-get` (not Docker):
```bash
sudo apt-get install redis-server clickhouse-client clickhouse-server neo4j
```

### Paper Trading
- Status: **Running** on port 8090
- Capital: $1,000 (paper)
- Mode: Binance Testnet
- Symbols: 848 loaded
- Health: http://localhost:8090/health
- Logs: /tmp/aitos-paper.log

### Monitoring
- Telegram bot: `aitos/telegram/status_bot.py`
- Monitoring script: `native-env/monitor.py`
- Hourly updates via `native-env/telegram_hourly.sh`

### Last Updated
2026-09-15
