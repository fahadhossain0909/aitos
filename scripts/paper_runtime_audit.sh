#!/usr/bin/env bash
set -euo pipefail

printf '=== AITOS Paper Runtime Audit ===\n'
printf 'UTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if command -v docker >/dev/null 2>&1; then
  echo '--- Docker containers ---'
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo
  echo '--- Container resource usage ---'
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.RestartCount}}' 2>/dev/null || true
  echo
  echo '--- Docker disk usage ---'
  docker system df
else
  echo 'Docker: NOT INSTALLED'
fi

echo
echo '--- Host memory ---'
free -h

echo
echo '--- Host disk ---'
df -h /

echo
echo '--- AITOS paper-trading processes ---'
pgrep -af 'run_paper_trading|aitos' || true

echo
echo 'Audit complete. Review non-zero restart counts, unhealthy containers, high memory usage, and low disk space before live trading.'
