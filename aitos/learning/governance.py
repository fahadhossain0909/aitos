"""Explicit operator-approval envelope for model promotion.

The registry validates the envelope and binds it to one candidate and one
validation result. Authentication of the human identity must be provided by
the calling UI/API; this module does not claim to authenticate GitHub users.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any


def _metric_payload(metrics: Any, label: str) -> dict[str, float]:
    if metrics is None:
        raise ValueError(f"approval requires {label} backtest metrics")
    payload = {}
    for name in (
        "total_return",
        "max_drawdown",
        "sharpe",
        "trades",
        "initial_equity",
        "final_equity",
    ):
        value = getattr(metrics, name, None)
        if value is None or not isfinite(float(value)):
            raise ValueError(f"approval {label} has invalid {name}")
        payload[name] = float(value)
    return payload


def _metrics(result: Any, champion_comparison: Any = None) -> dict[str, Any]:
    candidate_metrics = _metric_payload(
        getattr(getattr(result, "candidate", None), "metrics", None),
        "candidate",
    )
    walk = getattr(result, "walk_forward", None)
    holdout = getattr(walk, "locked_holdout", None) if walk else None
    if holdout is None or not getattr(walk, "passed", False):
        raise ValueError("approval requires passing walk-forward and locked holdout")
    holdout_metrics = _metric_payload(
        getattr(holdout, "metrics", None), "locked holdout"
    )

    window_payload = []
    for window in getattr(walk, "windows", ()) or ():
        result_metrics = _metric_payload(
            getattr(getattr(window, "result", None), "metrics", None),
            "walk-forward window",
        )
        window_payload.append(
            {
                "test_start": (
                    window.test_start.isoformat()
                    if getattr(window, "test_start", None) is not None
                    else None
                ),
                "test_end": (
                    window.test_end.isoformat()
                    if getattr(window, "test_end", None) is not None
                    else None
                ),
                "metrics": result_metrics,
            }
        )

    champion = getattr(result, "champion", None)
    champion_metrics = (
        _metric_payload(getattr(champion, "metrics", None), "champion")
        if champion is not None
        else None
    )
    return {
        "passed": bool(getattr(result, "passed", False)),
        "reason": str(getattr(result, "reason", "")),
        "candidate": candidate_metrics,
        "holdout": holdout_metrics,
        "walk_forward_windows": window_payload,
        "walk_forward_reason": str(getattr(walk, "reason", "")),
        "champion": champion_metrics,
        "champion_comparison": (
            asdict(champion_comparison) if champion_comparison is not None else None
        ),
    }


def validation_evidence_digest(result: Any, champion_comparison: Any = None) -> str:
    """Stable SHA-256 binding for the complete validation result being approved."""
    payload = json.dumps(
        _metrics(result, champion_comparison), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromotionApproval:
    candidate_name: str
    candidate_version: str
    approved_by: str
    approval_id: str
    validation_digest: str
    approved_at: datetime

    def validate(
        self,
        *,
        candidate_name: str,
        candidate_version: str,
        validation_digest: str,
        now: datetime | None = None,
        max_age: timedelta = timedelta(hours=24),
    ) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.candidate_name,
                self.candidate_version,
                self.approved_by,
                self.approval_id,
                self.validation_digest,
            )
        ):
            raise ValueError("promotion approval fields must be non-empty")
        if (
            self.candidate_name != candidate_name
            or self.candidate_version != candidate_version
        ):
            raise ValueError("approval does not match the requested candidate")
        if self.validation_digest != validation_digest:
            raise ValueError("approval does not match the current validation evidence")
        if (
            not isinstance(self.approved_at, datetime)
            or self.approved_at.tzinfo is None
        ):
            raise ValueError("approval timestamp must be a timezone-aware datetime")
        if max_age.total_seconds() <= 0:
            raise ValueError("approval maximum age must be positive")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("current approval-validation time must be timezone-aware")
        approved = self.approved_at.astimezone(timezone.utc)
        if approved > current + timedelta(minutes=5):
            raise ValueError("approval timestamp is in the future")
        if current - approved > max_age:
            raise ValueError("promotion approval has expired")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["approved_at"] = self.approved_at.astimezone(timezone.utc).isoformat()
        return value
