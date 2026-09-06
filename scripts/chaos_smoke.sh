#!/usr/bin/env bash
# Controlled dependency-failure smoke test for staging/paper environments.
# Never run against a live-money profile.
set -Eeuo pipefail

if [[ "${ENVIRONMENT:-production}" == "production" ]]; then
  echo "Refusing chaos test in production. Set ENVIRONMENT=staging or papertrade." >&2
  exit 2
fi

compose=(docker compose)

echo '=== CHAOS: Redis restart ==='
"${compose[@]}" stop redis
"${compose[@]}" start redis
"${compose[@]}" up -d --wait redis

echo 'Redis recovered.'

echo '=== CHAOS: Neo4j restart ==='
"${compose[@]}" stop neo4j
"${compose[@]}" start neo4j
"${compose[@]}" up -d --wait neo4j

echo 'Neo4j recovered.'

echo '=== CHAOS: application restart ==='
"${compose[@]}" restart aitos-paper
"${compose[@]}" up -d --wait aitos-paper
curl -fsS http://127.0.0.1:8090/health >/dev/null

echo '=== CHAOS TEST PASSED ==='
