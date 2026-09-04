#!/usr/bin/env bash
set -euo pipefail

# AITOS host storage bootstrap.
# The data disk is the single home for persistent state. The layout is split by
# responsibility so hot event-bus state cannot be confused with durable DB data,
# research/replay data, or operational artifacts. The script is fail-closed: it
# never falls back to the boot disk.

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
if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
fi

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
echo "UUID:       $DISK_UUID"
echo "Device:     $DEVICE"
echo "Filesystem: $FSTYPE"
echo "Mount:      $DATA_ROOT"

${SUDO[@]} mkdir -p "$DATA_ROOT"

if mountpoint -q "$DATA_ROOT"; then
  CURRENT_SOURCE="$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)"
  CURRENT_DEVICE="$(readlink -f "$CURRENT_SOURCE" 2>/dev/null || true)"
  [[ "$CURRENT_DEVICE" == "$DEVICE" ]] || die "$DATA_ROOT is already mounted from $CURRENT_SOURCE, not configured UUID $DISK_UUID. Refusing to continue."
  echo "Data disk is already mounted from the configured UUID."
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
  echo "Added persistent UUID mount to /etc/fstab."
else
  echo "An fstab entry for UUID $DISK_UUID already exists."
fi

log "Creating canonical AITOS data layout"
# Durable databases: large, queryable state only.
# Event bus: Redis hot state and its append-only archive are isolated here.
# Research: replay/backtest datasets that may grow independently of databases.
# Artifacts: backups/snapshots that must not share ClickHouse/Redis directories.
# Runtime: durable model state that survives container recreation.
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
  "$DATA_ROOT/runtime/tmp"

# Keep the legacy top-level names as symlinks during the architecture migration.
# Compose/env files on older deployments can therefore transition without
# copying or duplicating database bytes. New code should use the canonical paths.
for pair in \
  "clickhouse:databases/clickhouse" \
  "neo4j:databases/neo4j" \
  "redis:eventbus/redis"; do
  legacy_name="${pair%%:*}"
  canonical_rel="${pair#*:}"
  legacy_path="$DATA_ROOT/$legacy_name"
  canonical_path="$DATA_ROOT/$canonical_rel"
  if [[ -e "$legacy_path" && ! -L "$legacy_path" ]]; then
    # Refuse to hide real data behind a symlink. This forces an explicit
    # migration instead of silently moving/overwriting database state.
    die "Legacy storage path exists as a real directory: $legacy_path; migrate it explicitly before enabling canonical symlinks."
  fi
  if [[ -L "$legacy_path" ]]; then
    [[ "$(readlink "$legacy_path")" == "$canonical_rel" ]] || die "Unexpected legacy symlink target: $legacy_path"
  else
    ${SUDO[@]} ln -s "$canonical_rel" "$legacy_path"
  fi
done

# Service-specific ownership. The top-level root stays inspectable by the host
# deployment user; each database/event-bus subtree belongs to its container UID.
${SUDO[@]} chown "$HOST_UID:$HOST_GID" "$DATA_ROOT"
${SUDO[@]} chown -R "$CLICKHOUSE_UID:$CLICKHOUSE_GID" "$DATA_ROOT/databases/clickhouse"
${SUDO[@]} chown -R "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/databases/neo4j"
${SUDO[@]} chown -R "$REDIS_UID:$REDIS_GID" "$DATA_ROOT/eventbus/redis"
${SUDO[@]} chown -R "$HOST_UID:$HOST_GID" \
  "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"

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
  runtime/models runtime/tmp; do
  [[ -d "$DATA_ROOT/$required" ]] || die "Missing required directory: $DATA_ROOT/$required"
done
for link in clickhouse neo4j redis; do
  [[ -L "$DATA_ROOT/$link" ]] || die "Missing compatibility symlink: $DATA_ROOT/$link"
done

touch "$DATA_ROOT/.aitos-storage-write-test" 2>/dev/null || true
if [[ -e "$DATA_ROOT/.aitos-storage-write-test" ]]; then
  rm -f "$DATA_ROOT/.aitos-storage-write-test"
else
  tmp_file="$DATA_ROOT/.aitos-storage-write-test"
  ${SUDO[@]} touch "$tmp_file"
  ${SUDO[@]} rm -f "$tmp_file"
fi

echo "AITOS canonical storage bootstrap completed successfully."
echo "Persistent data root: $DATA_ROOT"
echo "Canonical layout: databases / eventbus / research / artifacts / runtime"
echo "Configured disk UUID: $DISK_UUID"
