#!/usr/bin/env bash
set -Eeuo pipefail

# One-time migration of known pre-canonical AITOS storage paths.
# This script is intentionally fail-closed: it never overwrites an existing
# canonical data directory and never deletes legacy data until the rename has
# completed and the source path is verified absent.

DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
LEGACY="$DATA_ROOT/clickhouse"
CANONICAL="$DATA_ROOT/databases/clickhouse"

log() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$DATA_ROOT" = /* ]] || die "AITOS_DATA_ROOT must be absolute."
[[ "${AITOS_ALLOW_LEGACY_STORAGE_MIGRATION:-false}" = true ]] || die "Legacy storage migration is disabled; set AITOS_ALLOW_LEGACY_STORAGE_MIGRATION=true only for the deployment migration step."

for command_name in find mountpoint readlink; do
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."
done

SUDO=(sudo)
[[ "$(id -u)" -eq 0 ]] && SUDO=()

mountpoint -q "$DATA_ROOT" || die "Data root is not mounted: $DATA_ROOT"
[[ -d "$DATA_ROOT" && ! -L "$DATA_ROOT" ]] || die "Invalid data root: $DATA_ROOT"

if [[ ! -e "$LEGACY" ]]; then
  echo "No legacy ClickHouse path found; nothing to migrate."
  exit 0
fi
[[ -d "$LEGACY" && ! -L "$LEGACY" ]] || die "Legacy ClickHouse path is not a real directory: $LEGACY"

if [[ -z "$(find "$LEGACY" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  "${SUDO[@]}" rmdir "$LEGACY"
  echo "Removed empty legacy ClickHouse directory: $LEGACY"
  exit 0
fi

log "Legacy ClickHouse migration"
"${SUDO[@]}" mkdir -p "$DATA_ROOT/databases"

if [[ -e "$CANONICAL" || -L "$CANONICAL" ]]; then
  if [[ -L "$CANONICAL" ]]; then
    die "Canonical ClickHouse path is a symlink; refusing migration: $CANONICAL"
  fi
  [[ -d "$CANONICAL" ]] || die "Canonical ClickHouse path exists but is not a directory: $CANONICAL"
  if [[ -n "$(find "$CANONICAL" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    die "Canonical ClickHouse path already contains data; refusing to overwrite: $CANONICAL"
  fi
  "${SUDO[@]}" rmdir "$CANONICAL"
fi

# Both paths are on the mounted AITOS data filesystem, so rename preserves the
# files, ownership, permissions, timestamps, and hard links without copying
# terabytes of ClickHouse data through the network.
"${SUDO[@]}" mv -- "$LEGACY" "$CANONICAL"
"${SUDO[@]}" sync

[[ ! -e "$LEGACY" ]] || die "Legacy ClickHouse path still exists after migration: $LEGACY"
[[ -d "$CANONICAL" ]] || die "Canonical ClickHouse path missing after migration: $CANONICAL"
[[ -n "$(find "$CANONICAL" -mindepth 1 -print -quit 2>/dev/null)" ]] || die "Canonical ClickHouse path is unexpectedly empty after migration."

echo "Legacy ClickHouse data migrated successfully:"
echo "  $LEGACY -> $CANONICAL"
