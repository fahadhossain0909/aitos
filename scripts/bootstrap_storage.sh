#!/usr/bin/env bash
set -euo pipefail

# AITOS storage bootstrap.
# If AITOS_DATA_DISK_UUID is set, use that external filesystem. If empty,
# durable data stays on AITOS_DATA_ROOT on the operating-system root filesystem.
# The selected filesystem is always verified; no silent fallback is permitted.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${AITOS_ENV_FILE:-$REPO_ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
DISK_UUID="${AITOS_DATA_DISK_UUID:-}"
HOST_UID="${AITOS_HOST_UID:-$(id -u)}"
HOST_GID="${AITOS_HOST_GID:-$(id -g)}"
AITOS_UID="${AITOS_UID:-1000}"
AITOS_GID="${AITOS_GID:-1000}"
CLICKHOUSE_UID="${CLICKHOUSE_UID:-101}"
CLICKHOUSE_GID="${CLICKHOUSE_GID:-101}"
REDIS_UID="${REDIS_UID:-999}"
REDIS_GID="${REDIS_GID:-999}"
NEO4J_UID="${NEO4J_UID:-7474}"
NEO4J_GID="${NEO4J_GID:-7474}"
log() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$DATA_ROOT" = /* ]] || die "AITOS_DATA_ROOT must be absolute."
[[ "$HOST_UID" =~ ^[0-9]+$ && "$HOST_GID" =~ ^[0-9]+$ ]] || die "AITOS_HOST_UID/GID must be numeric."
[[ "$AITOS_UID" =~ ^[0-9]+$ && "$AITOS_GID" =~ ^[0-9]+$ ]] || die "AITOS_UID/GID must be numeric."
[[ "$CLICKHOUSE_UID" =~ ^[0-9]+$ && "$CLICKHOUSE_GID" =~ ^[0-9]+$ ]] || die "ClickHouse UID/GID must be numeric."
[[ "$REDIS_UID" =~ ^[0-9]+$ && "$REDIS_GID" =~ ^[0-9]+$ ]] || die "Redis UID/GID must be numeric."
[[ "$NEO4J_UID" =~ ^[0-9]+$ && "$NEO4J_GID" =~ ^[0-9]+$ ]] || die "NEO4J_UID/GID must be numeric."
for command_name in findmnt mountpoint readlink lsblk; do command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."; done

ROOT_UUID="$(findmnt -rn -o UUID --target / | head -n1)"
ROOT_SOURCE="$(readlink -f "$(findmnt -rn -o SOURCE --target / | head -n1)")"
[[ -n "$ROOT_UUID" && -n "$ROOT_SOURCE" ]] || die "Unable to identify the operating system root filesystem."

# Prefer non-interactive sudo. If the host requires a password, consume the
# optional GitHub Environment secret over stdin only; never persist or log it.
ROOT_DISK_MODE=false
run_sudo() {
  if [[ "$(id -u)" -eq 0 || "$ROOT_DISK_MODE" == "true" ]]; then
    "$@"
  elif sudo -n -v >/dev/null 2>&1; then
    sudo -n -- "$@"
  elif [[ -n "${AITOS_SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "$AITOS_SUDO_PASSWORD" | sudo -S -p '' -- "$@"
  else
    die "This operation requires sudo. Configure the optional GitHub Environment secret AITOS_SUDO_PASSWORD, or grant the deployment user the required passwordless sudo permissions."
  fi
}

if [[ -z "$DISK_UUID" ]]; then
  log "Using operating-system root filesystem (no external data UUID configured)"
  if ! mkdir -p "$DATA_ROOT" 2>/dev/null; then run_sudo mkdir -p "$DATA_ROOT"; fi
  DATA_UUID="$(findmnt -rn -o UUID --target "$DATA_ROOT" | head -n1)"
  DATA_SOURCE="$(readlink -f "$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)")"
  [[ "$DATA_UUID" == "$ROOT_UUID" && "$DATA_SOURCE" == "$ROOT_SOURCE" ]] || die "AITOS_DATA_ROOT is not on the operating-system root filesystem. Refusing to silently use another mount."
  ROOT_DISK_MODE=true
  echo "Root filesystem UUID: $ROOT_UUID"
  echo "Data directory: $DATA_ROOT"
  # A short-lived root helper applies container UID/GID ownership without
  # requiring host sudo or changing ownership of existing database children.
  docker run --rm --user 0:0 -v "$DATA_ROOT:/data" \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    alpine:3.20 sh -ec '
      chown "$HOST_UID:$HOST_GID" /data
      mkdir -p /data/databases/clickhouse /data/databases/neo4j \
        /data/eventbus/redis/live /data/eventbus/redis/archive \
        /data/research/backtest /data/research/replay \
        /data/artifacts/backups /data/artifacts/snapshots \
        /data/runtime/models /data/runtime/logs/neo4j /data/runtime/tmp
      for directory in databases eventbus research artifacts runtime; do
        chown "$HOST_UID:$HOST_GID" "/data/$directory"
      done
    '
else
  ROOT_DISK_MODE=false
  [[ "$DISK_UUID" != "$ROOT_UUID" ]] || die "AITOS_DATA_DISK_UUID points to the operating-system root filesystem. Leave it empty for root-filesystem storage or configure a different external UUID."
  UUID_LINK="/dev/disk/by-uuid/$DISK_UUID"
  [[ -e "$UUID_LINK" ]] || die "Configured external data UUID was not found: $DISK_UUID"
  DEVICE="$(readlink -f "$UUID_LINK")"
  [[ -b "$DEVICE" ]] || die "Resolved UUID is not a block device: $DEVICE"
  FSTYPE="$(lsblk -no FSTYPE "$DEVICE" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
  if [[ -z "$FSTYPE" ]] && command -v blkid >/dev/null 2>&1; then FSTYPE="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"; fi
  [[ -n "$FSTYPE" ]] || die "No filesystem detected on $DEVICE."
  log "Using explicitly configured external data filesystem"
  echo "UUID: $DISK_UUID"; echo "Device: $DEVICE"; echo "Filesystem: $FSTYPE"; echo "Mount: $DATA_ROOT"
  run_sudo mkdir -p "$DATA_ROOT"
  if mountpoint -q "$DATA_ROOT"; then
    CURRENT_UUID="$(findmnt -rn -o UUID --target "$DATA_ROOT" | head -n1)"
    [[ "$CURRENT_UUID" == "$DISK_UUID" ]] || die "$DATA_ROOT is mounted from UUID $CURRENT_UUID, not configured UUID $DISK_UUID."
  else
    if find "$DATA_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
      die "Unmounted data root contains existing files; back them up and migrate deliberately before mounting the external filesystem."
    fi
    run_sudo mount -t "$FSTYPE" "$DEVICE" "$DATA_ROOT"
  fi
  mountpoint -q "$DATA_ROOT" || die "External data filesystem did not become a mount point."
  MOUNT_UUID="$(findmnt -rn -o UUID --target "$DATA_ROOT" | head -n1)"
  [[ "$MOUNT_UUID" == "$DISK_UUID" ]] || die "Mounted UUID $MOUNT_UUID does not match configured external UUID $DISK_UUID."
  log "Persisting external mount in /etc/fstab"
  FSTAB_LINE="UUID=$DISK_UUID $DATA_ROOT $FSTYPE defaults,nofail,x-systemd.device-timeout=30s 0 2"
  if ! grep -Eq "^[[:space:]]*UUID=$DISK_UUID[[:space:]]+" /etc/fstab; then
    run_sudo sh -c 'printf "%s\n" "$1" >> /etc/fstab' _ "$FSTAB_LINE"
  fi
fi

MIN_FREE_GB="${DATA_DISK_MIN_FREE_GB:-20}"
FREE_GB="$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
if [[ "$FREE_GB" =~ ^[0-9]+$ && "$FREE_GB" -lt "$MIN_FREE_GB" ]]; then
  log "Emergency database recovery"
  echo "Data disk free: ${FREE_GB}G; configured minimum: ${MIN_FREE_GB}G"
  if [[ "${AITOS_AUTO_RESET_DISPOSABLE_DATA:-true}" != "true" ]]; then
    die "Data disk is below the configured free-space floor and automatic disposable-data reset is disabled."
  fi
  test -f "$REPO_ROOT/scripts/reset_market_data_v1.sh" || die "Disposable database reset script is missing."
  AITOS_CONFIRM_RESET=YES bash "$REPO_ROOT/scripts/reset_market_data_v1.sh"
  FREE_GB="$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
  echo "Data disk free after emergency recovery: ${FREE_GB}G"
fi

has_entries() {
  if [[ "$ROOT_DISK_MODE" == "true" && "$1" == "$DATA_ROOT/"* ]]; then
    relative_path="${1#"$DATA_ROOT/"}"
    docker run --rm --user 0:0 -v "$DATA_ROOT:/data" alpine:3.20 sh -ec '
      [ -n "$(find "/data/$1" -mindepth 1 -print -quit 2>/dev/null)" ]
    ' sh "$relative_path"
  else
    [[ -n "$(run_sudo find "$1" -mindepth 1 -print -quit 2>/dev/null)" ]]
  fi
}

for legacy in clickhouse neo4j redis; do
  legacy_path="$DATA_ROOT/$legacy"
  if [[ -d "$legacy_path" && ! -L "$legacy_path" ]]; then
    if has_entries "$legacy_path"; then die "Legacy path contains data: $legacy_path; migrate explicitly before deployment."; fi
    run_sudo rmdir -- "$legacy_path" || die "Legacy path changed during inspection; refusing to remove: $legacy_path"
  elif [[ -L "$legacy_path" ]]; then
    run_sudo rm -f -- "$legacy_path"
  fi
done
STORAGE_DIR="$REPO_ROOT/.storage"
for legacy in others; do
  path="$STORAGE_DIR/$legacy"
  if [[ -d "$path" && ! -L "$path" ]]; then
    if has_entries "$path"; then die "Legacy checkout storage contains data: $path; migrate explicitly."; fi
    run_sudo rmdir -- "$path" || die "Legacy checkout storage changed during inspection; refusing to remove: $path"
  elif [[ -L "$path" ]]; then run_sudo rm -f -- "$path"; fi
done

log "Creating canonical AITOS data layout"
if [[ "$ROOT_DISK_MODE" == "true" ]]; then
  docker run --rm --user 0:0 -v "$DATA_ROOT:/data" alpine:3.20 sh -ec '
    mkdir -p /data/databases/clickhouse /data/databases/neo4j \
      /data/eventbus/redis/live /data/eventbus/redis/archive \
      /data/research/backtest /data/research/replay \
      /data/artifacts/backups /data/artifacts/snapshots \
      /data/runtime/models /data/runtime/logs/neo4j /data/runtime/tmp
  '
else
  run_sudo mkdir -p \
    "$DATA_ROOT/databases/clickhouse" "$DATA_ROOT/databases/neo4j" \
    "$DATA_ROOT/eventbus" "$DATA_ROOT/eventbus/redis" \
    "$DATA_ROOT/eventbus/redis/live" "$DATA_ROOT/eventbus/redis/archive" \
    "$DATA_ROOT/research/backtest" "$DATA_ROOT/research/replay" \
    "$DATA_ROOT/artifacts/backups" "$DATA_ROOT/artifacts/snapshots" \
    "$DATA_ROOT/runtime/models" "$DATA_ROOT/runtime/logs/neo4j" "$DATA_ROOT/runtime/tmp"
fi

REQUIRED_DATA_DIRS=(databases/clickhouse databases/neo4j eventbus/redis/live eventbus/redis/archive research/backtest research/replay artifacts/backups artifacts/snapshots runtime/models runtime/logs/neo4j runtime/tmp)
if [[ "$ROOT_DISK_MODE" == "true" ]]; then
  docker run --rm --user 0:0 -v "$DATA_ROOT:/data" alpine:3.20 sh -ec '
    for required in "$@"; do
      if [ ! -d "/data/$required" ]; then
        echo "Missing canonical directory: /data/$required"
        ls -ld /data /data/eventbus /data/eventbus/redis /data/eventbus/redis/live /data/eventbus/redis/archive 2>&1 || true
        find /data/eventbus -maxdepth 3 -print 2>&1 || true
        exit 1
      fi
    done
  ' sh "${REQUIRED_DATA_DIRS[@]}" || die "Canonical storage directory verification failed."
else
  for required in "${REQUIRED_DATA_DIRS[@]}"; do
    run_sudo test -d "$DATA_ROOT/$required" || die "Canonical directory creation failed: $DATA_ROOT/$required"
  done
fi

if [[ "$ROOT_DISK_MODE" == "true" ]]; then
  # Use a short-lived root helper container instead of interactive sudo.
  docker run --rm --user 0:0 -v "$DATA_ROOT:/data" \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -e CLICKHOUSE_UID="$CLICKHOUSE_UID" -e CLICKHOUSE_GID="$CLICKHOUSE_GID" \
    -e NEO4J_UID="$NEO4J_UID" -e NEO4J_GID="$NEO4J_GID" \
    -e REDIS_UID="$REDIS_UID" -e REDIS_GID="$REDIS_GID" \
    -e AITOS_UID="$AITOS_UID" -e AITOS_GID="$AITOS_GID" \
    alpine:3.20 sh -ec '
      chown "$HOST_UID:$HOST_GID" /data
      chown -R "$CLICKHOUSE_UID:$CLICKHOUSE_GID" /data/databases/clickhouse
      chown -R "$NEO4J_UID:$NEO4J_GID" /data/databases/neo4j
      chown -R "$REDIS_UID:$REDIS_GID" /data/eventbus/redis
      chown -R "$AITOS_UID:$AITOS_GID" /data/research /data/artifacts /data/runtime
      chown -R "$NEO4J_UID:$NEO4J_GID" /data/runtime/logs/neo4j
      chmod 0755 /data /data/databases /data/eventbus /data/research /data/artifacts /data/runtime
      chmod 0750 /data/eventbus/redis /data/eventbus/redis/live /data/eventbus/redis/archive
    '
else
  run_sudo chown "$HOST_UID:$HOST_GID" "$DATA_ROOT"
  run_sudo chown -R "$CLICKHOUSE_UID:$CLICKHOUSE_GID" "$DATA_ROOT/databases/clickhouse"
  run_sudo chown -R "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/databases/neo4j"
  run_sudo chown -R "$REDIS_UID:$REDIS_GID" "$DATA_ROOT/eventbus/redis"
  run_sudo chown -R "$AITOS_UID:$AITOS_GID" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
  run_sudo chown -R "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/runtime/logs/neo4j"
  run_sudo chmod 0755 "$DATA_ROOT" "$DATA_ROOT/databases" "$DATA_ROOT/eventbus" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
  run_sudo chmod 0750 "$DATA_ROOT/eventbus/redis" "$DATA_ROOT/eventbus/redis/live" "$DATA_ROOT/eventbus/redis/archive"
fi
log "Storage verification"
df -h "$DATA_ROOT"; findmnt --target "$DATA_ROOT"
if [[ "$ROOT_DISK_MODE" == "true" ]]; then
  docker run --rm --user 0:0 -v "$DATA_ROOT:/data" alpine:3.20 sh -ec '
    for required in "$@"; do test -d "/data/$required" || { echo "Missing required directory: /data/$required"; exit 1; }; done
  ' sh "${REQUIRED_DATA_DIRS[@]}" || die "Required storage directory verification failed."
else
  for required in "${REQUIRED_DATA_DIRS[@]}"; do run_sudo test -d "$DATA_ROOT/$required" || die "Missing required directory: $DATA_ROOT/$required"; done
fi
TMP_FILE="$DATA_ROOT/.aitos-storage-write-test"; run_sudo touch "$TMP_FILE"; run_sudo rm -f "$TMP_FILE"
# Verify the actual application identity can write its persistent runtime path.
AITOS_TEST_FILE="$DATA_ROOT/runtime/models/.aitos-runtime-write-test"
if [[ "$ROOT_DISK_MODE" == "true" ]]; then
  docker run --rm --user "$AITOS_UID:$AITOS_GID" -v "$DATA_ROOT/runtime/models:/models" alpine:3.20 sh -ec 'touch /models/.aitos-runtime-write-test && rm -f /models/.aitos-runtime-write-test' || die "Aitos UID $AITOS_UID cannot write runtime/models"
else
  run_sudo -u "#$AITOS_UID" touch "$AITOS_TEST_FILE" || die "Aitos UID $AITOS_UID cannot write runtime/models"
  run_sudo rm -f "$AITOS_TEST_FILE"
fi
echo "AITOS canonical storage bootstrap completed successfully."
