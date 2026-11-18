"""Evidence-first entity graph; candidate links never silently become identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import EntityEvidence, EntityLabel


@dataclass(frozen=True, slots=True)
class EntityProfile:
    entity_id: str
    labels: tuple[EntityLabel, ...]
    evidence_count: int
    verified_relations: int
    candidate_relations: int


class EntityEvidenceGraph:
    def __init__(self):
        self._labels: dict[str, list[EntityLabel]] = {}
        self._edges: dict[tuple[str, str, str], EntityEvidence] = {}

    def add_label(self, label: EntityLabel) -> None:
        key = (label.entity_id, label.category, label.label, label.source)
        if not any(
            (x.entity_id, x.category, x.label, x.source) == key
            for x in self._labels.get(label.entity_id, [])
        ):
            self._labels.setdefault(label.entity_id, []).append(label)

    def add_evidence(self, evidence: EntityEvidence) -> None:
        if evidence.status == "verified" and not (
            evidence.relation == "operator_confirmed_link"
            and evidence.evidence_type in {"operator_attestation", "signed_attestation"}
        ):
            raise ValueError(
                "verified ownership links require an explicit operator attestation"
            )
        key = (evidence.subject_id, evidence.object_id, evidence.relation)
        prior = self._edges.get(key)
        if prior is None or evidence.observed_at >= prior.observed_at:
            self._edges[key] = evidence

    def profile(self, entity_id: str) -> EntityProfile:
        labels = tuple(self._labels.get(entity_id, []))
        edges = [
            e for e in self._edges.values() if entity_id in (e.subject_id, e.object_id)
        ]
        return EntityProfile(
            entity_id,
            labels,
            len(edges),
            sum(e.status == "verified" for e in edges),
            sum(e.status == "candidate" for e in edges),
        )

    def candidate_cluster(
        self, entity_id: str, *, as_of: datetime | None = None
    ) -> set[str]:
        """Return direct, explicitly verified links only; no transitive auto-merging."""
        when = as_of or datetime.now(timezone.utc)
        if when.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return {
            e.object_id if e.subject_id == entity_id else e.subject_id
            for e in self._edges.values()
            if e.status == "verified"
            and entity_id in (e.subject_id, e.object_id)
            and e.observed_at <= when
        }
