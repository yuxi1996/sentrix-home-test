import unittest

from optimization.image_relation_graph import RelationGraphBuilder, RelationObservation
from optimization.memory_query import HybridMemoryRetriever, MemoryQuery
from optimization.metrics import graph_edge_metrics, memory_retrieval_metrics


class MemoryQueryTests(unittest.TestCase):
    def test_scope_and_time_filters_and_cross_channel_dedupe(self):
        query = MemoryQuery(
            text="婚礼合影", scope_id="album-a", time_start="2025-01-01T00:00:00",
            time_end="2025-12-31T23:59:59",
        )
        channels = {
            "structured": [{"asset_id": "a", "scope_id": "album-a", "captured_at": "2025-06-01T12:00:00", "score": 1.0}],
            "semantic_ann": [
                {"asset_id": "a", "scope_id": "album-a", "captured_at": "2025-06-01T12:00:00", "rank": 2, "evidence_ids": ["obs-a"]},
                {"asset_id": "wrong-scope", "scope_id": "album-b", "captured_at": "2025-06-01T12:00:00", "rank": 1},
            ],
            "lexical": [{"asset_id": "old", "scope_id": "album-a", "captured_at": "2024-06-01T12:00:00", "rank": 1}],
        }
        retriever = HybridMemoryRetriever(cache_size=4)
        results, trace = retriever.retrieve(query, channels, k=18)
        self.assertEqual([item["asset_id"] for item in results], ["a"])
        self.assertEqual(results[0]["evidence_ids"], ["a", "obs-a"])
        self.assertEqual(trace.filtered_count, 2)
        self.assertEqual(trace.deduplicated_count, 1)

    def test_repeated_query_uses_cache(self):
        query = MemoryQuery(text="旅行", scope_id="album-a")
        channels = {"lexical": [{"asset_id": "a", "rank": 1}]}
        retriever = HybridMemoryRetriever(cache_size=2)
        _, first = retriever.retrieve(query, channels)
        _, second = retriever.retrieve(query, {})
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)


class GraphTests(unittest.TestCase):
    def test_symmetric_edge_is_canonical_and_incremental_evidence_confirms(self):
        builder = RelationGraphBuilder(min_evidence=2, confirm_confidence=0.7)
        first = RelationObservation("album-a", "p2", "朋友", "p1", "photo-2", 0.8, event_id="event-1")
        second = RelationObservation("album-a", "p1", "朋友", "p2", "photo-3", 0.85, event_id="event-2")
        result = builder.build([first])
        self.assertEqual(result["stats"]["edge_count"], 1)
        self.assertEqual(result["edges"][0]["status"], "suggested")
        self.assertEqual(result["edges"][0]["revision"], 1)
        result = builder.update([second])
        edge = result["edges"][0]
        self.assertEqual((edge["subject_id"], edge["object_id"]), ("p1", "p2"))
        self.assertEqual(edge["status"], "confirmed")
        self.assertEqual(edge["evidence_ids"], ["photo-2", "photo-3"])
        self.assertEqual(edge["revision"], 2)

    def test_conflict_is_surfaced_and_not_confirmed(self):
        builder = RelationGraphBuilder(min_evidence=1, confirm_confidence=0.5)
        result = builder.build([
            RelationObservation("album-a", "p1", "母亲", "p2", "e1", 0.95),
            RelationObservation("album-a", "p1", "父亲", "p2", "e2", 0.95),
        ])
        self.assertEqual(result["stats"]["conflict_count"], 2)
        self.assertTrue(all(edge["status"] == "suggested" for edge in result["edges"]))
        self.assertEqual(len(result["conflicts"]), 2)


class MetricTests(unittest.TestCase):
    def test_memory_metrics(self):
        metrics = memory_retrieval_metrics(["x", "b", "a"], ["a", "b"], k=3)
        self.assertAlmostEqual(metrics["recall_at_k"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 0.5)

    def test_graph_metrics(self):
        metrics = graph_edge_metrics(
            [{"subject_id": "p1", "predicate": "朋友", "object_id": "p2"}],
            [{"subject_id": "p1", "predicate": "朋友", "object_id": "p2"}],
        )
        self.assertEqual(metrics["f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
