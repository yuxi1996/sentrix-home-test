"""Small, dependency-free metrics used by the optimization A/B harness."""

from __future__ import annotations

from itertools import combinations
from typing import Any, Iterable, Mapping


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def memory_retrieval_metrics(predicted: Iterable[str], relevant: Iterable[str], k: int = 18) -> dict[str, float]:
    ranked = list(predicted)[: max(1, int(k))]
    truth = set(str(item) for item in relevant)
    hits = [index for index, item in enumerate(ranked, 1) if str(item) in truth]
    precision = len(hits) / len(ranked) if ranked else 0.0
    recall = len(hits) / len(truth) if truth else 0.0
    reciprocal_rank = 1.0 / hits[0] if hits else 0.0
    dcg = sum(1.0 / __import__("math").log2(index + 1) for index in hits)
    ideal = sum(1.0 / __import__("math").log2(index + 1) for index in range(1, min(len(truth), len(ranked)) + 1))
    return {
        "precision_at_k": precision,
        "recall_at_k": recall,
        "f1_at_k": _f1(precision, recall),
        "mrr": reciprocal_rank,
        "ndcg_at_k": dcg / ideal if ideal else 0.0,
        "success_at_1": 1.0 if hits and hits[0] == 1 else 0.0,
    }


def graph_edge_metrics(predicted: Iterable[Mapping[str, Any]], truth: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    def key(item: Mapping[str, Any]) -> tuple[str, str, str]:
        return (str(item.get("subject_id") or item.get("subject_ref")), str(item.get("predicate")), str(item.get("object_id") or item.get("object_ref")))
    predicted_keys, truth_keys = {key(item) for item in predicted}, {key(item) for item in truth}
    tp = len(predicted_keys & truth_keys)
    precision = tp / len(predicted_keys) if predicted_keys else 0.0
    recall = tp / len(truth_keys) if truth_keys else 0.0
    return {"precision": precision, "recall": recall, "f1": _f1(precision, recall), "tp": float(tp), "fp": float(len(predicted_keys - truth_keys)), "fn": float(len(truth_keys - predicted_keys))}


def pairwise_identity_metrics(predicted: Mapping[str, str], truth: Mapping[str, str]) -> dict[str, float]:
    keys = sorted(set(predicted) & set(truth))
    tp = fp = fn = 0
    for left, right in combinations(keys, 2):
        pred_same, truth_same = predicted[left] == predicted[right], truth[left] == truth[right]
        if pred_same and truth_same:
            tp += 1
        elif pred_same:
            fp += 1
        elif truth_same:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": precision, "recall": recall, "f1": _f1(precision, recall), "tp": float(tp), "fp": float(fp), "fn": float(fn)}
