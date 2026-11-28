#!/usr/bin/env bash
set -Eeuo pipefail
: "${AITOS_DATA_ROOT:=/mnt/aitos-staging-data}"
: "${BOOT_FREE_RESERVE_BYTES:=8053063680}"
: "${DATA_DISK_MIN_FREE_GB:=20}"
: "${AITOS_DATA_DISK_UUID:=}"
APP_DIR="/opt/aitos-staging"
REPO="$AITOS_REPO"
COMMIT="$AITOS_COMMIT"
IMAGE="ghcr.io/${REPO,,}:staging-sha-${COMMIT}"

mkdir -p "$APP_DIR"
exec 9>"$HOME/.aitos-staging-deploy.lock"
flock 9

echo '=== STAGING BOOT DISK PREFLIGHT ==='
boot_free=$(df -B1 --output=avail "$HOME" | tail -1 | tr -d ' ')
[ "$boot_free" -ge "$BOOT_FREE_RESERVE_BYTES" ] || { echo '::error::Staging boot disk below reserve; refusing destructive cleanup.'; exit 1; }

echo '=== STAGING STORAGE MODE PREFLIGHT ==='
ROOT_UUID="$(findmnt -rn -o UUID --target / | head -n1)"
[ -n "$ROOT_UUID" ] || { echo '::error::Unable to identify root filesystem UUID.'; exit 1; }
if [ -n "$AITOS_DATA_DISK_UUID" ]; then
  AITOS_STORAGE_MODE=external
  [ "$AITOS_DATA_DISK_UUID" != "$ROOT_UUID" ] || { echo '::error::External data UUID must differ from the OS root UUID; leave it empty to use root storage.'; exit 1; }
  DATA_DEVICE="/dev/disk/by-uuid/$AITOS_DATA_DISK_UUID"
  [ -b "$(readlink -f "$DATA_DEVICE" 2>/dev/null || true)" ] || { echo "::error::Configured external data UUID is not attached: $AITOS_DATA_DISK_UUID"; lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS; exit 1; }
  if mountpoint -q "$AITOS_DATA_ROOT"; then
    MOUNTED_UUID="$(findmnt -rn -o UUID --target "$AITOS_DATA_ROOT" | head -n1)"
    [ "$MOUNTED_UUID" = "$AITOS_DATA_DISK_UUID" ] || { echo "::error::Data root is mounted from UUID $MOUNTED_UUID, expected $AITOS_DATA_DISK_UUID."; exit 1; }
  elif [ -d "$AITOS_DATA_ROOT" ] && find "$AITOS_DATA_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo '::error::Uncovered staging data exists at the mount point; refusing to hide it under the external filesystem.'
    exit 1
  fi
  echo "Storage mode: external UUID $AITOS_DATA_DISK_UUID"
else
  AITOS_STORAGE_MODE=root
  if mountpoint -q "$AITOS_DATA_ROOT"; then
    DATA_UUID="$(findmnt -rn -o UUID --target "$AITOS_DATA_ROOT" | head -n1)"
    [ "$DATA_UUID" = "$ROOT_UUID" ] || { echo "::error::No external UUID configured, but data root is mounted from UUID $DATA_UUID."; exit 1; }
  fi
  echo "Storage mode: OS root filesystem UUID $ROOT_UUID"
fi

echo '=== STAGING SOURCE DEPLOYMENT ==='
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT
curl -fsSL --retry 3 --retry-delay 2 "https://github.com/$REPO/archive/$COMMIT.tar.gz" -o "$tmp_dir/source.tar.gz"
mkdir -p "$tmp_dir/source"
tar -xzf "$tmp_dir/source.tar.gz" --strip-components=1 -C "$tmp_dir/source"
find "$APP_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$tmp_dir/source/." "$APP_DIR/"
cd "$APP_DIR"

echo '=== STAGING STORAGE BOOTSTRAP ==='
export AITOS_DATA_ROOT AITOS_DATA_DISK_UUID AITOS_STORAGE_MODE
export AITOS_AUTO_RESET_DISPOSABLE_DATA=false
export AITOS_HOST_UID="$(id -u)" AITOS_HOST_GID="$(id -g)"
chmod +x scripts/bootstrap_storage.sh
./scripts/bootstrap_storage.sh
unset AITOS_SUDO_PASSWORD
DATA_UUID="$(findmnt -rn -o UUID --target "$AITOS_DATA_ROOT" | head -n1)"
ROOT_UUID="$(findmnt -rn -o UUID --target / | head -n1)"
if [ -n "$AITOS_DATA_DISK_UUID" ]; then
  [ "$DATA_UUID" = "$AITOS_DATA_DISK_UUID" ] || { echo "::error::Mounted data UUID $DATA_UUID does not match configured external UUID $AITOS_DATA_DISK_UUID."; exit 1; }
  [ "$DATA_UUID" != "$ROOT_UUID" ] || { echo '::error::External storage unexpectedly resolves to the OS root filesystem.'; exit 1; }
else
  [ "$DATA_UUID" = "$ROOT_UUID" ] || { echo "::error::Root storage mode expected UUID $ROOT_UUID, found $DATA_UUID."; exit 1; }
fi

echo '=== STAGING ENVIRONMENT ==='
umask 077
SECRETS_FILE="$HOME/.aitos-staging-secrets"
saved_secret() { test -f "$SECRETS_FILE" && sed -n "s/^$1=//p" "$SECRETS_FILE" | tail -n 1 || true; }
REDIS_PASSWORD="${REDIS_PASSWORD:-$(saved_secret REDIS_PASSWORD)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-$(saved_secret CLICKHOUSE_PASSWORD)}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-$(saved_secret NEO4J_PASSWORD)}"
REDIS_PASSWORD="${REDIS_PASSWORD:-$(openssl rand -hex 32)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-$(openssl rand -hex 32)}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-$(openssl rand -hex 32)}"
printf 'REDIS_PASSWORD=%s\nCLICKHOUSE_PASSWORD=%s\nNEO4J_PASSWORD=%s\n' "$REDIS_PASSWORD" "$CLICKHOUSE_PASSWORD" "$NEO4J_PASSWORD" > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"
cat > .env << ENVEOF
ENVIRONMENT=staging
LOG_LEVEL=INFO
REQUIRE_HUMAN_APPROVAL_FOR_PROD=true
DEBUG=false
TESTING=false
AITOS_DATA_ROOT=$AITOS_DATA_ROOT
AITOS_DATA_DISK_UUID=$AITOS_DATA_DISK_UUID
AITOS_STORAGE_MODE=$AITOS_STORAGE_MODE
AITOS_AUTO_RESET_DISPOSABLE_DATA=false
AITOS_MODE=paper
AITOS_PAPER_ACCOUNT_SCOPE=paper:staging
AITOS_EMBEDDED_LEARNING_WORKER_ENABLED=false
AITOS_UID=1000
AITOS_GID=1000
LIVE_TRADING_ENABLED=false
AITOS_LIVE_EXECUTION_ENABLED=false
BINANCE_FUTURES_TESTNET_API_KEY=
BINANCE_FUTURES_TESTNET_API_SECRET=
AITOS_BINANCE_FUTURES_ENVIRONMENT=mainnet
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=$REDIS_PASSWORD
REDIS_DATA_DIR=$AITOS_DATA_ROOT/eventbus/redis
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=$CLICKHOUSE_PASSWORD
CLICKHOUSE_DATABASE=aitos
CLICKHOUSE_DB=aitos
CLICKHOUSE_DATA_DIR=$AITOS_DATA_ROOT/databases/clickhouse
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=$NEO4J_PASSWORD
NEO4J_DATA_DIR=$AITOS_DATA_ROOT/databases/neo4j
NEO4J_LOG_DIR=$AITOS_DATA_ROOT/runtime/logs/neo4j
LEARNING_POLL_SECONDS=60
LEARNING_LOOKBACK_HOURS=168
LEARNING_MODEL_PATH=$AITOS_DATA_ROOT/runtime/models/online_rl/deep_value.pkl
LEARNING_STATE_PATH=$AITOS_DATA_ROOT/runtime/models/online_rl/worker_state.json
BACKTEST_DATA_DIR=$AITOS_DATA_ROOT/research/backtest
REPLAY_DATA_DIR=$AITOS_DATA_ROOT/research/replay
BACKUP_DIR=$AITOS_DATA_ROOT/artifacts/backups
SNAPSHOT_DIR=$AITOS_DATA_ROOT/artifacts/snapshots
MODEL_DATA_DIR=$AITOS_DATA_ROOT/runtime/models
RUNTIME_TMP_DIR=$AITOS_DATA_ROOT/runtime/tmp
ARTIFACTS_DATA_DIR=$AITOS_DATA_ROOT/artifacts
AITOS_BOOT_RUNTIME_DIR=/var/lib/aitos
BOOT_FREE_BUFFER_GB=7.5
DATA_DISK_MIN_FREE_GB=$DATA_DISK_MIN_FREE_GB
DATA_DISK_TARGET_FREE_GB=25
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=false
BINANCE_RECV_WINDOW_MS=5000
BINANCE_HEDGE_MODE=false
APP_PORT=8001
APP_HOST=0.0.0.0
COMPOSE_PROJECT_NAME=aitos-staging
AITOS_IMAGE=ghcr.io/${REPO,,}
AITOS_IMAGE_TAG=staging-sha-${COMMIT}
AITOS_RUNTIME_IMAGE=$AITOS_RUNTIME_IMAGE
DATABASE_URL=
SENTRY_DSN=
ENVEOF
chmod 600 .env

echo '=== STAGING PATH ASSERTIONS ==='
grep -q '^REDIS_DATA_DIR=/mnt/aitos-staging-data/eventbus/redis$' .env
grep -q '^CLICKHOUSE_DATA_DIR=/mnt/aitos-staging-data/databases/clickhouse$' .env
grep -q '^NEO4J_DATA_DIR=/mnt/aitos-staging-data/databases/neo4j$' .env
! grep -q '^AITOS_DATA_ROOT=/mnt/aitos-data$' .env
! grep -q '^COMPOSE_PROJECT_NAME=aitos$' .env

echo '=== STAGING DOCKER DEPLOYMENT ==='
docker compose config --format json > /tmp/aitos-staging-compose.json
AITOS_REQUIRE_DIGEST_IMAGES=true python3 scripts/validate_compose_policy.py /tmp/aitos-staging-compose.json
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username "$GHCR_USERNAME" --password-stdin
trap 'docker logout ghcr.io >/dev/null 2>&1 || true; unset GHCR_TOKEN' EXIT
pull_attempt=1
until docker compose pull; do
  if [ "$pull_attempt" -ge 5 ]; then
    echo "GHCR pull failed after $pull_attempt attempts."
    exit 1
  fi
  sleep_seconds=$((pull_attempt * 15))
  echo "GHCR pull interrupted; retrying in $sleep_seconds seconds (attempt $((pull_attempt + 1))/5)."
  sleep "$sleep_seconds"
  pull_attempt=$((pull_attempt + 1))
done
docker logout ghcr.io || true
trap - EXIT
unset GHCR_TOKEN

# Decide whether the one-time named-volume migration is safe BEFORE
# stopping the current stack. A populated bind target is adoptable
# only when its ClickHouse UUID matches the legacy named volume.
mkdir -p "$AITOS_DATA_ROOT/artifacts/backups" "$AITOS_DATA_ROOT/databases/clickhouse"
CLICKHOUSE_MIGRATION_MARKER="$AITOS_DATA_ROOT/artifacts/backups/.clickhouse-named-volume-migrated"
CLICKHOUSE_MIGRATION_MODE=none
if docker volume inspect aitos-staging_clickhouse-data >/dev/null 2>&1 && [ ! -f "$CLICKHOUSE_MIGRATION_MARKER" ]; then
  if find "$AITOS_DATA_ROOT/databases/clickhouse" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    named_uuid="$(docker run --rm --user 0:0 -v aitos-staging_clickhouse-data:/source:ro alpine:3.20 sh -ec 'cat /source/uuid')"
    bind_uuid="$(docker run --rm --user 0:0 -v "$AITOS_DATA_ROOT/databases/clickhouse:/source:ro" alpine:3.20 sh -ec 'cat /source/uuid')"
    [ -n "$named_uuid" ] && [ "$named_uuid" = "$bind_uuid" ] || {
      echo "::error::ClickHouse bind target is populated but its UUID ($bind_uuid) does not match legacy named volume ($named_uuid); refusing deployment before stopping services."
      exit 1
    }
    CLICKHOUSE_MIGRATION_MODE=adopt-existing-bind
    echo "ClickHouse bind target already belongs to the same database lineage (UUID=$bind_uuid); preserve it and do not copy the older named volume."
  else
    CLICKHOUSE_MIGRATION_MODE=copy-named-volume
  fi
fi

docker compose down --remove-orphans --timeout 30
if docker inspect aitos-clickhouse --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
  echo '::error::ClickHouse is still running; refusing to snapshot or migrate its data.'
  exit 1
fi

if [ "$CLICKHOUSE_MIGRATION_MODE" = adopt-existing-bind ]; then
  clickhouse_backup="clickhouse-bind-adopted-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  docker run --rm --user 0:0 \
    -v "$AITOS_DATA_ROOT/databases/clickhouse:/source:ro" \
    -v "$AITOS_DATA_ROOT/artifacts/backups:/backup" \
    alpine:3.20 sh -ec 'tar -czf "/backup/$1" -C /source . && tar -tzf "/backup/$1" >/dev/null' sh "$clickhouse_backup"
  sha256sum "$AITOS_DATA_ROOT/artifacts/backups/$clickhouse_backup" > "$AITOS_DATA_ROOT/artifacts/backups/$clickhouse_backup.sha256"
  test -s "$AITOS_DATA_ROOT/artifacts/backups/$clickhouse_backup"
  marker_tmp="$CLICKHOUSE_MIGRATION_MARKER.tmp.$"
  {
    echo "status=verified-existing-bind-dataset"
    echo "named_volume=aitos-staging_clickhouse-data"
    echo "clickhouse_uuid=$bind_uuid"
    echo "bind_mount=$AITOS_DATA_ROOT/databases/clickhouse"
    echo "backup=$clickhouse_backup"
    echo "decision=preserve-existing-bind-data;do-not-copy-named-volume"
    echo "verified_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$marker_tmp"
  mv "$marker_tmp" "$CLICKHOUSE_MIGRATION_MARKER"
  echo "Existing ClickHouse bind dataset retained; verified backup=$clickhouse_backup"
elif [ "$CLICKHOUSE_MIGRATION_MODE" = copy-named-volume ]; then
  clickhouse_backup="clickhouse-named-volume-pre-bind-mount-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  docker run --rm --user 0:0 \
    -v aitos-staging_clickhouse-data:/source:ro \
    -v "$AITOS_DATA_ROOT/artifacts/backups:/backup" \
    alpine:3.20 sh -ec 'tar -czf "/backup/$1" -C /source . && tar -tzf "/backup/$1" >/dev/null' sh "$clickhouse_backup"
  sha256sum "$AITOS_DATA_ROOT/artifacts/backups/$clickhouse_backup" > "$AITOS_DATA_ROOT/artifacts/backups/$clickhouse_backup.sha256"
  docker run --rm --user 0:0 \
    -v aitos-staging_clickhouse-data:/source:ro \
    -v "$AITOS_DATA_ROOT/databases/clickhouse:/target" \
    alpine:3.20 sh -ec 'cp -a /source/. /target/ && chown -R 101:101 /target'
  named_uuid="$(docker run --rm --user 0:0 -v aitos-staging_clickhouse-data:/source:ro alpine:3.20 sh -ec 'cat /source/uuid')"
  bind_uuid="$(docker run --rm --user 0:0 -v "$AITOS_DATA_ROOT/databases/clickhouse:/source:ro" alpine:3.20 sh -ec 'cat /source/uuid')"
  [ -n "$named_uuid" ] && [ "$named_uuid" = "$bind_uuid" ] || { echo '::error::ClickHouse UUID verification failed after named-volume copy.'; exit 1; }
  marker_tmp="$CLICKHOUSE_MIGRATION_MARKER.tmp.$"
  {
    echo "status=migrated-from-named-volume"
    echo "named_volume=aitos-staging_clickhouse-data"
    echo "clickhouse_uuid=$bind_uuid"
    echo "backup=$clickhouse_backup"
    echo "verified_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$marker_tmp"
  mv "$marker_tmp" "$CLICKHOUSE_MIGRATION_MARKER"
  echo "ClickHouse named-volume data copied and UUID-verified; backup=$clickhouse_backup"
fi

docker compose up -d --remove-orphans --force-recreate redis clickhouse neo4j clickhouse-init redis-dlq-retention redis-stream-archive

echo '=== STAGING HEALTH GATE ==='
deadline=$((SECONDS + 900))
while [ "$SECONDS" -lt "$deadline" ]; do
  redis_state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' aitos-redis 2>/dev/null || true)
  clickhouse_state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' aitos-clickhouse 2>/dev/null || true)
  neo4j_state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' aitos-neo4j 2>/dev/null || true)
  if [ "$redis_state" = healthy ] && [ "$clickhouse_state" = healthy ] && [ "$neo4j_state" = healthy ]; then break; fi
  sleep 10
done
for container in aitos-redis aitos-clickhouse aitos-neo4j; do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || echo missing)"
  if [ "$state" != healthy ]; then
    echo "::error::$container failed readiness gate: $state"
    docker inspect --format '{{json .State.Health}}' "$container" 2>&1 || true
    docker logs --tail 250 --timestamps "$container" 2>&1 || true
    exit 1
  fi
done
init_deadline=$((SECONDS + 240))
while :; do
  init_id="$(docker compose ps --all -q clickhouse-init)"
  if [ -n "$init_id" ]; then
    init_state="$(docker inspect --format '{{.State.Status}}' "$init_id")"
    if [ "$init_state" = exited ]; then
      init_exit="$(docker inspect --format '{{.State.ExitCode}}' "$init_id")"
      if [ "$init_exit" != 0 ]; then
        docker logs --tail 200 "$init_id" 2>&1 || true
        echo "ClickHouse schema initialization failed with exit code $init_exit."
        exit 1
      fi
      break
    fi
  fi
  if [ "$SECONDS" -ge "$init_deadline" ]; then
    echo "ClickHouse schema initialization did not complete within 240 seconds."
    exit 1
  fi
  sleep 5
done
echo "ClickHouse base schema initialization: PASS"

echo '=== STAGING SCOPED CLICKHOUSE STATE MIGRATION ==='
# Quiesce all application writers before the one-time schema cutover.
docker compose stop aitos-paper aitos-learning aitos-storage-maintenance || true
clickhouse_cmd() {
  docker exec -e CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" aitos-clickhouse \
    clickhouse-client --user default --password "$CLICKHOUSE_PASSWORD" \
    --database aitos "$@"
}
# Ensure the learning worker's keyset cursor column exists before it starts.
clickhouse_cmd --query "ALTER TABLE learning_experiences ADD COLUMN IF NOT EXISTS ingested_at DateTime64(3, 'UTC') DEFAULT timestamp"
scoped_columns="$(clickhouse_cmd --query "SELECT count() FROM system.columns WHERE database='aitos' AND ((table='trade_runtime_state' AND name IN ('account_scope','trade_id','symbol','state','payload_json','legacy_payload_json','updated_at','source_fingerprint','source_occurrence','payload_sha256')) OR (table='paper_closed_trade_ledger' AND name IN ('account_scope','trade_id','payload_json','legacy_payload_json','updated_at','source_fingerprint','source_occurrence','payload_sha256')))")"
scoped_engines="$(clickhouse_cmd --query "SELECT count() FROM system.tables WHERE database='aitos' AND name IN ('trade_runtime_state','paper_closed_trade_ledger') AND engine='MergeTree' AND (startsWith(sorting_key, '(account_scope') OR startsWith(sorting_key, 'account_scope'))")"
if [ "$scoped_columns" = 18 ] && [ "$scoped_engines" = 2 ]; then
  echo 'Scoped ClickHouse state schema already verified; migration skipped.'
else
  partial_state="$(clickhouse_cmd --query "SELECT count() FROM system.tables WHERE database='aitos' AND name IN ('trade_runtime_state_v2','paper_closed_trade_ledger_v2','trade_runtime_state_legacy_pre_scope_v1','paper_closed_trade_ledger_legacy_pre_scope_v1')")"
  if [ "$partial_state" != 0 ]; then
    echo 'Refusing automatic migration: partial v2 tables or retained backups already exist.'
    exit 1
  fi
  # The staging DB may be fresh and have no closed-trade rows yet.
  # Create the empty legacy source so the verified migration can cut over both tables.
  clickhouse_cmd --query "CREATE TABLE IF NOT EXISTS trade_runtime_state (trade_id String, symbol LowCardinality(String), state LowCardinality(String), payload_json String, updated_at DateTime64(3, 'UTC')) ENGINE = ReplacingMergeTree(updated_at) ORDER BY trade_id"
  clickhouse_cmd --query "CREATE TABLE IF NOT EXISTS paper_closed_trade_ledger (trade_id String, payload_json String, updated_at DateTime64(3, 'UTC')) ENGINE = ReplacingMergeTree(updated_at) ORDER BY trade_id"
  runtime_rows="$(clickhouse_cmd --query 'SELECT count() FROM trade_runtime_state')"
  closed_rows="$(clickhouse_cmd --query 'SELECT count() FROM paper_closed_trade_ledger')"
  echo "Legacy state rows: runtime=$runtime_rows closed=$closed_rows"
  # Stop ClickHouse cleanly and retain a complete filesystem backup of its named volume.
  docker compose stop clickhouse
  backup_name="clickhouse-pre-scope-cutover-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  docker run --rm --user 0:0 \
    -v "$AITOS_DATA_ROOT/databases/clickhouse:/data:ro" \
    -v "$AITOS_DATA_ROOT/artifacts/backups:/backup" \
    alpine:3.20 sh -ec 'tar -czf "/backup/$1" -C /data . && tar -tzf "/backup/$1" >/dev/null' sh "$backup_name"
  sha256sum "$AITOS_DATA_ROOT/artifacts/backups/$backup_name" > "$AITOS_DATA_ROOT/artifacts/backups/$backup_name.sha256"
  test -s "$AITOS_DATA_ROOT/artifacts/backups/$backup_name"
  echo "ClickHouse volume backup retained: $AITOS_DATA_ROOT/artifacts/backups/$backup_name"
  docker compose start clickhouse
  clickhouse_deadline=$((SECONDS + 240))
  until [ "$(docker inspect --format '{{.State.Health.Status}}' aitos-clickhouse 2>/dev/null || true)" = healthy ]; do
    if [ "$SECONDS" -ge "$clickhouse_deadline" ]; then
      echo 'ClickHouse did not return healthy after backup.'
      docker logs --tail 200 aitos-clickhouse 2>&1 || true
      exit 1
    fi
    sleep 5
  done
  manifest_name="scoped-state-attribution-manifest-$(date -u +%Y%m%dT%H%M%SZ).json"
  docker compose run --rm --no-deps \
    -v "$AITOS_DATA_ROOT/artifacts/backups:/backup" \
    --entrypoint python3 aitos-paper \
    /app/scripts/migrate_scoped_clickhouse_state.py \
    --apply --cutover --confirm-writers-stopped --confirm-backup-retained \
    --manifest-out "/backup/$manifest_name"
  scoped_columns_after="$(clickhouse_cmd --query "SELECT count() FROM system.columns WHERE database='aitos' AND ((table='trade_runtime_state' AND name IN ('account_scope','trade_id','symbol','state','payload_json','legacy_payload_json','updated_at','source_fingerprint','source_occurrence','payload_sha256')) OR (table='paper_closed_trade_ledger' AND name IN ('account_scope','trade_id','payload_json','legacy_payload_json','updated_at','source_fingerprint','source_occurrence','payload_sha256')))")"
  scoped_engines_after="$(clickhouse_cmd --query "SELECT count() FROM system.tables WHERE database='aitos' AND name IN ('trade_runtime_state','paper_closed_trade_ledger') AND engine='MergeTree' AND (startsWith(sorting_key, '(account_scope') OR startsWith(sorting_key, 'account_scope'))")"
  [ "$scoped_columns_after" = 18 ] && [ "$scoped_engines_after" = 2 ]
  echo 'Scoped ClickHouse state migration and post-cutover verification PASSED.'
fi
docker compose up -d aitos-paper aitos-learning aitos-storage-maintenance

bad_containers="$(docker compose ps --all --format '{{.Name}} {{.State}}' | awk '{state=tolower($2); if (state ~ /unhealthy|restarting/ || (state ~ /exited/ && $1 !~ /clickhouse-init/)) print}')"
if [ -n "$bad_containers" ]; then printf '%s\n' "$bad_containers"; exit 1; fi

echo '=== STAGING STORAGE GATE ==='
DATA_UUID="$(findmnt -rn -o UUID --target "$AITOS_DATA_ROOT" | head -n1)"
ROOT_UUID="$(findmnt -rn -o UUID --target / | head -n1)"
if [ -n "$AITOS_DATA_DISK_UUID" ]; then
  [ "$DATA_UUID" = "$AITOS_DATA_DISK_UUID" ] || { echo "::error::Final storage gate expected external UUID $AITOS_DATA_DISK_UUID, found $DATA_UUID."; exit 1; }
  [ "$DATA_UUID" != "$ROOT_UUID" ] || { echo '::error::Final storage gate detected external UUID on the OS root filesystem.'; exit 1; }
else
  [ "$DATA_UUID" = "$ROOT_UUID" ] || { echo "::error::Final storage gate expected OS root UUID $ROOT_UUID, found $DATA_UUID."; exit 1; }
fi
df -h "$AITOS_DATA_ROOT"
docker run --rm --user 0:0 -v "$AITOS_DATA_ROOT:/data" alpine:3.20 sh -ec '
  for path in databases/clickhouse databases/neo4j eventbus/redis/live eventbus/redis/archive research/backtest research/replay artifacts/backups artifacts/snapshots runtime/models runtime/logs/neo4j runtime/tmp; do
    test -d "/data/$path" || { echo "Missing persistent path: /data/$path"; exit 1; }
  done
'
echo "=== STAGING DEPLOYMENT SUCCESS: $COMMIT ==="

