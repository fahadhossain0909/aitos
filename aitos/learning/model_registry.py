"""Versioned model registry with explicit candidate/champion states."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


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
    """Filesystem-backed registry with explicit promotion and rollback history."""

    VALID_STATUSES = {"candidate", "champion", "rejected", "archived"}

    def __init__(self, path: str = "models/registry.json") -> None:
        self.path = path

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        import os

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, sort_keys=True, default=str)
        os.replace(tmp, self.path)

    def register(self, artifact: ModelArtifact) -> ModelArtifact:
        if artifact.status not in self.VALID_STATUSES:
            raise ValueError("invalid model status")
        rows = self._load()
        if any(
            r["name"] == artifact.name and r["version"] == artifact.version
            for r in rows
        ):
            raise ValueError("model version already registered")
        rows.append(artifact.as_dict())
        self._save(rows)
        return artifact

    def promote(self, name: str, version: str) -> ModelArtifact:
        rows = self._load()
        found = None
        for row in rows:
            if row["name"] == name and row["status"] == "champion":
                row["status"] = "archived"
            if row["name"] == name and row["version"] == version:
                if row["status"] != "candidate":
                    raise ValueError("only candidate models can be promoted")
                row["status"] = "champion"
                found = row
        if found is None:
            raise KeyError(f"unknown candidate: {name}:{version}")
        self._save(rows)
        return ModelArtifact(**found)

    def get_champion(self, name: str) -> ModelArtifact | None:
        for row in self._load():
            if row["name"] == name and row["status"] == "champion":
                return ModelArtifact(**row)
        return None
