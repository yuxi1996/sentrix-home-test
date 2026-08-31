"""Evidence-aware graph relation construction for image/person memories.

The current Sentrix graph path already normalizes model hypotheses.  This
module adds the missing persistence boundary: stable edge identity,
aggregation across evidence, symmetric-edge canonicalization, conflict
detection, and safe incremental updates.  It intentionally keeps uncertain
edges as ``suggested`` instead of silently promoting them to facts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import math
from typing import Any, Iterable


SYMMETRIC_RELATIONS = {"配偶", "朋友", "同事", "同学", "邻居", "兄弟姐妹", "co_occurs"}
INVERSE_RELATIONS = {
    "父亲": "孩子", "母亲": "孩子", "孩子": "父母", "祖父母": "孙辈",
    "孙辈": "祖父母", "老师": "学生", "学生": "老师", "照护者": "被照护者",
    "被照护者": "照护者", "主人": "访客", "访客": "主人",
}


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _stable_id(scope_id: str, subject: str, predicate: str, obj: str) -> str:
    raw = "\x1f".join((str(scope_id), subject, predicate, obj)).encode()
    return "rel_" + hashlib.sha256(raw).hexdigest()[:20]


@dataclass(frozen=True)
class RelationObservation:
    scope_id: str
    subject_id: str
    predicate: str
    object_id: str
    evidence_id: str
    confidence: float = 0.0
    event_id: str = ""
    moment_id: str = ""
    source: str = "model"


@dataclass
class RelationEdge:
    id: str
    scope_id: str
    subject_id: str
    predicate: str
    object_id: str
    confidence: float
    status: str
    evidence_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    moment_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    revision: int = 1
    conflict: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_id": self.scope_id,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "confidence": round(self.confidence, 8),
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "event_ids": list(self.event_ids),
            "moment_ids": list(self.moment_ids),
            "sources": list(self.sources),
            "revision": self.revision,
            "conflict": self.conflict,
        }


class RelationGraphBuilder:
    """Build and incrementally update evidence-aggregated relation edges.

    Promotion is intentionally conservative: an edge needs at least two
    independent evidence IDs, or one evidence item with confidence >= 0.92,
    before becoming ``confirmed``.  Contradictions remain ``suggested`` and
    are surfaced in ``conflicts`` for review.
    """

    def __init__(self, *, min_evidence: int = 2, confirm_confidence: float = 0.78):
        self.min_evidence = max(1, int(min_evidence))
        self.confirm_confidence = _clamp(confirm_confidence)
        self._edges: dict[str, RelationEdge] = {}

    def build(self, observations: Iterable[RelationObservation]) -> dict[str, Any]:
        self._edges = {}
        self.update(observations)
        return self.snapshot()

    def update(self, observations: Iterable[RelationObservation]) -> dict[str, Any]:
        for observation in observations:
            normalized = self._normalize(observation)
            if normalized is None:
                continue
            key = _stable_id(
                normalized.scope_id,
                normalized.subject_id,
                normalized.predicate,
                normalized.object_id,
            )
            edge = self._edges.get(key)
            is_new = edge is None
            if edge is None:
                edge = RelationEdge(
                    id=key,
                    scope_id=normalized.scope_id,
                    subject_id=normalized.subject_id,
                    predicate=normalized.predicate,
                    object_id=normalized.object_id,
                    confidence=0.0,
                    status="suggested",
                )
                self._edges[key] = edge
            prior = set(edge.evidence_ids)
            changed = False
            if normalized.evidence_id and normalized.evidence_id not in prior:
                edge.evidence_ids.append(normalized.evidence_id)
                edge.confidence = self._aggregate_confidence(edge.confidence, normalized.confidence, len(edge.evidence_ids))
                changed = True
            if normalized.event_id and normalized.event_id not in edge.event_ids:
                edge.event_ids.append(normalized.event_id)
                changed = True
            if normalized.moment_id and normalized.moment_id not in edge.moment_ids:
                edge.moment_ids.append(normalized.moment_id)
                changed = True
            if normalized.source and normalized.source not in edge.sources:
                edge.sources.append(normalized.source)
                changed = True
            if changed and not is_new:
                edge.revision += 1
        self._recompute_states()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        conflicts = self._find_conflicts()
        conflict_ids = {item["edge_id"] for item in conflicts}
        for edge in self._edges.values():
            edge.conflict = edge.id in conflict_ids
            if edge.conflict:
                edge.status = "suggested"
        return {
            "edges": [edge.as_dict() for edge in sorted(self._edges.values(), key=lambda item: item.id)],
            "conflicts": conflicts,
            "stats": self._stats(),
        }

    def _normalize(self, observation: RelationObservation) -> RelationObservation | None:
        scope = str(observation.scope_id or "").strip()
        subject = str(observation.subject_id or "").strip()
        obj = str(observation.object_id or "").strip()
        predicate = str(observation.predicate or "").strip()
        if not scope or not subject or not obj or subject == obj or not predicate:
            return None
        if predicate in SYMMETRIC_RELATIONS and subject > obj:
            subject, obj = obj, subject
        return RelationObservation(
            scope_id=scope,
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            evidence_id=str(observation.evidence_id or "").strip(),
            confidence=_clamp(observation.confidence),
            event_id=str(observation.event_id or "").strip(),
            moment_id=str(observation.moment_id or "").strip(),
            source=str(observation.source or "model").strip(),
        )

    @staticmethod
    def _aggregate_confidence(prior: float, new: float, support_count: int) -> float:
        # Independent observations raise confidence, but never linearly to 1.
        prior = _clamp(prior)
        new = _clamp(new)
        combined = 1.0 - (1.0 - prior) * (1.0 - new)
        support_bonus = min(0.12, 0.03 * max(0, support_count - 1))
        return min(0.995, combined + support_bonus)

    def _recompute_states(self) -> None:
        for edge in self._edges.values():
            if len(edge.evidence_ids) >= self.min_evidence and edge.confidence >= self.confirm_confidence:
                edge.status = "confirmed"
            else:
                edge.status = "suggested"

    def _find_conflicts(self) -> list[dict[str, Any]]:
        grouped: defaultdict[tuple[str, str, str], list[RelationEdge]] = defaultdict(list)
        for edge in self._edges.values():
            if edge.predicate in {"父亲", "母亲", "孩子", "父母"}:
                grouped[(edge.scope_id, edge.subject_id, edge.object_id)].append(edge)
        conflicts = []
        for key, edges in grouped.items():
            predicates = {edge.predicate for edge in edges}
            if len(predicates) > 1:
                for edge in edges:
                    conflicts.append({
                        "edge_id": edge.id,
                        "scope_id": key[0],
                        "subject_id": key[1],
                        "object_id": key[2],
                        "kind": "incompatible_parent_predicates",
                        "predicates": sorted(predicates),
                    })
        return sorted(conflicts, key=lambda item: item["edge_id"])

    def _stats(self) -> dict[str, int]:
        edges = list(self._edges.values())
        return {
            "edge_count": len(edges),
            "confirmed_count": sum(edge.status == "confirmed" for edge in edges),
            "suggested_count": sum(edge.status == "suggested" for edge in edges),
            "conflict_count": sum(edge.conflict for edge in edges),
            "evidence_count": sum(len(edge.evidence_ids) for edge in edges),
        }
