"""Bounded storage maintenance for ClickHouse and the backtest cache."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import clickhouse_connect

RETENTION_LADDER = (90, 30, 15, 10, 7)
DEFAULT_DB = "aitos"
DEFAULT_BUDGET_GB = 100
DEFAULT_TARGET_GB = 90
DEFAULT_CACHE_GB = 20

# Only explicitly listed high-volume historical tables can be evicted.
# Unknown tables are never mutated automatically.
EVICTABLE_TABLES = {
    "order_book_snapshots": "time",
    "order_book_updates": "time",
    "market_ohlcv": "time",
}

# Extra safeguard: even if a future table is accidentally added to the
# evictable list, names containing these tokens remain protected.
PROTECTED_TABLE_TOKENS = (
    "trade",
    "order",
    "fill",
    "position",
    "decision",
    "risk",
    "model",
    "experience",
    "journal",
    "strategy",
    "execution",
    "portfolio",
)
PROTECTED_CACHE_NAMES = {"manifest.json", "download_manifest.json", ".gitkeep"}


@dataclass(frozen=True)
class StorageConfig:
    clickhouse_budget_gb: float = DEFAULT_BUDGET_GB
    clickhouse_target_gb: float = DEFAULT_TARGET_GB
    backtest_cache_gb: float = DEFAULT_CACHE_GB
    interval_seconds: int = 86400
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "StorageConfig":
        return cls(
            clickhouse_budget_gb=float(
                os.getenv("CLICKHOUSE_STORAGE_BUDGET_GB", DEFAULT_BUDGET_GB)
            ),
            clickhouse_target_gb=float(
                os.getenv("CLICKHOUSE_STORAGE_TARGET_GB", DEFAULT_TARGET_GB)
            ),
            backtest_cache_gb=float(
                os.getenv("BACKTEST_CACHE_MAX_GB", DEFAULT_CACHE_GB)
            ),
            interval_seconds=int(
                os.getenv("STORAGE_MAINTENANCE_INTERVAL_SECONDS", 86400)
            ),
            dry_run=os.getenv("STORAGE_MAINTENANCE_DRY_RUN", "false").lower()
            in {"1", "true", "yes"},
        )


def _gb(value: int | float) -> float:
    return float(value) / (1024**3)


def _protected(table: str) -> bool:
    name = table.lower()
    return any(token in name for token in PROTECTED_TABLE_TOKENS)


def choose_retention_days(
    current_gb: float, target_gb: float, evictable_daily_gb: float
) -> int:
    """Choose the longest configured retention window that fits the budget."""
    if current_gb <= target_gb or evictable_daily_gb <= 0:
        return RETENTION_LADDER[0]
    for days in RETENTION_LADDER:
        if evictable_daily_gb * days <= target_gb:
            return days
    return RETENTION_LADDER[-1]


def _table_inventory(
    client, database: str
) -> list[tuple[str, int, datetime | None, datetime | None]]:
    rows = client.query(
        """
        SELECT table, sum(bytes_on_disk) AS bytes,
               min(min_time) AS min_time, max(max_time) AS max_time
        FROM system.parts
        WHERE database = {db:String} AND active
        GROUP BY table
        ORDER BY bytes DESC
        """,
        parameters={"db": database},
    ).result_rows
    return [(str(row[0]), int(row[1] or 0), row[2], row[3]) for row in rows]


def enforce_clickhouse(
    client, config: StorageConfig, database: str = DEFAULT_DB
) -> dict:
    inventory = _table_inventory(client, database)
    total_bytes = sum(row[1] for row in inventory)
    evictable = [
        row
        for row in inventory
        if row[0] in EVICTABLE_TABLES and not _protected(row[0])
    ]
    evictable_bytes = sum(row[1] for row in evictable)
    protected_bytes = max(0, total_bytes - evictable_bytes)

    if not evictable:
        return {
            "total_gb": _gb(total_bytes),
            "protected_gb": _gb(protected_bytes),
            "evictable_gb": 0.0,
            "retention_days": RETENTION_LADDER[0],
            "evicted": [],
            "reason": "no configured evictable tables",
        }

    daily_bytes = 0.0
    for _table, size, min_time, max_time in evictable:
        if min_time and max_time:
            span_days = max(1.0, (max_time - min_time).total_seconds() / 86400.0)
            daily_bytes += size / span_days

    target_bytes = config.clickhouse_target_gb * (1024**3)
    available_evictable_bytes = max(0.0, target_bytes - protected_bytes)
    retention = choose_retention_days(
        _gb(evictable_bytes),
        _gb(available_evictable_bytes),
        _gb(daily_bytes),
    )

    evicted: list[str] = []
    # Keep the configured target as the normal cleanup trigger. The physical
    # budget is a safety boundary for VPS sizing; this controller never deletes
    # protected data to force the target lower.
    if _gb(total_bytes) > config.clickhouse_target_gb:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        for table, _size, _min_time, _max_time in evictable:
            time_column = EVICTABLE_TABLES[table]
            sql = (
                f"ALTER TABLE `{database}`.`{table}` DELETE "
                f"WHERE {time_column} < {{cutoff:DateTime64(3)}}"
            )
            if not config.dry_run:
                client.command(sql, parameters={"cutoff": cutoff})
            evicted.append(f"{table}<{cutoff.isoformat()}")

    return {
        "total_gb": _gb(total_bytes),
        "protected_gb": _gb(protected_bytes),
        "evictable_gb": _gb(evictable_bytes),
        "retention_days": retention,
        "evicted": evicted,
        "dry_run": config.dry_run,
        "protected_data_exceeds_target": protected_bytes > target_bytes,
        "budget_gb": config.clickhouse_budget_gb,
        "target_gb": config.clickhouse_target_gb,
    }


def _files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (path for path in root.rglob("*") if path.is_file())


def enforce_backtest_cache(root: Path, max_gb: float, dry_run: bool = False) -> dict:
    files = [path for path in _files(root) if path.name not in PROTECTED_CACHE_NAMES]
    files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
    total = sum(path.stat().st_size for path in files if path.exists())
    removed: list[str] = []
    limit = max_gb * (1024**3)

    while total > limit and files:
        path = files.pop(0)
        if not path.exists():
            continue
        size = path.stat().st_size
        if not dry_run:
            path.unlink()
        total -= size
        removed.append(str(path))

    return {
        "cache_gb": _gb(total),
        "max_gb": max_gb,
        "removed": removed,
        "dry_run": dry_run,
    }


def run_once(config: StorageConfig) -> dict:
    client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DB", DEFAULT_DB),
    )
    try:
        clickhouse_result = enforce_clickhouse(
            client, config, os.getenv("CLICKHOUSE_DB", DEFAULT_DB)
        )
    finally:
        client.close()

    cache_result = enforce_backtest_cache(
        Path(os.getenv("BACKTEST_DATA_DIR", "/data/backtest")),
        config.backtest_cache_gb,
        config.dry_run,
    )
    return {"clickhouse": clickhouse_result, "backtest_cache": cache_result}


def main() -> None:
    config = StorageConfig.from_env()
    while True:
        try:
            print(run_once(config), flush=True)
        except Exception as exc:  # pragma: no cover - operational guard
            print(f"storage maintenance failed: {exc}", flush=True)
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
