"""Deterministic hybrid memory retrieval.

This is an adapter-level implementation of the retrieval contract used by
Sentrix.  It accepts the output of existing structured, lexical and ANN
retrievers and produces one deduplicated evidence window.  It does not call a
model and does not own canonical facts; all evidence IDs remain traceable to a
source retriever.

Design goals:
* apply scope and hard metadata filters before ranking;
* use reciprocal-rank fusion (RRF) across independent channels;
* merge repeated asset hits without losing channel provenance;
* keep the result deterministic for benchmark replay;
* expose a small bounded TTL cache for repeated multi-turn queries.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
import time
from typing import Any, Iterable, Mapping


def _text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _normalise_query(query: str) -> str:
    return re.sub(r"\s+", " ", _text(query))


@dataclass(frozen=True)
class MemoryQuery:
    """The subset of a Sentrix query that retrieval must enforce."""

    text: str
    scope_id: str
    person_ids: tuple[str, ...] = ()
    place: str = ""
    media_types: tuple[str, ...] = ()
    time_start: str | None = None
    time_end: str | None = None
    user_goal: str = ""

    def cache_key(self, k: int) -> str:
        payload = {
            "text": _normalise_query(self.text),
            "scope": self.scope_id,
            "people": sorted(_text(item) for item in self.person_ids),
            "place": _text(self.place),
            "media": sorted(_text(item) for item in self.media_types),
            "start": self.time_start,
            "end": self.time_end,
            "goal": _normalise_query(self.user_goal),
            "k": int(k),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class RankedMemory:
    asset_id: str
    score: float
    evidence_ids: list[str] = field(default_factory=list)
    channels: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "score": round(self.score, 8),
            "evidence_ids": list(self.evidence_ids),
            "channels": list(self.channels),
            "metadata": dict(self.metadata),
        }


@dataclass
class RetrievalTrace:
    cache_hit: bool
    candidate_count: int
    returned_count: int
    channel_counts: dict[str, int]
    filtered_count: int
    deduplicated_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "cache_hit": self.cache_hit,
            "candidate_count": self.candidate_count,
            "returned_count": self.returned_count,
            "channel_counts": dict(self.channel_counts),
            "filtered_count": self.filtered_count,
            "deduplicated_count": self.deduplicated_count,
        }


class _TTLCache:
    def __init__(self, maxsize: int, ttl_seconds: float):
        self.maxsize = max(0, int(maxsize))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._items: OrderedDict[str, tuple[float, list[dict[str, Any]], RetrievalTrace]] = OrderedDict()

    def get(self, key: str) -> tuple[list[dict[str, Any]], RetrievalTrace] | None:
        if not self.maxsize:
            return None
        item = self._items.get(key)
        if item is None:
            return None
        created, results, trace = item
        if self.ttl_seconds and time.monotonic() - created > self.ttl_seconds:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return [dict(result) for result in results], trace

    def put(self, key: str, results: list[dict[str, Any]], trace: RetrievalTrace) -> None:
        if not self.maxsize:
            return
        self._items[key] = (time.monotonic(), [dict(result) for result in results], trace)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)


class HybridMemoryRetriever:
    """Fuse existing retrieval channels into a single evidence contract.

    A channel item must contain ``asset_id`` and may contain ``rank``,
    ``score``, ``evidence_ids`` and metadata fields such as ``scope_id`` or
    ``captured_at``.  The mapping key is the stable channel name, for example
    ``structured``, ``lexical``, ``semantic_ann`` or ``event``.
    """

    DEFAULT_WEIGHTS = {
        "structured": 1.25,
        "lexical": 1.0,
        "semantic_ann": 0.9,
        "visual_ann": 0.85,
        "event": 0.8,
    }

    def __init__(
        self,
        *,
        rrf_k: int = 60,
        channel_weights: Mapping[str, float] | None = None,
        cache_size: int = 128,
        cache_ttl_seconds: float = 20.0,
    ):
        self.rrf_k = max(1, int(rrf_k))
        self.channel_weights = dict(self.DEFAULT_WEIGHTS)
        self.channel_weights.update(channel_weights or {})
        self._cache = _TTLCache(cache_size, cache_ttl_seconds)

    def retrieve(
        self,
        query: MemoryQuery,
        channels: Mapping[str, Iterable[Mapping[str, Any]]],
        *,
        k: int = 18,
    ) -> tuple[list[dict[str, Any]], RetrievalTrace]:
        k = max(1, int(k))
        key = query.cache_key(k)
        cached = self._cache.get(key)
        if cached is not None:
            results, old_trace = cached
            trace = RetrievalTrace(
                cache_hit=True,
                candidate_count=old_trace.candidate_count,
                returned_count=len(results),
                channel_counts=dict(old_trace.channel_counts),
                filtered_count=old_trace.filtered_count,
                deduplicated_count=old_trace.deduplicated_count,
            )
            return results, trace

        merged: dict[str, RankedMemory] = {}
        channel_counts: dict[str, int] = {}
        candidate_count = 0
        filtered_count = 0
        for channel_name, raw_items in channels.items():
            name = str(channel_name)
            items = list(raw_items or [])
            channel_counts[name] = len(items)
            for position, raw in enumerate(items, 1):
                candidate_count += 1
                item = dict(raw or {})
                asset_id = str(item.get("asset_id") or item.get("id") or "").strip()
                if not asset_id or not self._matches_query(item, query):
                    filtered_count += 1
                    continue
                rank = max(1, int(item.get("rank") or position))
                channel_weight = max(0.0, _number(self.channel_weights.get(name), 1.0))
                rrf = channel_weight / (self.rrf_k + rank)
                score = rrf + 0.05 * max(0.0, min(1.0, _number(item.get("score"))))
                evidence_ids = self._evidence_ids(item, asset_id)
                current = merged.get(asset_id)
                provenance = {
                    "channel": name,
                    "rank": rank,
                    "score": round(_number(item.get("score")), 8),
                    "rrf": round(rrf, 8),
                }
                if current is None:
                    merged[asset_id] = RankedMemory(
                        asset_id=asset_id,
                        score=score,
                        evidence_ids=evidence_ids,
                        channels=[provenance],
                        metadata=self._safe_metadata(item),
                    )
                else:
                    current.score += score
                    current.evidence_ids = list(dict.fromkeys(current.evidence_ids + evidence_ids))
                    current.channels.append(provenance)
                    for key_name, value in self._safe_metadata(item).items():
                        if not current.metadata.get(key_name) and value:
                            current.metadata[key_name] = value

        results = sorted(
            (item.as_dict() for item in merged.values()),
            key=lambda item: (-item["score"], item["asset_id"]),
        )
        for item in results:
            item["channels"].sort(key=lambda value: (value["rank"], value["channel"]))
        results = results[:k]
        trace = RetrievalTrace(
            cache_hit=False,
            candidate_count=candidate_count,
            returned_count=len(results),
            channel_counts=channel_counts,
            filtered_count=filtered_count,
            deduplicated_count=max(0, candidate_count - filtered_count - len(merged)),
        )
        self._cache.put(key, results, trace)
        return results, trace

    @staticmethod
    def _evidence_ids(item: Mapping[str, Any], asset_id: str) -> list[str]:
        raw = item.get("evidence_ids") or item.get("observation_ids") or [asset_id]
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        return list(dict.fromkeys(str(value) for value in raw if str(value).strip()))

    @staticmethod
    def _safe_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
        allowed = ("scope_id", "captured_at", "place", "media_type", "event_id", "person_ids")
        return {key: item.get(key) for key in allowed if item.get(key) not in (None, "", [])}

    @staticmethod
    def _matches_query(item: Mapping[str, Any], query: MemoryQuery) -> bool:
        item_scope = item.get("scope_id")
        if item_scope and str(item_scope) != str(query.scope_id):
            return False
        if query.media_types and str(item.get("media_type") or "") not in set(query.media_types):
            return False
        item_time = _parse_time(item.get("captured_at"))
        start, end = _parse_time(query.time_start), _parse_time(query.time_end)
        if start and (item_time is None or item_time < start):
            return False
        if end and (item_time is None or item_time > end):
            return False
        if query.place and _text(query.place) not in _text(item.get("place")):
            return False
        if query.person_ids:
            present = {str(value) for value in (item.get("person_ids") or [])}
            requested = {str(value) for value in query.person_ids}
            if not present.intersection(requested):
                return False
        return True
