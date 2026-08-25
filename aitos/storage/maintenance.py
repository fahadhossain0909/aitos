"""Bounded storage maintenance for boot and data disks."""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import clickhouse_connect

RETENTION_LADDER = (90, 30, 15, 10, 7)
DEFAULT_DB = "aitos"
DEFAULT_BUDGET_GB = 130
DEFAULT_TARGET_GB = 120
DEFAULT_BOOT_BUFFER_GB = 7.5
DEFAULT_DATA_DISK_MIN_FREE_GB = 2.5
DEFAULT_DATA_DISK_TARGET_FREE_GB = 25.0
DEFAULT_AUTO_DELETE_PERCENT = 2.5

BOOT_DISPOSABLE_DIRS = (
    "cache",
    "caches",
    "logs",
    "backtest",
    "snapshots",
    "tmp",
    "backups",
)

EVICTABLE_TABLES = {
    "order_book_snapshots": "time",
    "order_book_updates": "time",
    "market_ohlcv": "time",
}

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


@dataclass(frozen=True)
class StorageConfig:
    clickhouse_budget_gb: float = DEFAULT_BUDGET_GB
    clickhouse_target_gb: float = DEFAULT_TARGET_GB
    boot_buffer_gb: float = DEFAULT_BOOT_BUFFER_GB
    data_disk_min_free_gb: float = DEFAULT_DATA_DISK_MIN_FREE_GB
    data_disk_target_free_gb: float = DEFAULT_DATA_DISK_TARGET_FREE_GB
    interval_seconds: int = 300
    dry_run: bool = False
    auto_delete_percent: float = DEFAULT_AUTO_DELETE_PERCENT

    @classmethod
    def from_env(cls) -> "StorageConfig":
        return cls(
            clickhouse_budget_gb=float(
                os.getenv("CLICKHOUSE_STORAGE_BUDGET_GB", DEFAULT_BUDGET_GB)
            ),
            clickhouse_target_gb=float(
                os.getenv("CLICKHOUSE_STORAGE_TARGET_GB", DEFAULT_TARGET_GB)
            ),
            boot_buffer_gb=float(
                os.getenv("BOOT_FREE_BUFFER_GB", DEFAULT_BOOT_BUFFER_GB)
            ),
            data_disk_min_free_gb=float(
                os.getenv("DATA_DISK_MIN_FREE_GB", DEFAULT_DATA_DISK_MIN_FREE_GB)
            ),
            data_disk_target_free_gb=float(
                os.getenv(
                    "DATA_DISK_TARGET_FREE_GB", DEFAULT_DATA_DISK_TARGET_FREE_GB
                )
            ),
            interval_seconds=int(
                os.getenv("STORAGE_MAINTENANCE_INTERVAL_SECONDS", 300)
            ),
            dry_run=os.getenv("STORAGE_MAINTENANCE_DRY_RUN", "false").lower()
            in {"1", "true", "yes"},
            auto_delete_percent=float(
                os.getenv("STORAGE_AUTO_DELETE_PERCENT", DEFAULT_AUTO_DELETE_PERCENT)
            ),
        )


def _gb(value: int | float) -> float:
    return float(value) / (1024**3)


def _protected(table: str) -> bool:
    name = table.lower()
    return any(token in name for token in PROTECTED_TABLE_TOKENS)


def choose_retention_days(
    current_gb: float, target_gb: float, evictable_daily_gb: float
) -> int:
    if current_gb <= target_gb or evictable_daily_gb <= 0:
        return RETENTION_LADDER[0]
    for days in RETENTION_LADDER:
        if evictable_daily_gb * days <= target_gb:
            return days
    return RETENTION_LADDER[-1]


def _table_inventory(client, database: str):
    rows = client.query(
        """
        SELECT table, sum(bytes_on_disk) AS bytes,
               min(min_time) AS min_time, max(max_time) AS max_time
        FROM system.parts
        WHERE database = {db:String} AND active
        GROUP BY table ORDER BY bytes DESC
        """,
        parameters={"db": database},
    ).result_rows
    return [(str(row[0]), int(row[1] or 0), row[2], row[3]) for row in rows]


def enforce_clickhouse(
    client,
    config: StorageConfig,
    database: str = DEFAULT_DB,
    force_emergency: bool = False,
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
            "retention_days": 7,
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
        _gb(evictable_bytes), _gb(available_evictable_bytes), _gb(daily_bytes)
    )
    if force_emergency:
        retention = RETENTION_LADDER[-1]

    evicted: list[str] = []
    if force_emergency or _gb(total_bytes) > config.clickhouse_target_gb:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        for table, _size, _min_time, _max_time in evictable:
            time_column = EVICTABLE_TABLES[table]
            sql = (
                f"ALTER TABLE `{database}`.`{table}` DELETE WHERE "
                f"{time_column} < {{cutoff:DateTime64(3)}}"
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
        "emergency": force_emergency,
        "protected_data_exceeds_target": protected_bytes > target_bytes,
        "budget_gb": config.clickhouse_budget_gb,
        "target_gb": config.clickhouse_target_gb,
    }


def _files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _boot_free_bytes(root: Path) -> int:
    stats = os.statvfs(root)
    return stats.f_bavail * stats.f_frsize


def _disposable_files(root: Path) -> list[tuple[int, int, Path]]:
    candidates: list[tuple[int, int, Path]] = []
    for priority, dirname in enumerate(BOOT_DISPOSABLE_DIRS):
        directory = root / dirname
        for path in _files(directory):
            try:
                candidates.append((priority, path.stat().st_mtime_ns, path))
            except FileNotFoundError:
                continue
    return candidates


def prune_for_boot_buffer(
    root: Path,
    free_buffer_gb: float,
    delete_percent: float = 2.5,
    dry_run: bool = False,
    known_free_bytes: int | None = None,
) -> dict:
    free_before = (
        known_free_bytes if known_free_bytes is not None else _boot_free_bytes(root)
    )
    target = int(free_buffer_gb * (1024**3))
    if free_before >= target:
        return {
            "free_before_gb": _gb(free_before),
            "free_after_gb": _gb(free_before),
            "deleted_gb": 0.0,
            "deleted_files": [],
            "cleanup_needed": False,
            "reserve_met": True,
        }

    required = target - free_before
    disposable = _disposable_files(root)
    total_disposable = sum(
        path.stat().st_size for _, _, path in disposable if path.exists()
    )
    batch = max(1, int(total_disposable * delete_percent / 100.0))
    delete_bytes = max(required, batch)
    freed = 0
    deleted: list[str] = []

    for _priority, _mtime, path in sorted(
        disposable, key=lambda item: (item[0], item[1])
    ):
        if freed >= delete_bytes:
            break
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if not dry_run:
            path.unlink()
        deleted.append(str(path))
        freed += size

    free_after = _boot_free_bytes(root) if not dry_run else free_before + freed
    return {
        "free_before_gb": _gb(free_before),
        "free_after_gb": _gb(free_after),
        "deleted_gb": _gb(freed),
        "deleted_files": deleted,
        "cleanup_needed": True,
        "reserve_met": free_after >= target,
        "delete_percent": delete_percent,
        "dry_run": dry_run,
    }


def inspect_boot_storage(others_root: Path, config: StorageConfig) -> dict:
    free_bytes = _boot_free_bytes(others_root)
    free_gb = _gb(free_bytes)
    result = {
        "boot_free_gb": free_gb,
        "boot_buffer_gb": config.boot_buffer_gb,
        "reserve_met": free_gb >= config.boot_buffer_gb,
    }
    if not result["reserve_met"]:
        result["prune"] = prune_for_boot_buffer(
            others_root,
            config.boot_buffer_gb,
            config.auto_delete_percent,
            config.dry_run,
            known_free_bytes=free_bytes,
        )
    else:
        result["prune"] = None
    return result


def inspect_data_disk(root: Path, config: StorageConfig) -> dict:
    stats = os.statvfs(root)
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    return {
        "total_gb": _gb(total),
        "free_gb": _gb(free),
        "used_gb": _gb(total - free),
        "min_free_gb": config.data_disk_min_free_gb,
        "target_free_gb": config.data_disk_target_free_gb,
        "emergency": _gb(free) < config.data_disk_min_free_gb,
    }


def run_once(config: StorageConfig, boot_only: bool = False) -> dict:
    others_root = Path(os.getenv("OTHERS_DATA_DIR", "/others"))
    boot_result = inspect_boot_storage(others_root, config)
    if boot_only:
        return {"boot_storage": boot_result}
    data_disk_root = Path(os.getenv("DATA_DISK_DIR", "/data-disk"))
    data_disk_result = inspect_data_disk(data_disk_root, config)
    client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DB", DEFAULT_DB),
    )
    try:
        clickhouse_result = enforce_clickhouse(
            client,
            config,
            os.getenv("CLICKHOUSE_DB", DEFAULT_DB),
            data_disk_result["emergency"],
        )
    finally:
        client.close()
    return {
        "clickhouse": clickhouse_result,
        "data_disk": data_disk_result,
        "boot_storage": boot_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--boot-only", action="store_true")
    args = parser.parse_args()
    config = StorageConfig.from_env()
    if args.once or args.boot_only:
        print(run_once(config, boot_only=args.boot_only), flush=True)
        return
    while True:
        try:
            print(run_once(config), flush=True)
        except Exception as exc:
            print(f"storage maintenance failed: {exc}", flush=True)
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
