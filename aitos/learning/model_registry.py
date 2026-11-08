"""Versioned model registry with evidence-gated promotion and rollback history."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from aitos.config.paths import get_model_data_dir
from aitos.learning.governance import PromotionApproval, validation_evidence_digest
from aitos.learning.regime_feedback import ModelComparison
from aitos.learning.validation import ValidationResult


@dataclass(frozen=True)
class ModelArtifact:
    name: str
    version: str
    kind: str
    status: str = "candidate"
    parent_version: str | None = None
    training_data_id: str | None = None
    metrics: dict[str, float] | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.created_at.isoformat()
        return value


class ModelRegistry:
    """Filesystem-backed registry with serialized writes and evidence-gated promotion."""

    VALID_STATUSES = {"candidate", "champion", "rejected", "archived"}

    def __init__(self, path: str | None = None) -> None:
        self.path = str(path or (get_model_data_dir() / "registry.json"))

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        handle = open(f"{self.path}.lock", "a+b")
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

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _artifact_from_row(row: dict[str, Any]) -> ModelArtifact:
        value = dict(row)
        created_at = value.get("created_at")
        if isinstance(created_at, str):
            value["created_at"] = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )
        return ModelArtifact(**value)

    @staticmethod
    def _metrics_payload(result: Any) -> dict[str, float]:
        metrics = getattr(result, "metrics", None)
        if metrics is None:
            raise ValueError("promotion requires backtest metrics")
        payload: dict[str, float] = {}
        for key in (
            "total_return",
            "max_drawdown",
            "sharpe",
            "trades",
            "initial_equity",
            "final_equity",
        ):
            value = getattr(metrics, key, None)
            if value is None:
                continue
            number = float(value)
            if not isfinite(number):
                raise ValueError(f"promotion metric {key} must be finite")
            payload[key] = number
        required = {"total_return", "max_drawdown", "sharpe", "trades"}
        if not required.issubset(payload):
            raise ValueError("promotion evidence is missing required backtest metrics")
        return payload

    def register(self, artifact: ModelArtifact) -> ModelArtifact:
        if artifact.status != "candidate":
            raise ValueError("new model artifacts must be registered as candidates")
        with self._write_lock():
            rows = self._load()
            if any(
                row["name"] == artifact.name and row["version"] == artifact.version
                for row in rows
            ):
                raise ValueError("model version already registered")
            rows.append(artifact.as_dict())
            self._save(rows)
        return artifact

    def promote(
        self,
        name: str,
        version: str,
        *,
        validation_result: ValidationResult,
        approval: PromotionApproval | None = None,
        champion_comparison: ModelComparison | None = None,
    ) -> ModelArtifact:
        """Promote only after locked holdout, walk-forward and challenger evidence.

        First promotion for a model family requires a passing locked holdout and
        walk-forward result. Replacing an existing champion additionally requires
        a statistically accepted champion/challenger reward comparison.
        """
        if not validation_result.passed:
            raise ValueError("candidate failed validation and cannot be promoted")
        walk_forward = validation_result.walk_forward
        if (
            walk_forward is None
            or not walk_forward.passed
            or walk_forward.locked_holdout is None
        ):
            raise ValueError(
                "promotion requires passing walk-forward and locked holdout"
            )
        candidate_metrics = self._metrics_payload(validation_result.candidate)
        holdout_metrics = self._metrics_payload(walk_forward.locked_holdout)
        digest = validation_evidence_digest(validation_result, champion_comparison)
        if not isinstance(approval, PromotionApproval):
            raise ValueError("promotion requires an explicit operator approval")
        approval.validate(
            candidate_name=name,
            candidate_version=version,
            validation_digest=digest,
        )

        with self._write_lock():
            rows = self._load()
            candidate = next(
                (
                    row
                    for row in rows
                    if row["name"] == name
                    and row["version"] == version
                    and row["status"] == "candidate"
                ),
                None,
            )
            if candidate is None:
                raise KeyError(f"unknown candidate: {name}:{version}")

            current_champion = next(
                (
                    row
                    for row in rows
                    if row["name"] == name and row["status"] == "champion"
                ),
                None,
            )
            if current_champion is not None:
                if champion_comparison is None or not champion_comparison.accepted:
                    raise ValueError(
                        "replacing a champion requires an accepted statistical comparison"
                    )
                if validation_result.champion is None:
                    raise ValueError(
                        "challenger validation must include the current champion backtest"
                    )
            elif champion_comparison is not None and not champion_comparison.accepted:
                raise ValueError(
                    "initial champion comparison, when supplied, must pass"
                )

            if any(
                (row.get("metadata") or {})
                .get("promotion_evidence", {})
                .get("operator_approval", {})
                .get("approval_id")
                == approval.approval_id
                for row in rows
            ):
                raise ValueError("promotion approval has already been consumed")

            evidence = {
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "operator_approval": approval.to_dict(),
                "validation_digest": digest,
                "validation_reason": validation_result.reason,
                "candidate_metrics": candidate_metrics,
                "walk_forward_reason": walk_forward.reason,
                "walk_forward_windows": len(walk_forward.windows),
                "locked_holdout_metrics": holdout_metrics,
                "champion_comparison": (
                    asdict(champion_comparison)
                    if champion_comparison is not None
                    else None
                ),
            }
            metadata = dict(candidate.get("metadata") or {})
            metadata["promotion_evidence"] = evidence

            for row in rows:
                if row["name"] == name and row["status"] == "champion":
                    row["status"] = "archived"
            candidate["status"] = "champion"
            candidate["metadata"] = metadata
            candidate["metrics"] = candidate_metrics
            self._save(rows)
            promoted = dict(candidate)

        return self._artifact_from_row(promoted)

    def get_champion(self, name: str) -> ModelArtifact | None:
        for row in self._load():
            if row["name"] == name and row["status"] == "champion":
                return self._artifact_from_row(row)
        return None

    def list_artifacts(self, name: str | None = None) -> list[ModelArtifact]:
        """Return a snapshot of registered artifacts for governance workflows."""
        rows = self._load()
        if name is not None:
            rows = [row for row in rows if row.get("name") == name]
        return [self._artifact_from_row(dict(row)) for row in rows]

    def get_artifact(self, name: str, version: str) -> ModelArtifact | None:
        for row in self._load():
            if row.get("name") == name and row.get("version") == version:
                return self._artifact_from_row(dict(row))
        return None

    def rollback(self, name: str, version: str, *, reason: str) -> ModelArtifact:
        """Restore a previously archived version as champion with an audit record."""
        if not reason.strip():
            raise ValueError("rollback reason is required")
        with self._write_lock():
            rows = self._load()
            target = next(
                (
                    row
                    for row in rows
                    if row.get("name") == name
                    and row.get("version") == version
                    and row.get("status") in {"archived", "champion"}
                ),
                None,
            )
            if target is None:
                raise KeyError(f"unknown rollback target: {name}:{version}")
            current = next(
                (
                    row
                    for row in rows
                    if row.get("name") == name and row.get("status") == "champion"
                ),
                None,
            )
            if current is target:
                return self._artifact_from_row(dict(target))
            now = datetime.now(timezone.utc).isoformat()
            if current is not None:
                current["status"] = "archived"
                current_meta = dict(current.get("metadata") or {})
                history = list(current_meta.get("rollback_history") or [])
                history.append(
                    {"at": now, "reason": reason, "restored_version": version}
                )
                current_meta["rollback_history"] = history
                current["metadata"] = current_meta
            target["status"] = "champion"
            target_meta = dict(target.get("metadata") or {})
            history = list(target_meta.get("rollback_history") or [])
            history.append(
                {
                    "at": now,
                    "reason": reason,
                    "from_version": current.get("version") if current else None,
                }
            )
            target_meta["rollback_history"] = history
            target["metadata"] = target_meta
            self._save(rows)
            return self._artifact_from_row(dict(target))
