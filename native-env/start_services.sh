#!/bin/bash
set -e
# Start Redis
redis-server --daemonize yes --requirepass "$(grep REDIS_PASSWORD /home/fahad/aitos/.env | cut -d= -f2)" --port 6379 --dir /mnt/aitos-data/eventbus/redis/live --appendonly yes --maxmemory 2gb --maxmemory-policy noeviction
# Start ClickHouse (already running via apt)
# Start Neo4j (already running via neo4j package)
echo "All AITOS services started."
