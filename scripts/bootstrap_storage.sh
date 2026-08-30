#!/usr/bin/env bash
set -euo pipefail

# AITOS host storage bootstrap.
# Mounts the configured data disk by filesystem UUID, persists the mount,
# creates the database directory layout, and applies container-safe ownership.
# This script is intentionally fail-closed: it never falls back to the boot disk.

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

# These are the UIDs used by the current AITOS compose services. Keep them
# configurable so an image change can be handled without changing this script.
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

command -v findmnt >/dev/null 2>&1 || die "findmnt is required."
command -v blkid >/dev/null 2>&1 || die "blkid is required."
command -v mountpoint >/dev/null 2>&1 || die "mountpoint is required."

UUID_LINK="/dev/disk/by-uuid/$DISK_UUID"
[[ -e "$UUID_LINK" ]] || die "Configured disk UUID was not found: $DISK_UUID"
DEVICE="$(readlink -f "$UUID_LINK")"
[[ -b "$DEVICE" ]] || die "Resolved UUID is not a block device: $DEVICE"

FSTYPE="$(blkid -o value -s TYPE "$DEVICE" || true)"
[[ -n "$FSTYPE" ]] || die "No filesystem detected on $DEVICE; refusing to mount an unknown filesystem."

log "Data disk preflight"
echo "UUID:      $DISK_UUID"
echo "Device:    $DEVICE"
echo "Filesystem: $FSTYPE"
echo "Mount:     $DATA_ROOT"

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

log "Creating AITOS data layout"
${SUDO[@]} mkdir -p \
  "$DATA_ROOT/clickhouse" \
  "$DATA_ROOT/neo4j" \
  "$DATA_ROOT/redis/live" \
  "$DATA_ROOT/redis/archive"

# Host UID/GID owns the mount root so deployment tooling can inspect it.
# Database subdirectories are owned by their container service UIDs instead.
${SUDO[@]} chown "$HOST_UID:$HOST_GID" "$DATA_ROOT"
${SUDO[@]} chown "$CLICKHOUSE_UID:$CLICKHOUSE_GID" "$DATA_ROOT/clickhouse"
${SUDO[@]} chown "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/neo4j"
${SUDO[@]} chown "$REDIS_UID:$REDIS_GID" "$DATA_ROOT/redis" "$DATA_ROOT/redis/live" "$DATA_ROOT/redis/archive"

${SUDO[@]} chmod 0755 "$DATA_ROOT" "$DATA_ROOT/clickhouse" "$DATA_ROOT/neo4j" "$DATA_ROOT/redis"
${SUDO[@]} chmod 0750 "$DATA_ROOT/redis/live" "$DATA_ROOT/redis/archive"

log "Storage verification"
df -h "$DATA_ROOT"
findmnt --target "$DATA_ROOT"
for required in clickhouse neo4j redis redis/live redis/archive; do
  [[ -d "$DATA_ROOT/$required" ]] || die "Missing required directory: $DATA_ROOT/$required"
done

touch "$DATA_ROOT/.aitos-storage-write-test" 2>/dev/null || true
if [[ -e "$DATA_ROOT/.aitos-storage-write-test" ]]; then
  rm -f "$DATA_ROOT/.aitos-storage-write-test"
else
  # The host deployment UID may not have write permission on the mount root;
  # verify using a temporary root-owned file instead of weakening DB permissions.
  tmp_file="$DATA_ROOT/.aitos-storage-write-test"
  ${SUDO[@]} touch "$tmp_file"
  ${SUDO[@]} rm -f "$tmp_file"
fi

echo "AITOS storage bootstrap completed successfully."
echo "Persistent data root: $DATA_ROOT"
echo "Configured disk UUID:  $DISK_UUID"
