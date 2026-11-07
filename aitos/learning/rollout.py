"""Auditable staged/canary rollout state machine with fail-closed routing hooks."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from aitos.learning.governance import PromotionApproval, validation_evidence_digest


class RolloutCoordinator:
    """Coordinate staging, bounded canary, explicit promotion and rollback.

    Router callbacks must be implemented by an authenticated deployment
    adapter. The coordinator deliberately cannot authenticate a human itself.
    """

    def __init__(
        self,
        path: str,
        registry: Any,
        *,
        canary_router: Callable[[str, str, float], None],
        production_router: Callable[[str, str], None],
        rollback_router: Callable[[str, str | None], None],
        minimum_canary_samples: int = 100,
        minimum_canary_observations: int = 3,
        maximum_error_rate: float = 0.01,
        maximum_drawdown: float = 0.10,
        minimum_return_delta: float = -0.02,
    ) -> None:
        if minimum_canary_samples < 1 or minimum_canary_observations < 1:
            raise ValueError(
                "canary sample and observation thresholds must be positive"
            )
        if not 0 <= maximum_error_rate <= 1 or not 0 <= maximum_drawdown <= 1:
            raise ValueError("canary thresholds must be in [0, 1]")
        self.path = path
        self.registry = registry
        self.canary_router = canary_router
        self.production_router = production_router
        self.rollback_router = rollback_router
        self.minimum_canary_samples = minimum_canary_samples
        self.minimum_canary_observations = minimum_canary_observations
        self.maximum_error_rate = maximum_error_rate
        self.maximum_drawdown = maximum_drawdown
        self.minimum_return_delta = minimum_return_delta

    @contextmanager
    def _lock(self) -> Iterator[None]:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        handle = open(self.path + ".lock", "a+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("rollout journal is corrupt")
        return value

    def _write(self, rows: dict[str, Any]) -> None:
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(rows, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _key(name: str, version: str) -> str:
        if not name.strip() or not version.strip():
            raise ValueError("candidate name and version are required")
        return f"{name}:{version}"

    def stage(
        self,
        name: str,
        version: str,
        validation_result: Any,
        champion_comparison: Any = None,
    ) -> dict[str, Any]:
        key = self._key(name, version)
        if not getattr(validation_result, "passed", False):
            raise ValueError("only a validated candidate can be staged")
        walk = getattr(validation_result, "walk_forward", None)
        if (
            walk is None
            or not getattr(walk, "passed", False)
            or walk.locked_holdout is None
        ):
            raise ValueError(
                "staging requires walk-forward and locked holdout evidence"
            )
        artifact = self.registry.get_artifact(name, version)
        if artifact is None or artifact.status != "candidate":
            raise ValueError("staging requires a registered candidate artifact")
        champion = self.registry.get_champion(name)
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "name": name,
            "version": version,
            "state": "staged",
            "validation_digest": validation_evidence_digest(
                validation_result, champion_comparison
            ),
            "previous_champion": champion.version if champion else None,
            "created_at": now,
            "canary_observations": [],
            "audit": [{"state": "staged", "at": now}],
        }
        with self._lock():
            rows = self._read()
            existing = rows.get(key)
            if existing and existing.get("state") not in {
                "rolled_back",
                "rejected",
            }:
                raise ValueError("candidate already has an active rollout")
            rows[key] = row
            self._write(rows)
        return dict(row)

    def start_canary(
        self, name: str, version: str, traffic_fraction: float = 0.05
    ) -> dict[str, Any]:
        key = self._key(name, version)
        if not 0 < traffic_fraction <= 0.25:
            raise ValueError("canary traffic fraction must be in (0, 0.25]")
        with self._lock():
            rows = self._read()
            row = rows.get(key)
            if not row or row.get("state") != "staged":
                raise ValueError("only staged candidates can enter canary")
            self.canary_router(name, version, traffic_fraction)
            now = datetime.now(timezone.utc).isoformat()
            row["state"] = "canary"
            row["traffic_fraction"] = traffic_fraction
            row["audit"].append(
                {
                    "state": "canary",
                    "at": now,
                    "traffic_fraction": traffic_fraction,
                }
            )
            self._write(rows)
            return dict(row)

    def observe_canary(
        self,
        name: str,
        version: str,
        *,
        sample_count: int,
        error_rate: float,
        drawdown: float,
        return_delta: float,
    ) -> dict[str, Any]:
        key = self._key(name, version)
        values = (float(error_rate), float(drawdown), float(return_delta))
        if sample_count < 0 or not all(math.isfinite(value) for value in values):
            raise ValueError(
                "canary observations must be finite and sample_count non-negative"
            )
        if not 0 <= error_rate <= 1 or not 0 <= drawdown <= 1:
            raise ValueError("canary error rate and drawdown must be in [0, 1]")
        observation_payload = {
            "sample_count": int(sample_count),
            "error_rate": error_rate,
            "drawdown": drawdown,
            "return_delta": return_delta,
        }
        observation_id = hashlib.sha256(
            json.dumps(
                observation_payload, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self._lock():
            rows = self._read()
            row = rows.get(key)
            if not row or row.get("state") not in {"canary", "canary_healthy"}:
                raise ValueError("candidate is not in canary")
            observations = row["canary_observations"]
            if any(
                item.get("observation_id") == observation_id for item in observations
            ):
                return dict(row)
            previous_count = max(
                (int(item["sample_count"]) for item in observations), default=0
            )
            if sample_count <= previous_count:
                raise ValueError("canary sample_count must increase monotonically")
            now = datetime.now(timezone.utc).isoformat()
            observation = {
                **observation_payload,
                "observation_id": observation_id,
                "at": now,
            }
            observations.append(observation)
            failed = (
                error_rate > self.maximum_error_rate
                or drawdown > self.maximum_drawdown
                or return_delta < self.minimum_return_delta
            )
            if failed:
                previous = row.get("previous_champion")
                try:
                    self.rollback_router(name, previous)
                    row["state"] = "rolled_back"
                    row["rollback_reason"] = "canary_threshold_breach"
                except Exception as exc:
                    row["state"] = "rollback_pending"
                    row["rollback_reason"] = "canary_rollback_route_failed"
                    row["rollback_error"] = str(exc)
                row["audit"].append(
                    {
                        "state": row["state"],
                        "at": now,
                        "reason": row["rollback_reason"],
                    }
                )
            elif (
                sample_count >= self.minimum_canary_samples
                and len(observations) >= self.minimum_canary_observations
            ):
                row["state"] = "canary_healthy"
                row["audit"].append({"state": "canary_healthy", "at": now})
            self._write(rows)
            return dict(row)

    def promote(
        self,
        name: str,
        version: str,
        validation_result: Any,
        approval: PromotionApproval,
        champion_comparison: Any = None,
    ) -> Any:
        key = self._key(name, version)
        with self._lock():
            rows = self._read()
            row = rows.get(key)
            if not row or row.get("state") != "canary_healthy":
                raise ValueError("promotion requires a healthy canary")
            digest = validation_evidence_digest(validation_result, champion_comparison)
            if digest != row["validation_digest"]:
                raise ValueError("validation evidence changed after staging")
            promoted = self.registry.promote(
                name,
                version,
                validation_result=validation_result,
                approval=approval,
                champion_comparison=champion_comparison,
            )
            try:
                self.production_router(name, version)
            except Exception as promotion_error:
                previous = row.get("previous_champion")
                try:
                    self.rollback_router(name, previous)
                    if previous:
                        self.registry.rollback(
                            name, previous, reason="production_router_failed"
                        )
                        row["state"] = "rolled_back"
                    else:
                        row["state"] = "rollback_pending"
                except Exception as rollback_error:
                    row["state"] = "rollback_pending"
                    row["rollback_error"] = str(rollback_error)
                now = datetime.now(timezone.utc).isoformat()
                row["rollback_reason"] = "production_router_failed"
                row["audit"].append(
                    {
                        "state": row["state"],
                        "at": now,
                        "reason": row["rollback_reason"],
                    }
                )
                self._write(rows)
                if row["state"] == "rollback_pending":
                    raise RuntimeError(
                        "promotion failed and rollback requires manual reconciliation"
                    ) from promotion_error
                raise
            row["state"] = "promoted"
            row["approval_id"] = approval.approval_id
            now = datetime.now(timezone.utc).isoformat()
            row["audit"].append(
                {
                    "state": "promoted",
                    "at": now,
                    "approval_id": approval.approval_id,
                }
            )
            self._write(rows)
            return promoted

    def rollback(self, name: str, version: str, reason: str) -> Any:
        key = self._key(name, version)
        if not reason.strip():
            raise ValueError("rollback reason is required")
        with self._lock():
            rows = self._read()
            row = rows.get(key)
            if not row or row.get("state") != "promoted":
                raise ValueError("only promoted candidates can be rolled back")
            previous = row.get("previous_champion")
            if not previous:
                row["state"] = "rollback_pending"
                row["rollback_reason"] = reason
                row["audit"].append(
                    {
                        "state": "rollback_pending",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "reason": reason,
                    }
                )
                self._write(rows)
                raise RuntimeError(
                    "no previous champion; manual reconciliation required"
                )
            restored = self.registry.rollback(name, previous, reason=reason)
            try:
                self.rollback_router(name, previous)
            except Exception as exc:
                row["state"] = "rollback_pending"
                row["rollback_reason"] = reason
                row["rollback_error"] = str(exc)
                row["audit"].append(
                    {
                        "state": "rollback_pending",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "reason": reason,
                    }
                )
                self._write(rows)
                raise RuntimeError(
                    "registry restored but runtime rollback needs reconciliation"
                ) from exc
            row["state"] = "rolled_back"
            row["rollback_reason"] = reason
            row["audit"].append(
                {
                    "state": "rolled_back",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                }
            )
            self._write(rows)
            return restored

    def get(self, name: str, version: str) -> dict[str, Any] | None:
        with self._lock():
            row = self._read().get(self._key(name, version))
            return dict(row) if row else None
