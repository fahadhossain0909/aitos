#!/usr/bin/env bash
# Roll back the immutable AITOS deployment to a previously published GHCR tag.
# Usage: AITOS_ROLLBACK_TAG=sha-<commit> ./scripts/rollback_deploy.sh
set -Eeuo pipefail

TAG="${AITOS_ROLLBACK_TAG:-}"
if [[ -z "$TAG" ]]; then
  echo "AITOS_ROLLBACK_TAG is required (example: sha-<commit>)." >&2
  exit 2
fi

: "${AITOS_IMAGE:=ghcr.io/fahadhossain0909/aitos}"
export AITOS_IMAGE AITOS_IMAGE_TAG="$TAG"

echo "Rolling back to ${AITOS_IMAGE}:${AITOS_IMAGE_TAG}"
docker compose pull
docker compose up -d --remove-orphans --force-recreate

deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  if [[ "$(docker inspect --format '{{.State.Health.Status}}' aitos-redis 2>/dev/null || true)" == "healthy" && \
        "$(docker inspect --format '{{.State.Health.Status}}' aitos-clickhouse 2>/dev/null || true)" == "healthy" && \
        "$(docker inspect --format '{{.State.Health.Status}}' aitos-neo4j 2>/dev/null || true)" == "healthy" ]]; then
    echo "Rollback health gate passed."
    exit 0
  fi
  sleep 10
done

echo "Rollback health gate failed." >&2
exit 1
