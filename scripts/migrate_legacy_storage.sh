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

migrate_directory() {
  local label="$1" legacy="$2" canonical="$3"

  if [[ ! -e "$legacy" ]]; then
    echo "No legacy $label path found; nothing to migrate."
    return 0
  fi
  [[ -d "$legacy" && ! -L "$legacy" ]] || die "Legacy $label path is not a real directory: $legacy"

  if [[ -z "$(find "$legacy" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    "${SUDO[@]}" rmdir "$legacy"
    echo "Removed empty legacy $label directory: $legacy"
    return 0
  fi

  log "Legacy $label migration"
  "${SUDO[@]}" mkdir -p "$(dirname "$canonical")"

  if [[ -e "$canonical" || -L "$canonical" ]]; then
    [[ ! -L "$canonical" ]] || die "Canonical $label path is a symlink; refusing migration: $canonical"
    [[ -d "$canonical" ]] || die "Canonical $label path exists but is not a directory: $canonical"
    if [[ -n "$(find "$canonical" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
      die "Canonical $label path already contains data; refusing to overwrite: $canonical"
    fi
    "${SUDO[@]}" rmdir "$canonical"
  fi

  "${SUDO[@]}" mv -- "$legacy" "$canonical"
  "${SUDO[@]}" sync

  [[ ! -e "$legacy" ]] || die "Legacy $label path still exists after migration: $legacy"
  [[ -d "$canonical" ]] || die "Canonical $label path missing after migration: $canonical"
  [[ -n "$(find "$canonical" -mindepth 1 -print -quit 2>/dev/null)" ]] || die "Canonical $label path is unexpectedly empty after migration."

  echo "Legacy $label data migrated successfully:"
  echo "  $legacy -> $canonical"
}

# ClickHouse and Neo4j use a direct directory rename because their canonical
# paths preserve the complete database directory layout.
migrate_directory "ClickHouse" \
  "$DATA_ROOT/clickhouse" \
  "$DATA_ROOT/databases/clickhouse"

migrate_directory "Neo4j" \
  "$DATA_ROOT/neo4j" \
  "$DATA_ROOT/databases/neo4j"

# Redis has a layout migration rather than a simple directory rename: the
# canonical bind mount is eventbus/redis/live and the archive directory is
# separate. Support both the old flat Redis layout and an intermediate
# legacy live/archive layout. Refuse unknown payloads so nothing is silently
# discarded.
LEGACY_REDIS="$DATA_ROOT/redis"
REDIS_ROOT="$DATA_ROOT/eventbus/redis"
REDIS_LIVE="$REDIS_ROOT/live"
REDIS_ARCHIVE="$REDIS_ROOT/archive"

if [[ -e "$LEGACY_REDIS" ]]; then
  [[ -d "$LEGACY_REDIS" && ! -L "$LEGACY_REDIS" ]] || die "Legacy Redis path is not a real directory: $LEGACY_REDIS"
  if [[ -n "$(find "$LEGACY_REDIS" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    log "Legacy Redis migration"
    "${SUDO[@]}" mkdir -p "$REDIS_ROOT"

    # Migrate an intermediate legacy live directory as a whole. This preserves
    # Redis's append-only directory structure and all file metadata.
    LEGACY_REDIS_LIVE="$LEGACY_REDIS/live"
    if [[ -e "$LEGACY_REDIS_LIVE" ]]; then
      [[ -d "$LEGACY_REDIS_LIVE" && ! -L "$LEGACY_REDIS_LIVE" ]] || die "Legacy Redis live path is not a real directory: $LEGACY_REDIS_LIVE"

      if [[ -z "$(find "$LEGACY_REDIS_LIVE" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
        "${SUDO[@]}" rmdir "$LEGACY_REDIS_LIVE"
        echo "Removed empty legacy Redis live directory: $LEGACY_REDIS_LIVE"
      else
        if [[ -e "$REDIS_LIVE" || -L "$REDIS_LIVE" ]]; then
          [[ ! -L "$REDIS_LIVE" ]] || die "Canonical Redis live path is a symlink; refusing migration: $REDIS_LIVE"
          [[ -d "$REDIS_LIVE" ]] || die "Canonical Redis live path exists but is not a directory: $REDIS_LIVE"
          [[ -z "$(find "$REDIS_LIVE" -mindepth 1 -print -quit 2>/dev/null)" ]] || die "Canonical Redis live path already contains data; refusing to overwrite: $REDIS_LIVE"
          "${SUDO[@]}" rmdir "$REDIS_LIVE"
        fi
        "${SUDO[@]}" mv -- "$LEGACY_REDIS_LIVE" "$REDIS_LIVE"
      fi
    fi

    # Migrate a legacy archive directory as a whole when present.
    LEGACY_REDIS_ARCHIVE="$LEGACY_REDIS/archive"
    if [[ -e "$LEGACY_REDIS_ARCHIVE" ]]; then
      [[ -d "$LEGACY_REDIS_ARCHIVE" && ! -L "$LEGACY_REDIS_ARCHIVE" ]] || die "Legacy Redis archive path is not a real directory: $LEGACY_REDIS_ARCHIVE"
      if [[ -z "$(find "$LEGACY_REDIS_ARCHIVE" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
        "${SUDO[@]}" rmdir "$LEGACY_REDIS_ARCHIVE"
      else
        if [[ -e "$REDIS_ARCHIVE" || -L "$REDIS_ARCHIVE" ]]; then
          [[ ! -L "$REDIS_ARCHIVE" ]] || die "Canonical Redis archive path is a symlink; refusing migration: $REDIS_ARCHIVE"
          [[ -d "$REDIS_ARCHIVE" ]] || die "Canonical Redis archive path exists but is not a directory: $REDIS_ARCHIVE"
          [[ -z "$(find "$REDIS_ARCHIVE" -mindepth 1 -print -quit 2>/dev/null)" ]] || die "Canonical Redis archive path already contains data; refusing to overwrite: $REDIS_ARCHIVE"
          "${SUDO[@]}" rmdir "$REDIS_ARCHIVE"
        fi
        "${SUDO[@]}" mkdir -p "$(dirname "$REDIS_ARCHIVE")"
        "${SUDO[@]}" mv -- "$LEGACY_REDIS_ARCHIVE" "$REDIS_ARCHIVE"
      fi
    fi

    # Migrate known flat Redis persistence payloads into the canonical live
    # directory. Unknown files remain fatal rather than being deleted.
    "${SUDO[@]}" mkdir -p "$REDIS_LIVE"
    for item in dump.rdb appendonly.aof appendonlydir; do
      source="$LEGACY_REDIS/$item"
      target="$REDIS_LIVE/$item"
      [[ -e "$source" ]] || continue
      [[ ! -e "$target" ]] || die "Refusing to overwrite existing Redis live payload: $target"
      "${SUDO[@]}" mv -- "$source" "$target"
    done

    if [[ -n "$(find "$LEGACY_REDIS" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
      echo "Remaining legacy Redis paths:" >&2
      find "$LEGACY_REDIS" -mindepth 1 -maxdepth 2 -print >&2
      die "Legacy Redis path contains unknown data after migration; refusing to remove: $LEGACY_REDIS"
    fi
    "${SUDO[@]}" rmdir "$LEGACY_REDIS"
    "${SUDO[@]}" sync
    echo "Legacy Redis data migrated successfully:"
    echo "  $LEGACY_REDIS -> $REDIS_ROOT"
  else
    "${SUDO[@]}" rmdir "$LEGACY_REDIS"
    echo "Removed empty legacy Redis directory: $LEGACY_REDIS"
  fi
fi

echo "Legacy storage migration completed successfully."
