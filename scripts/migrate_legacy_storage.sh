#!/usr/bin/env bash
set -Eeuo pipefail

# One-time migration of known pre-canonical AITOS storage paths.
# This script is intentionally fail-closed: it never overwrites an existing
# canonical data directory and never deletes legacy data until the migration
# has completed and the source path is verified absent.

DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
ALLOW="${AITOS_ALLOW_LEGACY_STORAGE_MIGRATION:-false}"

log() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$DATA_ROOT" = /* ]] || die "AITOS_DATA_ROOT must be absolute."
[[ "$ALLOW" = true ]] || die "Legacy storage migration is disabled; set AITOS_ALLOW_LEGACY_STORAGE_MIGRATION=true only for the deployment migration step."

for command_name in find mountpoint readlink; do
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."
done

SUDO=(sudo)
[[ "$(id -u)" -eq 0 ]] && SUDO=()

mountpoint -q "$DATA_ROOT" || die "Data root is not mounted: $DATA_ROOT"
[[ -d "$DATA_ROOT" && ! -L "$DATA_ROOT" ]] || die "Invalid data root: $DATA_ROOT"

has_entries() {
  # Container-owned files may be invisible to the deployment user. Always
  # inspect protected storage with the same privilege used for migration.
  [[ -n "$("${SUDO[@]}" find "$1" -mindepth 1 -print -quit 2>/dev/null)" ]]
}

migrate_directory() {
  local label="$1" legacy="$2" canonical="$3"

  if [[ ! -e "$legacy" ]]; then
    echo "No legacy $label path found; nothing to migrate."
    return 0
  fi
  [[ -d "$legacy" && ! -L "$legacy" ]] || die "Legacy $label path is not a real directory: $legacy"

  if ! has_entries "$legacy"; then
    "${SUDO[@]}" rmdir -- "$legacy" || die "Legacy $label path changed during inspection; refusing to continue: $legacy"
    echo "Removed empty legacy $label directory: $legacy"
    return 0
  fi

  log "Legacy $label migration"
  "${SUDO[@]}" mkdir -p "$(dirname "$canonical")"

  if [[ -e "$canonical" || -L "$canonical" ]]; then
    [[ ! -L "$canonical" ]] || die "Canonical $label path is a symlink; refusing migration: $canonical"
    [[ -d "$canonical" ]] || die "Canonical $label path exists but is not a directory: $canonical"
    if has_entries "$canonical"; then
      die "Canonical $label path already contains data; refusing to overwrite: $canonical"
    fi
    "${SUDO[@]}" rmdir -- "$canonical" || die "Canonical $label path changed during inspection; refusing migration: $canonical"
  fi

  "${SUDO[@]}" mv -- "$legacy" "$canonical"
  "${SUDO[@]}" sync

  [[ ! -e "$legacy" ]] || die "Legacy $label path still exists after migration: $legacy"
  [[ -d "$canonical" ]] || die "Canonical $label path missing after migration: $canonical"
  has_entries "$canonical" || die "Canonical $label path is unexpectedly empty after migration: $canonical"

  echo "Legacy $label data migrated successfully:"
  echo "  $legacy -> $canonical"
}

migrate_directory "ClickHouse" \
  "$DATA_ROOT/clickhouse" \
  "$DATA_ROOT/databases/clickhouse"

migrate_directory "Neo4j" \
  "$DATA_ROOT/neo4j" \
  "$DATA_ROOT/databases/neo4j"

# Redis has a layout migration rather than a simple directory rename.
LEGACY_REDIS="$DATA_ROOT/redis"
REDIS_ROOT="$DATA_ROOT/eventbus/redis"
REDIS_LIVE="$REDIS_ROOT/live"
REDIS_ARCHIVE="$REDIS_ROOT/archive"

if [[ -e "$LEGACY_REDIS" ]]; then
  [[ -d "$LEGACY_REDIS" && ! -L "$LEGACY_REDIS" ]] || die "Legacy Redis path is not a real directory: $LEGACY_REDIS"
  if has_entries "$LEGACY_REDIS"; then
    log "Legacy Redis migration"
    "${SUDO[@]}" mkdir -p "$REDIS_ROOT"

    LEGACY_REDIS_LIVE="$LEGACY_REDIS/live"
    if [[ -e "$LEGACY_REDIS_LIVE" ]]; then
      [[ -d "$LEGACY_REDIS_LIVE" && ! -L "$LEGACY_REDIS_LIVE" ]] || die "Legacy Redis live path is not a real directory: $LEGACY_REDIS_LIVE"
      if ! has_entries "$LEGACY_REDIS_LIVE"; then
        "${SUDO[@]}" rmdir -- "$LEGACY_REDIS_LIVE" || die "Legacy Redis live path changed during inspection; refusing to continue: $LEGACY_REDIS_LIVE"
        echo "Removed empty legacy Redis live directory: $LEGACY_REDIS_LIVE"
      else
        if [[ -e "$REDIS_LIVE" || -L "$REDIS_LIVE" ]]; then
          [[ ! -L "$REDIS_LIVE" ]] || die "Canonical Redis live path is a symlink; refusing migration: $REDIS_LIVE"
          [[ -d "$REDIS_LIVE" ]] || die "Canonical Redis live path exists but is not a directory: $REDIS_LIVE"
          if has_entries "$REDIS_LIVE"; then
            die "Canonical Redis live path already contains data; refusing to overwrite: $REDIS_LIVE"
          fi
          "${SUDO[@]}" rmdir -- "$REDIS_LIVE" || die "Canonical Redis live path changed during inspection; refusing migration: $REDIS_LIVE"
        fi
        "${SUDO[@]}" mv -- "$LEGACY_REDIS_LIVE" "$REDIS_LIVE"
      fi
    fi

    LEGACY_REDIS_ARCHIVE="$LEGACY_REDIS/archive"
    if [[ -e "$LEGACY_REDIS_ARCHIVE" ]]; then
      [[ -d "$LEGACY_REDIS_ARCHIVE" && ! -L "$LEGACY_REDIS_ARCHIVE" ]] || die "Legacy Redis archive path is not a real directory: $LEGACY_REDIS_ARCHIVE"
      if ! has_entries "$LEGACY_REDIS_ARCHIVE"; then
        "${SUDO[@]}" rmdir -- "$LEGACY_REDIS_ARCHIVE" || die "Legacy Redis archive path changed during inspection; refusing to continue: $LEGACY_REDIS_ARCHIVE"
      else
        if [[ -e "$REDIS_ARCHIVE" || -L "$REDIS_ARCHIVE" ]]; then
          [[ ! -L "$REDIS_ARCHIVE" ]] || die "Canonical Redis archive path is a symlink; refusing migration: $REDIS_ARCHIVE"
          [[ -d "$REDIS_ARCHIVE" ]] || die "Canonical Redis archive path exists but is not a directory: $REDIS_ARCHIVE"
          if has_entries "$REDIS_ARCHIVE"; then
            die "Canonical Redis archive path already contains data; refusing to overwrite: $REDIS_ARCHIVE"
          fi
          "${SUDO[@]}" rmdir -- "$REDIS_ARCHIVE" || die "Canonical Redis archive path changed during inspection; refusing migration: $REDIS_ARCHIVE"
        fi
        "${SUDO[@]}" mkdir -p "$(dirname "$REDIS_ARCHIVE")"
        "${SUDO[@]}" mv -- "$LEGACY_REDIS_ARCHIVE" "$REDIS_ARCHIVE"
      fi
    fi

    "${SUDO[@]}" mkdir -p "$REDIS_LIVE"
    for item in dump.rdb appendonly.aof appendonlydir; do
      source="$LEGACY_REDIS/$item"
      target="$REDIS_LIVE/$item"
      [[ -e "$source" ]] || continue
      [[ ! -e "$target" ]] || die "Refusing to overwrite existing Redis live payload: $target"
      "${SUDO[@]}" mv -- "$source" "$target"
    done

    if has_entries "$LEGACY_REDIS"; then
      echo "Remaining legacy Redis paths:" >&2
      "${SUDO[@]}" find "$LEGACY_REDIS" -mindepth 1 -maxdepth 2 -print >&2
      die "Legacy Redis path contains unknown data after migration; refusing to remove: $LEGACY_REDIS"
    fi
    "${SUDO[@]}" rmdir -- "$LEGACY_REDIS" || die "Legacy Redis root changed during migration; refusing to remove: $LEGACY_REDIS"
    "${SUDO[@]}" sync
    echo "Legacy Redis data migrated successfully:"
    echo "  $LEGACY_REDIS -> $REDIS_ROOT"
  else
    "${SUDO[@]}" rmdir -- "$LEGACY_REDIS" || die "Legacy Redis path changed during inspection; refusing to remove: $LEGACY_REDIS"
    echo "Removed empty legacy Redis directory: $LEGACY_REDIS"
  fi
fi

echo "Legacy storage migration completed successfully."
