#!/usr/bin/env python3
"""Reproducible CPU comparison; each run gets an independent process.

prepare freezes existing bilingual holdout qrels before any inference.
run consumes a frozen JSON {documents:[{id,text}], queries:[{id,text,lang,qrels}]}.
Private corpora/results belong outside the repository. No production DB access.
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(output):
    path = Path(__file__).resolve().parents[1] / "retrieval-eval/benchmark_v2.py"
    spec = importlib.util.spec_from_file_location("cpu_benchmark_fixture", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    validator = module._load_sibling("drift_validator")
    portfolio = module._load_sibling("query_holdout_v2")
    documents = []
    for path in sorted(module.FIXTURE_ROOT.rglob("*.md")):
        if path.stem not in module.GENRES:
            continue
        for chunk in validator.parse_fixture(path):
            documents.append(
                {
                    "id": f"{path.relative_to(module.FIXTURE_ROOT).as_posix()}|{chunk.heading}",
                    "text": chunk.heading + "\n\n" + chunk.body,
                }
            )
    tagged = module.collect_tagged_chunks()
    queries = [
        {
            "id": q.query_id,
            "text": q.text,
            "lang": q.lang,
            "qrels": module.build_qrels(q, tagged)["relevant"],
        }
        for q in portfolio.QUERIES
    ]
    write(
        output,
        {"dataset": "repository-bilingual-holdout-v2", "documents": documents, "queries": queries},
    )


async def worker(args):
    import numpy as np
    import psutil
    from memtomem.config import EmbeddingConfig, embedding_policy_fingerprint
    from memtomem.embedding.onnx import OnnxEmbedder
    from memtomem.chunking.bounded import TokenBudget
    from memtomem.config import Mem2MemConfig

    from benchmark_support import runtime_identity

    implementation = runtime_identity()
    data = json.loads(args.corpus.read_text())
    # Common input contract is independent of which model is measured.
    budget = TokenBudget(Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx")).indexing)
    texts = [doc["text"] for doc in data["documents"]]
    # Refuse truncation; freeze shorter or rechunked documents during preparation.
    if any(budget.count(t, special=True) > 512 for t in texts):
        raise ValueError(
            "Corpus exceeds matched 512-token input budget; prepare lossless fragments first"
        )
    config = EmbeddingConfig(
        provider="onnx",
        model=args.model,
        dimension=384 if "e5" in args.model else 1024,
        threads=args.threads,
        onnx_batch_size=args.batch,
        max_sequence_tokens=512,
        onnx_variant=args.variant,
        onnx_artifact_path=args.artifact or "",
    )
    embedder = OnnxEmbedder(config)
    process = psutil.Process()
    stop = threading.Event()
    samples = []

    def sample():
        process.cpu_percent()
        while not stop.wait(0.05):
            samples.append((process.memory_info().rss, process.cpu_percent()))

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        start = time.perf_counter()
        await embedder.embed_query("warmup")
        load_s = time.perf_counter() - start
        cpu_start = time.process_time()
        start = time.perf_counter()
        if args.reuse_vectors_from:
            previous = json.loads(args.reuse_vectors_from.read_text())
            expected = {
                "model": args.model,
                "variant": args.variant,
                "policy": embedding_policy_fingerprint(config),
                "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
            }
            if any(previous.get(k) != v for k, v in expected.items()):
                raise ValueError("Reused vectors do not match corpus/model/policy")
            vectors = np.load(args.reuse_vectors_from.with_suffix(".npy"))
            if vectors.shape != (len(texts), config.dimension) or not np.isfinite(vectors).all():
                raise ValueError("Reused vectors have invalid shape or values")
            index_s = 0.0
        else:
            vectors = np.asarray(await embedder.embed_texts(texts), dtype=np.float32)
            index_s = time.perf_counter() - start
        index_cpu_s = time.process_time() - cpu_start
        query_cpu_start = time.process_time()
        latencies, ranks, metrics, query_vectors = [], {}, {}, []
        for query in data["queries"]:
            start = time.perf_counter()
            vector = np.asarray(await embedder.embed_query(query["text"]), dtype=np.float32)
            query_vectors.append(vector)
            scores = vectors @ vector
            if args.monolingual:
                allowed = np.asarray(
                    [doc["id"].startswith(query["lang"] + ":") for doc in data["documents"]]
                )
                if not allowed.any():
                    raise ValueError("Monolingual corpus IDs must carry the query language prefix")
                scores = np.where(allowed, scores, -np.inf)
            order = np.argsort(-scores, kind="stable")
            order = order[np.isfinite(scores[order])]
            latencies.append((time.perf_counter() - start) * 1000)
            ids = list(dict.fromkeys(data["documents"][i]["id"] for i in order))[:10]
            ranks[query["id"]] = ids
            rel = query["qrels"]
            relevant = {key for key, value in rel.items() if value > 0}
            if not relevant:
                if data.get("evaluation") == "latency-only-unjudged":
                    continue
                raise ValueError("Queries require frozen positive qrels")
            gains = [float(rel.get(key, 0)) for key in ids]
            dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))
            ideal = sorted(rel.values(), reverse=True)[:10]
            idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
            row = {
                "recall@10": len(set(ids) & relevant) / len(relevant),
                "mrr@10": next((1 / (i + 1) for i, g in enumerate(gains) if g > 0), 0),
                "ndcg@10": dcg / idcg if idcg else 0,
                "zero_hit": int(not any(gains)),
            }
            metrics.setdefault(query["lang"], []).append(row)
        result = {
            "implementation": implementation,
            "retrieval_scope": "monolingual" if args.monolingual else "mixed-corpus",
            "reused_document_vectors": bool(args.reuse_vectors_from),
            "model": args.model,
            "variant": args.variant,
            "threads": args.threads,
            "batch": args.batch,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
            "libraries": {
                name: importlib.metadata.version(name)
                for name in ("onnxruntime", "fastembed", "numpy", "tokenizers")
            },
            "policy": embedding_policy_fingerprint(config),
            "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
            "documents": len(texts),
            "queries": len(latencies),
            "load_s": load_s,
            "index_s": index_s,
            "cpu_s": time.process_time() - cpu_start,
            "index_cpu_s": index_cpu_s,
            "query_cpu_s": time.process_time() - query_cpu_start,
            "query_p50_ms": float(np.percentile(latencies, 50)),
            "query_p95_ms": float(np.percentile(latencies, 95)),
            "retained_rss_bytes": process.memory_info().rss,
            "peak_rss_bytes": max(
                process.memory_info().rss, max((s[0] for s in samples), default=0)
            ),
            "peak_cpu_percent": max((s[1] for s in samples), default=0),
            "metrics": {
                lang: {k: statistics.fmean(r[k] for r in rows) for k in rows[0]}
                for lang, rows in metrics.items()
            },
            "ranks": ranks,
        }
        np.save(args.output.with_suffix(".npy"), vectors)
        np.save(args.output.with_name(args.output.stem + ".queries.npy"), np.asarray(query_vectors))
        write(args.output, result)
    finally:
        stop.set()
        monitor.join(timeout=1)
        await embedder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "worker", "run"])
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="multilingual-e5-small")
    parser.add_argument("--variant", default="fp32")
    parser.add_argument("--artifact")
    parser.add_argument("--bge-artifact")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--monolingual", action="store_true")
    parser.add_argument("--reuse-vectors-from", type=Path)
    parser.add_argument("--reuse-after-first", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output)
    elif args.command == "worker":
        asyncio.run(worker(args))
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        for run in range(args.runs):
            profiles = [("multilingual-e5-small", "fp32", None), ("bge-m3", "fp32", None)]
            if args.artifact:
                variant = json.loads((Path(args.artifact) / "manifest.json").read_text())["variant"]
                profiles.append(("multilingual-e5-small", variant, args.artifact))
            if args.bge_artifact:
                variant = json.loads((Path(args.bge_artifact) / "manifest.json").read_text())[
                    "variant"
                ]
                profiles.append(("bge-m3", variant, args.bge_artifact))
            if run % 2:
                profiles.reverse()
            for model, variant, artifact in profiles:
                cmd = [
                    sys.executable,
                    __file__,
                    "worker",
                    "--corpus",
                    str(args.corpus),
                    "--output",
                    str(args.output / f"{model}-{variant}-{run}.json"),
                    "--model",
                    model,
                    "--variant",
                    variant,
                    "--threads",
                    str(args.threads),
                    "--batch",
                    str(args.batch),
                ]
                if artifact:
                    cmd += ["--artifact", artifact]
                if args.monolingual:
                    cmd += ["--monolingual"]
                if args.reuse_after_first and run > 0:
                    cmd += ["--reuse-vectors-from", str(args.output / f"{model}-{variant}-0.json")]
                subprocess.run(cmd, check=True, timeout=1800)


if __name__ == "__main__":
    main()
