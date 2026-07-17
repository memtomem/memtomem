"""Re-export shim — canonical IR metrics now live in the package.

Historically these pure functions lived here under ``tests/``. They moved to
:mod:`memtomem.quality.metrics` (#1802) so replay/evaluation code can import
them at runtime. This shim keeps existing test imports (``from ir_metrics
import ...``) and the ``tools/retrieval-eval`` file-path loaders working
unchanged. Import the package module directly in new code.
"""

from __future__ import annotations

from memtomem.quality.metrics import (
    hit_rate_at_k,
    mean,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    recall_labeled_at_k,
    reciprocal_rank_at_k,
)

__all__ = [
    "recall_at_k",
    "recall_labeled_at_k",
    "reciprocal_rank_at_k",
    "ndcg_at_k",
    "precision_at_k",
    "hit_rate_at_k",
    "mean",
]
