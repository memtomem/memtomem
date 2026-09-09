"""Aggregate independent runs; compare quantization only within a model."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
groups = defaultdict(list)
for path in args.directory.glob("*.json"):
    data = json.loads(path.read_text())
    if "corpus_sha256" in data:
        groups[(data["model"], data["variant"])].append((path, data))
result = {}
for (model, variant), rows in sorted(groups.items()):
    reuse_modes = {r.get("reused_document_vectors", False) for _, r in rows}
    if len(reuse_modes) > 1:
        raise ValueError("Summarize encoding and query-only runs separately")
    hashes = {r["corpus_sha256"] for _, r in rows}
    if len(hashes) != 1:
        raise ValueError("Cannot aggregate different frozen corpora")
    summary = {
        "runs": len(rows),
        "corpus_sha256": next(iter(hashes)),
        "median": {
            key: statistics.median(r[key] for _, r in rows)
            for key in [
                "index_s",
                "cpu_s",
                "load_s",
                "query_p50_ms",
                "query_p95_ms",
                "retained_rss_bytes",
                "peak_rss_bytes",
                "peak_cpu_percent",
            ]
        },
        "metrics": rows[0][1]["metrics"],
    }
    summary["resource_scope"] = "query-only" if True in reuse_modes else "encoding-and-query"
    if True in reuse_modes:
        summary["median"]["index_s"] = None
    control = groups.get((model, "fp32"))
    if variant != "fp32" and control:
        base_path, base = control[0]
        candidate_path, candidate = rows[0]
        if candidate["corpus_sha256"] != base["corpus_sha256"]:
            raise ValueError("Vector comparison requires identical document order/content")
        a = np.load(base_path.with_suffix(".npy"))
        b = np.load(candidate_path.with_suffix(".npy"))
        cosine = np.sum(a * b, axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
        overlap = [
            len(set(ids) & set(candidate["ranks"][q])) / 10 for q, ids in base["ranks"].items()
        ]
        summary["quantization_drift"] = {
            "document_cosine_mean": float(cosine.mean()),
            "document_cosine_min": float(cosine.min()),
            "top10_overlap_mean": statistics.fmean(overlap),
        }
    result[model + "/" + variant] = summary
args.output.write_text(json.dumps(result, indent=2) + "\n")
