#!/usr/bin/env python3
"""Evaluate canonical JSONL predictions against JSONL ground truth.

The file format is intentionally tiny so the script can consume exported 153
traces without exposing the Sentrix database schema.  See the optimization
document for examples.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optimization.metrics import graph_edge_metrics, memory_retrieval_metrics


def _read(path: str) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-pred")
    parser.add_argument("--memory-truth")
    parser.add_argument("--graph-pred")
    parser.add_argument("--graph-truth")
    parser.add_argument("--k", type=int, default=18)
    args = parser.parse_args()
    if not ((args.memory_pred and args.memory_truth) or (args.graph_pred and args.graph_truth)):
        parser.error("至少提供 memory-pred/truth 或 graph-pred/truth")
    output = {}
    if args.memory_pred and args.memory_truth:
        truth = {str(row["query_id"]): row.get("relevant_asset_ids", []) for row in _read(args.memory_truth)}
        output["memory"] = []
        for row in _read(args.memory_pred):
            query_id = str(row["query_id"])
            ranked = row.get("asset_ids") or [item.get("asset_id") for item in row.get("results", [])]
            output["memory"].append({"query_id": query_id, "metrics": memory_retrieval_metrics(ranked, truth.get(query_id, []), args.k)})
    if args.graph_pred and args.graph_truth:
        truth = {str(row["query_id"]): row.get("edges", []) for row in _read(args.graph_truth)}
        output["graph"] = []
        for row in _read(args.graph_pred):
            query_id = str(row["query_id"])
            output["graph"].append({"query_id": query_id, "metrics": graph_edge_metrics(row.get("edges", []), truth.get(query_id, []))})
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
