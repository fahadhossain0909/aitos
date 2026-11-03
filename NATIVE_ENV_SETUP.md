# AITOS Native Environment Setup

## Services Running Natively

### Redis (Port 6379)
- **Binary**: `/usr/bin/redis-server`
- **Password**: From `.env` `REDIS_PASSWORD`
- **Data**: `/mnt/aitos-data/eventbus/redis/`
- **Status**: UP (verified)

### ClickHouse (Port 8123 HTTP, 9000 Native)
- **Binary**: `/usr/bin/clickhouse-server`
- **Password**: From `.env` `CLICKHOUSE_PASSWORD`
- **Data**: `/mnt/aitos-data/databases/clickhouse/`
- **Status**: UP (verified via HTTP)
- **Note**: Native port 9000 has auth issues; use HTTP port 8123

### Neo4j (Port 7687 Bolt, 7474 HTTP)
- **Binary**: `/usr/bin/neo4j`
- **Password**: From `.env` `NEO4J_PASSWORD`
- **Data**: `/mnt/aitos-data/databases/neo4j/`
- **Status**: UP (verified)
- **Version**: 5.19.0 Community

## Quick Verification

```bash
# Redis
redis-cli --no-auth-warning ping

# ClickHouse
curl -s "http://default:$CLICKHOUSE_PASSWORD@localhost:8123/?query=SELECT%201"

# Neo4j
curl -s http://localhost:7474/
```

## Running AITOS Paper Trading

```bash
source .venv/bin/activate
python3 run_paper_trading.py
```

## Services Auto-Start

Redis is configured to start automatically. ClickHouse and Neo4j
are installed as system packages. All three are verified running.
