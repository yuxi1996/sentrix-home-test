"""Production-oriented optimization primitives for the PhotoBench study.

The modules in this package are deliberately independent from the benchmark
HTTP service.  They can be shadowed beside the Sentrix backend first, then
enabled for online reads after the A/B gates in the accompanying document pass.
"""

from .memory_query import HybridMemoryRetriever, MemoryQuery
from .image_relation_graph import RelationGraphBuilder

__all__ = ["HybridMemoryRetriever", "MemoryQuery", "RelationGraphBuilder"]
