#!/usr/bin/env bash
set -euo pipefail

# AITOS host storage bootstrap. Durable data lives only on the configured
# data disk; the boot disk is reserved for OS/Docker/deployment state.
# Fail closed: never fall back to the boot disk.
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
[[ -n "$DISK_UUID" ]] || die "AITOS_DATA_DISK_UUID is required; refusing to write database data to the boot disk."
[[ "$DATA_ROOT" = /* ]] || die "AITOS_DATA_ROOT must be absolute."
[[ "$HOST_UID" =~ ^[0-9]+$ && "$HOST_GID" =~ ^[0-9]+$ ]] || die "AITOS_HOST_UID/GID must be numeric."
[[ "$CLICKHOUSE_UID" =~ ^[0-9]+$ && "$CLICKHOUSE_GID" =~ ^[0-9]+$ ]] || die "ClickHouse UID/GID must be numeric."
[[ "$REDIS_UID" =~ ^[0-9]+$ && "$REDIS_GID" =~ ^[0-9]+$ ]] || die "Redis UID/GID must be numeric."
[[ "$NEO4J_UID" =~ ^[0-9]+$ && "$NEO4J_GID" =~ ^[0-9]+$ ]] || die "Neo4j UID/GID must be numeric."
SUDO=(sudo); [[ "$(id -u)" -eq 0 ]] && SUDO=()
for command_name in findmnt mountpoint readlink lsblk; do command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."; done
UUID_LINK="/dev/disk/by-uuid/$DISK_UUID"
[[ -e "$UUID_LINK" ]] || die "Configured disk UUID was not found: $DISK_UUID"
DEVICE="$(readlink -f "$UUID_LINK")"
[[ -b "$DEVICE" ]] || die "Resolved UUID is not a block device: $DEVICE"
FSTYPE="$(lsblk -no FSTYPE "$DEVICE" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
if [[ -z "$FSTYPE" ]] && command -v blkid >/dev/null 2>&1; then FSTYPE="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"; fi
[[ -n "$FSTYPE" ]] || die "No filesystem detected on $DEVICE."
log "Data disk preflight"
echo "UUID: $DISK_UUID"; echo "Device: $DEVICE"; echo "Filesystem: $FSTYPE"; echo "Mount: $DATA_ROOT"
${SUDO[@]} mkdir -p "$DATA_ROOT"
if mountpoint -q "$DATA_ROOT"; then
  CURRENT_SOURCE="$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)"
  CURRENT_DEVICE="$(readlink -f "$CURRENT_SOURCE" 2>/dev/null || true)"
  [[ "$CURRENT_DEVICE" == "$DEVICE" ]] || die "$DATA_ROOT is mounted from $CURRENT_SOURCE, not UUID $DISK_UUID."
else
  ${SUDO[@]} mount -t "$FSTYPE" "$DEVICE" "$DATA_ROOT"
fi
mountpoint -q "$DATA_ROOT" || die "Data disk did not become a mount point."
MOUNT_SOURCE="$(findmnt -rn -o SOURCE --target "$DATA_ROOT" | head -n1)"
MOUNT_DEVICE="$(readlink -f "$MOUNT_SOURCE" 2>/dev/null || true)"
[[ "$MOUNT_DEVICE" == "$DEVICE" ]] || die "Mounted source verification failed."
log "Persisting mount in /etc/fstab"
FSTAB_LINE="UUID=$DISK_UUID $DATA_ROOT $FSTYPE defaults,nofail,x-systemd.device-timeout=30s 0 2"
if ! ${SUDO[@]} grep -Eq "^[[:space:]]*UUID=${DISK_UUID}[[:space:]]+" /etc/fstab; then printf '%s\n' "$FSTAB_LINE" | ${SUDO[@]} tee -a /etc/fstab >/dev/null; fi
log "Creating canonical AITOS data layout"
${SUDO[@]} mkdir -p \
  "$DATA_ROOT/databases/clickhouse" "$DATA_ROOT/databases/neo4j" \
  "$DATA_ROOT/eventbus/redis/live" "$DATA_ROOT/eventbus/redis/archive" \
  "$DATA_ROOT/research/backtest" "$DATA_ROOT/research/replay" \
  "$DATA_ROOT/artifacts/backups" "$DATA_ROOT/artifacts/snapshots" \
  "$DATA_ROOT/runtime/models" "$DATA_ROOT/runtime/logs/neo4j" "$DATA_ROOT/runtime/tmp"
# Fail closed if obsolete root-level paths contain real data. Empty legacy
# directories are removed; no symlinks are recreated because all env paths are
# now canonical.
for legacy in clickhouse neo4j redis; do
  legacy_path="$DATA_ROOT/$legacy"
  if [[ -d "$legacy_path" && ! -L "$legacy_path" ]]; then
    if [[ -n "$(find "$legacy_path" -mindepth 1 -print -quit 2>/dev/null)" ]]; then die "Legacy path contains data: $legacy_path; migrate explicitly before deployment."; fi
    ${SUDO[@]} rmdir "$legacy_path"
  elif [[ -L "$legacy_path" ]]; then
    ${SUDO[@]} rm -f "$legacy_path"
  fi
done
STORAGE_DIR="$REPO_ROOT/.storage"
# The checkout must not be a durable-data location. Remove old compatibility
# links/directories only when empty; never delete their payload implicitly.
for legacy in others; do
  path="$STORAGE_DIR/$legacy"
  if [[ -d "$path" && ! -L "$path" ]]; then
    if [[ -n "$(find "$path" -mindepth 1 -print -quit 2>/dev/null)" ]]; then die "Legacy checkout storage contains data: $path; migrate explicitly."; fi
    ${SUDO[@]} rmdir "$path"
  elif [[ -L "$path" ]]; then ${SUDO[@]} rm -f "$path"; fi
done
${SUDO[@]} chown "$HOST_UID:$HOST_GID" "$DATA_ROOT"
${SUDO[@]} chown -R "$CLICKHOUSE_UID:$CLICKHOUSE_GID" "$DATA_ROOT/databases/clickhouse"
${SUDO[@]} chown -R "$NEO4J_UID:$NEO4J_GID" "$DATA_ROOT/databases/neo4j"
${SUDO[@]} chown -R "$REDIS_UID:$REDIS_GID" "$DATA_ROOT/eventbus/redis"
${SUDO[@]} chown -R "$HOST_UID:$HOST_GID" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
${SUDO[@]} chmod 0755 "$DATA_ROOT" "$DATA_ROOT/databases" "$DATA_ROOT/eventbus" "$DATA_ROOT/research" "$DATA_ROOT/artifacts" "$DATA_ROOT/runtime"
${SUDO[@]} chmod 0750 "$DATA_ROOT/eventbus/redis" "$DATA_ROOT/eventbus/redis/live" "$DATA_ROOT/eventbus/redis/archive"
log "Storage verification"
df -h "$DATA_ROOT"; findmnt --target "$DATA_ROOT"
for required in databases/clickhouse databases/neo4j eventbus/redis/live eventbus/redis/archive research/backtest research/replay artifacts/backups artifacts/snapshots runtime/models runtime/logs/neo4j runtime/tmp; do [[ -d "$DATA_ROOT/$required" ]] || die "Missing required directory: $DATA_ROOT/$required"; done
TMP_FILE="$DATA_ROOT/.aitos-storage-write-test"; ${SUDO[@]} touch "$TMP_FILE"; ${SUDO[@]} rm -f "$TMP_FILE"
echo "AITOS canonical storage bootstrap completed successfully."
