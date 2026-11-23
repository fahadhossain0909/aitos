"""Validate security-critical invariants in the fully merged Docker Compose model.

Run after `docker compose config --format json`; checking source YAML alone is
insufficient because Compose replaces command arrays across override files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _option(command: list[str], name: str) -> str | None:
    try:
        return command[command.index(name) + 1]
    except (ValueError, IndexError):
        return None


def validate(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    services = config.get("services", {})
    redis = services.get("redis")
    if not isinstance(redis, dict):
        return ["Compose model is missing the redis service."]

    command = redis.get("command")
    if not isinstance(command, list):
        return ["Redis command must be a normalized argument list."]

    required = {
        "--appendonly": "yes",
        "--appendfsync": "everysec",
        "--maxmemory-policy": "noeviction",
    }
    for flag, expected in required.items():
        actual = _option(command, flag)
        if actual != expected:
            errors.append(f"Redis {flag} must be {expected!r}; got {actual!r}.")

    maxmemory = _option(command, "--maxmemory")
    if not maxmemory or maxmemory in {"0", "0b", "0kb", "0mb", "0gb"}:
        errors.append("Redis maxmemory must be explicitly bounded and non-zero.")

    password = _option(command, "--requirepass")
    if not password:
        errors.append("Redis must have a non-empty --requirepass argument.")

    maintenance = services.get("aitos-storage-maintenance", {})
    environment = maintenance.get("environment", {})
    minimum_free = str(environment.get("DATA_DISK_MIN_FREE_GB", ""))
    if minimum_free != "2.5":
        errors.append(
            "DATA_DISK_MIN_FREE_GB must remain 2.5 GB; "
            f"the merged Compose model contains {minimum_free!r}."
        )

    paper = services.get("aitos-paper", {})
    live = services.get("aitos-live", {})
    if paper and live:
        paper_image = paper.get("image")
        live_image = live.get("image")
        if not paper_image or paper_image != live_image:
            errors.append(
                "Paper and Live must resolve to the exact same runtime image reference."
            )
        paper_mounts = paper.get("volumes", [])
        if any(
            isinstance(mount, dict)
            and mount.get("target") == "/app"
            or isinstance(mount, str)
            and (":/app:" in mount or mount.endswith(":/app"))
            for mount in paper_mounts
        ):
            errors.append(
                "Paper must not bind-mount source over /app; validate the built image."
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read normalized Compose JSON: {exc}", file=sys.stderr)
        return 2

    errors = validate(config)
    if errors:
        for error in errors:
            print(f"COMPOSE POLICY FAIL: {error}", file=sys.stderr)
        return 1

    print("Compose security policy passed (Redis, storage threshold, paper/live parity).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
