#!/usr/bin/env bash
set -euo pipefail

# AITOS host storage bootstrap. The data disk is the single home for durable
# trading state. The boot disk is reserved for OS/Docker/deployment state.
# This script is fail-closed: it never falls back to the boot disk.

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
CLICKHOUSE_UID="${CLICKHOUSE_UID:-101}"
CLICKHOUSE_GID="${CLICKHOUSE_GID:-101}"
REDIS_UID="${REDIS_UID:-999}"
REDIS_GID="${REDIS_GID:-999}"
NEO4J_UID="${NEO4J_UID:-7474}"
NEO4J_GID="${NEO4J_GID:-7474}"

log() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -n "$DISK_UUID" ]] || die "AITOS_DATA_DISK_UUID is required; refusing to risk writing database data to the boot disk."
[[ "$DATA_ROOT" = /* ]] || die "AITOS_DATA_ROOT must be an absolute path."
[[ "$HOST_UID" =~ ^[0-9]+$ && "$HOST_GID" =~ ^[0-9]+$ ]] || die "AITOS_HOST_UID/GID must be numeric."
[[ "$CLICKHOUSE_UID" =~ ^[0-9]+$ && "$CLICKHOUSE_GID" =~ ^[0-9]+$ ]] || die "ClickHouse UID/GID must be numeric."
[[ "$REDIS_UID" =~ ^[0-9]+$ && "$REDIS_GID" =~ ^[0-9]+$ ]] || die "Redis UID/GID must be numeric."
[[ "$NEO4J_UID" =~ ^[0-9]+$ && "$NEO4J_GID" =~ ^[0-9]+$ ]] || die "Neo4j UID/GID must be numeric."

SUDO=(sudo)
if [[ "$(id -u)" -eq 0 ]]; then SUDO=(); fi
for command_name in findmnt mountpoint readlink lsblk; do
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."
done

UUID_LINK="/dev/disk/by-uuid/$DISK_UUID"
[[ -e "$UUID_LINK" ]] || die "Configured disk UUID was not found: $DISK_UUID"
DEVICE="$(readlink -f "$UUID_LINK")"
[[ -b "$DEVICE" ]] || die "Resolved UUID is not a block device: $DEVICE"
FSTYPE="$(lsblk -no FSTYPE "$DEVICE" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
if [[ -z "$FSTYPE" ]] && command -v blkid >/dev/null 2>&1; then
  FSTYPE="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"
fi
[[ -n "$FSTYPE" ]] || die "No filesystem detected on $DEVICE; refusing to mount an unknown filesystem."

log "Data disk preflight"
echo "UUID: $DISK_UUID"
echo "Device: $DEVICE"
echo "Filesystem: $FSTYPE"
echo "Mount: $DATA_ROOT"
${SUDO[@]} mkdir -p "$DATA_ROOT"

if mountpoint -q "$DATA_ROOT"; then
  CURRENT_SOURCE="$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)"
  CURRENT_DEVICE="$(readlink -f "$CURRENT_SOURCE" 2>/dev/null || true)"
  [[ "$CURRENT_DEVICE" == "$DEVICE" ]] || die "$DATA_ROOT is already mounted from $CURRENT_SOURCE, not configured UUID $DISK_UUID."
else
  log "Mounting data disk"
  ${SUDO[@]} mount -t "$FSTYPE" "$DEVICE" "$DATA_ROOT"
fi
mountpoint -q "$DATA_ROOT" || die "Data disk did not become a mount point: $DATA_ROOT"
MOUNT_SOURCE="$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)"
MOUNT_DEVICE="$(readlink -f "$MOUNT_SOURCE" 2>/dev/null || true)"
[[ "$MOUNT_DEVICE" == "$DEVICE" ]] || die "Mounted source verification failed: expected $DEVICE, got $MOUNT_SOURCE"

log "Persisting mount in /etc/fstab"
FSTAB_LINE="UUID=$DISK_UUID $DATA_ROOT $FSTYPE defaults,nofail,x-systemd.device-timeout=30s 0 2"
if ! ${SUDO[@]} grep -Eq "^[[:space:]]*UUID=${DISK_UUID}[[:space:]]+" /etc/fstab; then
  printf '%s\n' "$FSTAB_LINE" | ${SUDO[@]} tee -a /etc/fstab >/dev/null
fi

log "Creating canonical AITOS data layout"
${SUDO[@]} mkdir -p \
  "$DATA_ROOT/databases/clickhouse" \
  "$DATA_ROOT/databases/neo4j" \
  "$DATA_ROOT/eventbus/redis/live" \
  "$DATA_ROOT/eventbus/redis/archive" \
  "$DATA_ROOT/research/backtest" \
  "$DATA_ROOT/research/replay" \
  "$DATA_ROOT/artifacts/backups" \
  "$DATA_ROOT/artifacts/snapshots" \
  "$DATA_ROOT/runtime/models" \
  "$DATA_ROOT/runtime/logs/neo4j" \
  "$DATA_ROOT/runtime/tmp"

# Compatibility paths for the current deployment environment. These are links,
# not duplicate data stores, and can be removed after all env vars are canonical.
for pair in \
  "clickhouse:databases/clickhouse" \
  "neo4j:databases/neo4j" \
  "redis:eventbus/redis"; do
  legacy_name="${pair%%:*}"
  canonical_rel="${pair#*:}"
  legacy_path="$DATA_ROOT/$legacy_name"
  if [[ -e "$legacy_path" && ! -L "$legacy_path" ]]; then
    die "Legacy storage path exists as a real directory: $legacy_path; explicit migration required."
  fi
  if [[ -L "$legacy_path" ]]; then
    [[ "$(readlink "$legacy_path")" == "$canonical_rel" ]] || die "Unexpected legacy symlink target: $legacy_path"
  else
    ${SUDO[@]} ln -s "$canonical_rel" "$legacy_path"
  fi
done

# Keep only a tiny compatibility surface on the deployment checkout. Large
# backtest/backup/snapshot/log payloads physically live on the data disk.
STORAGE_DIR="$REPO_ROOT/.storage"
${SUDO[@]} mkdir -p "$STORAGE_DIR"
for pair in \
  "backtest:$DATA_ROOT/research/backtest" \
  "backups:$DATA_ROOT/artifacts/backups" \
  "snapshots:$DATA_ROOT/artifacts/snapshots" \
  "neo4j-logs:$DATA_ROOT/runtime/logs/neo4j"; do
  name="${pair%%:*}"
  target="${pair#*:}"
  link="$STORAGE_DIR/$name"
  if [[ -e "$link" && ! -L "$link" ]]; then
    die "Deployment storage compatibility path exists as a real directory: $link; explicit migration required."
  fi
  if [[ -L "$link" ]]; then
    [[ "$(readlink "$link")" == "$target" ]] || die "Unexpected storage link target: $link"
  else
    ${SUDO[@]} ln -s "$target" "$link"
  fi
done

${SUDO[@]} chown "$HOST_UID:$HOST_GID" "$DATA_ROOT"
${SUDO[@]} chown -R "$CLICKHOUSE_UID:$CLICKHOUSE_GID" "$DATA_ROOT/databases/clickhouse"
${SUDO[@]} chown -R "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/databases/neo4j"
${SUDO[@]} chown -R "$REDIS_UID:$REDIS_GID" "$DATA_ROOT/eventbus/redis"
${SUDO[@]} chown -R "$HOST_UID:$HOST_GID" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
${SUDO[@]} chmod 0755 "$DATA_ROOT" "$DATA_ROOT/databases" "$DATA_ROOT/eventbus" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
${SUDO[@]} chmod 0750 "$DATA_ROOT/eventbus/redis" "$DATA_ROOT/eventbus/redis/live" "$DATA_ROOT/eventbus/redis/archive"

log "Storage verification"
df -h "$DATA_ROOT"
findmnt --target "$DATA_ROOT"
for required in \
  databases/clickhouse databases/neo4j \
  eventbus/redis/live eventbus/redis/archive \
  research/backtest research/replay \
  artifacts/backups artifacts/snapshots \
  runtime/models runtime/logs/neo4j runtime/tmp; do
  [[ -d "$DATA_ROOT/$required" ]] || die "Missing required directory: $DATA_ROOT/$required"
done
for link in clickhouse neo4j redis; do
  [[ -L "$DATA_ROOT/$link" ]] || die "Missing required compatibility symlink: $DATA_ROOT/$link"
done
for link in backtest backups snapshots neo4j-logs; do
  [[ -L "$STORAGE_DIR/$link" ]] || die "Missing deployment storage symlink: $STORAGE_DIR/$link"
done

TMP_FILE="$DATA_ROOT/.aitos-storage-write-test"
${SUDO[@]} touch "$TMP_FILE"
${SUDO[@]} rm -f "$TMP_FILE"

echo "AITOS canonical storage bootstrap completed successfully."
echo "Persistent data root: $DATA_ROOT"
echo "Canonical layout: databases / eventbus / research / artifacts / runtime"
echo "Configured disk UUID: $DISK_UUID"
