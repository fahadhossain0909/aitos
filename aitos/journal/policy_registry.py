"""Persistent, validated policy registry for AITOS decision fusion."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ActivePolicy:
    version: str
    weights: dict[str, float]
    min_confidence: float = 0.60
    updated_at: str = ""


class PolicyRegistry:
    """Small atomic JSON registry suitable for a single active policy owner."""

    def __init__(
        self,
        path: str,
        default_weights: Mapping[str, float],
        default_min_confidence: float = 0.60,
    ):
        self._path = Path(path)
        self._default = ActivePolicy(
            "baseline", dict(default_weights), default_min_confidence
        )
        self._active = self._load()

    @property
    def active(self) -> ActivePolicy:
        return self._active

    def activate(
        self,
        version: str,
        weights: Mapping[str, float],
        min_confidence: float | None = None,
    ) -> ActivePolicy:
        normalized = {str(k): float(v) for k, v in weights.items()}
        if not normalized or any(v < 0 for v in normalized.values()):
            raise ValueError("Policy weights must be non-empty and non-negative")
        total = sum(normalized.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError("Policy weights must sum to 1")
        confidence = (
            self._active.min_confidence
            if min_confidence is None
            else float(min_confidence)
        )
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        policy = ActivePolicy(
            str(version), normalized, confidence, datetime.now(timezone.utc).isoformat()
        )
        self._atomic_write(
            {
                "version": policy.version,
                "weights": policy.weights,
                "min_confidence": policy.min_confidence,
                "updated_at": policy.updated_at,
            }
        )
        self._active = policy
        return policy

    def _load(self) -> ActivePolicy:
        if not self._path.exists():
            return self._default
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return ActivePolicy(
                str(data["version"]),
                {str(k): float(v) for k, v in data["weights"].items()},
                float(data.get("min_confidence", self._default.min_confidence)),
                str(data.get("updated_at", "")),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self._default

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
