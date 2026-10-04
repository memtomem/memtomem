#!/usr/bin/env python3
"""Compare embedding models and rerankers on the language-separated v2 holdout.

Each selected profile runs in its own child process, so the peak RSS it
reports belongs to that profile alone and no process-global state (the FTS
tokenizer, fastembed's custom-model registry, loaded models) carries over.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

NON_COMMERCIAL_LICENSES = frozenset({"cc-by-nc-4.0"})


@dataclass(frozen=True)
class RerankerSpec:
    model: str
    # From the model's Hugging Face metadata (cardData.license), not fastembed.
    license: str
    model_card: str
    # Set for models fastembed 0.8 does not ship; registered in the child.
    hf_repo: str | None = None
    model_file: str | None = None
    size_in_gb: float = 0.0


@dataclass(frozen=True)
class ProfileSpec:
    embedding_models: dict[str, tuple[str, int]] | None
    tokenizer: str = "unicode61"
    reranker: RerankerSpec | None = None


MULTILINGUAL_RERANKER = "jinaai/jina-reranker-v2-base-multilingual"
JINA_CARD = "https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual"
JINA_FP32 = RerankerSpec(MULTILINGUAL_RERANKER, "cc-by-nc-4.0", JINA_CARD)
# Same weights as fp32, quantized; the license does not change with precision.
JINA_INT8 = RerankerSpec(
    f"{MULTILINGUAL_RERANKER}:int8",
    "cc-by-nc-4.0",
    JINA_CARD,
    hf_repo=MULTILINGUAL_RERANKER,
    model_file="onnx/model_int8.onnx",
    size_in_gb=0.28,
)
# #2650's candidate for the wizard's multilingual option. The ONNX export
# declares no license; its base model Alibaba-NLP/gte-multilingual-reranker-base
# is Apache-2.0.
GTE_INT8 = RerankerSpec(
    "onnx-community/gte-multilingual-reranker-base:int8",
    "apache-2.0 (base model; ONNX export declares none)",
    "https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base",
    hf_repo="onnx-community/gte-multilingual-reranker-base",
    model_file="onnx/model_int8.onnx",
    size_in_gb=0.34,
)

BGE_M3_MODELS = {
    "english": ("BAAI/bge-m3", 1024),
    "korean": ("BAAI/bge-m3", 1024),
    "cross_language": ("BAAI/bge-m3", 1024),
}
# The Korean-optimized `mm init` preset: ONNX multilingual-e5-small + kiwipiepy.
E5_MODELS = {
    "english": ("intfloat/multilingual-e5-small", 384),
    "korean": ("intfloat/multilingual-e5-small", 384),
    "cross_language": ("intfloat/multilingual-e5-small", 384),
}
E5_TOKENIZER = "kiwipiepy"

PROFILES: dict[str, ProfileSpec] = {
    "language_specific": ProfileSpec(None),
    "language_specific_reranked": ProfileSpec(None, reranker=JINA_FP32),
    "bge_m3": ProfileSpec(BGE_M3_MODELS),
    "bge_m3_reranked": ProfileSpec(BGE_M3_MODELS, reranker=JINA_FP32),
    "e5_kiwipiepy": ProfileSpec(E5_MODELS, E5_TOKENIZER),
    "e5_kiwipiepy_jina": ProfileSpec(E5_MODELS, E5_TOKENIZER, JINA_FP32),
    "e5_kiwipiepy_jina_int8": ProfileSpec(E5_MODELS, E5_TOKENIZER, JINA_INT8),
    "e5_kiwipiepy_gte": ProfileSpec(E5_MODELS, E5_TOKENIZER, GTE_INT8),
}

# delta name -> (candidate profile, control profile)
DELTAS: dict[str, tuple[str, str]] = {
    "reranker_on_language_specific": ("language_specific_reranked", "language_specific"),
    "bge_m3_vs_language_specific": ("bge_m3", "language_specific"),
    "reranker_on_bge_m3": ("bge_m3_reranked", "bge_m3"),
    "jina_on_e5_kiwipiepy": ("e5_kiwipiepy_jina", "e5_kiwipiepy"),
    "jina_int8_on_e5_kiwipiepy": ("e5_kiwipiepy_jina_int8", "e5_kiwipiepy"),
    "gte_on_e5_kiwipiepy": ("e5_kiwipiepy_gte", "e5_kiwipiepy"),
    "jina_int8_vs_jina_on_e5_kiwipiepy": ("e5_kiwipiepy_jina_int8", "e5_kiwipiepy_jina"),
}

# Files fastembed 0.8 downloads for a Hugging Face model besides model_file and
# additional_files (`ModelManagement.download_files_from_huggingface`).
FASTEMBED_COMMON_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "preprocessor_config.json",
)


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_model_compare_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _macro(track: dict[str, Any], metric: str) -> float:
    values = [
        float(value) for key, value in track["aggregate"].items() if key.endswith(f"|{metric}")
    ]
    return statistics.fmean(values)


def _profile_summary(report: dict[str, Any]) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    for name, track in report["tracks"].items():
        tracks[name] = {
            "embedding": track["embedding"],
            "reranker": track["reranker"],
            "resolved": track["resolved"],
            "index": track["index"],
            "runs": track["runs"],
            "zero_hit_count": track["zero_hit_count"],
            "latency_ms": track["latency_ms"],
            "latency_runs_ms": track["latency_runs_ms"],
            "macro": {
                metric: round(_macro(track, metric), 6)
                for metric in ("recall@10", "mrr@10", "ndcg@10")
            },
            "max_run_spread": track["max_run_spread"],
            "run_spreads": track["run_spreads"],
            "aggregate": track["aggregate"],
        }
    return {"search": report["search"], "tracks": tracks}


def _delta(candidate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in control["tracks"]:
        candidate_track = candidate["tracks"][name]
        control_track = control["tracks"][name]
        result[name] = {
            "macro": {
                metric: round(
                    candidate_track["macro"][metric] - control_track["macro"][metric],
                    6,
                )
                for metric in ("recall@10", "mrr@10", "ndcg@10")
            },
            "zero_hit_count": (candidate_track["zero_hit_count"] - control_track["zero_hit_count"]),
            "p95_latency_ms": round(
                candidate_track["latency_ms"]["p95"] - control_track["latency_ms"]["p95"],
                3,
            ),
            "slices": {
                key: round(float(value) - float(control_track["aggregate"].get(key, 0.0)), 6)
                for key, value in candidate_track["aggregate"].items()
                if key in control_track["aggregate"]
            },
        }
    return result


def _license_notice(spec: ProfileSpec) -> str | None:
    reranker = spec.reranker
    if reranker is None or reranker.license not in NON_COMMERCIAL_LICENSES:
        return None
    return (
        f"notice: {reranker.model} is licensed {reranker.license} (non-commercial use only; "
        f"{reranker.model_card}). It is measured here for comparison and is downloaded "
        "on first use."
    )


def _register_reranker(spec: RerankerSpec) -> None:
    """Register a reranker fastembed does not ship, or confirm an existing one."""
    if spec.hf_repo is None:
        return
    from fastembed.common.model_description import ModelSource
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    for known in TextCrossEncoder.list_supported_models():
        if known["model"] != spec.model:
            continue
        source = (known.get("sources") or {}).get("hf")
        if source != spec.hf_repo or known.get("model_file") != spec.model_file:
            raise RuntimeError(
                f"{spec.model} is already registered from {source}/{known.get('model_file')}, "
                f"not {spec.hf_repo}/{spec.model_file}"
            )
        return
    TextCrossEncoder.add_custom_model(
        model=spec.model,
        sources=ModelSource(hf=spec.hf_repo),
        model_file=spec.model_file or "onnx/model.onnx",
        license=spec.license,
        size_in_gb=spec.size_in_gb,
    )


def _reranker_footprint(model: str, cache_dir: Path) -> dict[str, Any]:
    """Bytes on disk of the files fastembed downloads for ``model``.

    Reads the snapshot ``refs/main`` points at: fastembed calls
    ``snapshot_download`` with the default ``main`` revision both online and
    offline, so that is the snapshot it loaded even when older revisions are
    still cached. Counts the snapshot's common config/tokenizer files plus
    this model's graph and additional files only: jina's fp32 and int8 graphs
    share one snapshot directory, so summing the directory would count both.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    description = next(
        (m for m in TextCrossEncoder.list_supported_models() if m["model"] == model), None
    )
    if description is None:
        raise RuntimeError(f"{model} is not a registered fastembed reranker")
    repo = description["sources"]["hf"]
    model_file = description["model_file"]
    repo_dir = cache_dir / f"models--{repo.replace('/', '--')}"
    revision = (repo_dir / "refs" / "main").read_text(encoding="utf-8").strip()
    snapshot = repo_dir / "snapshots" / revision
    if not (snapshot / model_file).exists():
        raise RuntimeError(f"{model_file} is not in the cached {repo} snapshot {revision}")
    wanted = [*FASTEMBED_COMMON_FILES, model_file, *description.get("additional_files", [])]
    files: dict[str, int] = {}
    seen: set[tuple[int, int]] = set()
    for name in wanted:
        path = snapshot / name
        if not path.exists():
            continue
        stat = path.stat()  # follows the snapshot symlink to its blob
        if (stat.st_dev, stat.st_ino) in seen:
            continue
        seen.add((stat.st_dev, stat.st_ino))
        files[name] = stat.st_size
    return {
        "repo": repo,
        "revision": revision,
        "bytes": sum(files.values()),
        "files": files,
    }


def _peak_rss_bytes() -> int:
    import resource  # POSIX only; imported here so the tests load this module on Windows.

    # ru_maxrss is bytes on macOS and KiB on Linux.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw if sys.platform == "darwin" else raw * 1024)


def run_profile(name: str, *, runs: int, reranker_pool: int) -> dict[str, Any]:
    """Run one profile in this process; meant for the child process only."""
    import asyncio

    from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir

    spec = PROFILES[name]
    if spec.reranker is not None:
        _register_reranker(spec.reranker)
    benchmark = _load_benchmark()
    started = time.perf_counter()
    report = asyncio.run(
        benchmark.benchmark(
            runs=runs,
            embedding_models=spec.embedding_models,
            tokenizer=spec.tokenizer,
            reranker_model=spec.reranker.model if spec.reranker else None,
            reranker_pool=reranker_pool,
        )
    )
    wall_s = time.perf_counter() - started
    peak_rss = _peak_rss_bytes()
    footprint: dict[str, Any] | None = None
    if spec.reranker is not None:
        # Measured after the benchmark; a failure here must not discard it.
        try:
            footprint = _reranker_footprint(spec.reranker.model, resolve_fastembed_cache_dir())
        except (OSError, RuntimeError) as exc:
            footprint = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "profile": name,
        "runs": report["runs"],
        "queries": report["portfolio"]["queries"],
        "portfolio": report["portfolio"],
        "corpus": report["corpus"],
        "environment": {
            **report["environment"],
            "fastembed": importlib.metadata.version("fastembed"),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "summary": _profile_summary(report),
        "cost": {
            "wall_s": round(wall_s, 1),
            # Whole child process: model loads, indexing and every track and run.
            "peak_rss_bytes": peak_rss,
            "reranker_footprint": footprint,
        },
        "license": spec.reranker.license if spec.reranker else None,
        "model_card": spec.reranker.model_card if spec.reranker else None,
    }


def _child_argv(name: str, *, runs: int, reranker_pool: int, output: Path) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--run-profile",
        name,
        "--runs",
        str(runs),
        "--reranker-pool",
        str(reranker_pool),
        "--output",
        str(output),
    ]


def _run_child(name: str, *, runs: int, reranker_pool: int) -> dict[str, Any]:
    with TemporaryDirectory(prefix=f"compare_{name}_") as tmp:
        output = Path(tmp) / "profile.json"
        subprocess.run(
            _child_argv(name, runs=runs, reranker_pool=reranker_pool, output=output),
            check=True,
        )
        return json.loads(output.read_text(encoding="utf-8"))


def compare(
    *,
    runs: int = 1,
    reranker_pool: int = 20,
    profiles: tuple[str, ...] = tuple(PROFILES),
    run_child: Callable[..., dict[str, Any]] = _run_child,
) -> dict[str, Any]:
    unknown = [name for name in profiles if name not in PROFILES]
    if unknown or not profiles:
        raise ValueError(f"unknown or empty profile selection: {unknown or profiles}")
    raw: dict[str, dict[str, Any]] = {}
    for name in profiles:
        notice = _license_notice(PROFILES[name])
        if notice:
            print(notice, file=sys.stderr)
        raw[name] = run_child(name, runs=runs, reranker_pool=reranker_pool)
        footprint = raw[name]["cost"]["reranker_footprint"]
        if footprint is not None and "error" in footprint:
            print(
                f"warning: {name} disk footprint not measured: {footprint['error']}",
                file=sys.stderr,
            )

    shared = {
        field: {name: report[field] for name, report in raw.items()}
        for field in ("runs", "queries", "portfolio", "corpus", "environment")
    }
    for field, values in shared.items():
        if len({json.dumps(value, sort_keys=True) for value in values.values()}) != 1:
            raise RuntimeError(f"profiles disagree on {field}: {values}")
    first = raw[profiles[0]]
    for name, report in raw.items():
        observed_pool = report["summary"]["search"]["reranker_pool"]
        if PROFILES[name].reranker is not None and observed_pool != reranker_pool:
            raise RuntimeError(f"{name} ran with pool {observed_pool}, not {reranker_pool}")

    summaries = {
        name: {
            **report["summary"],
            "cost": report["cost"],
            "license": report["license"],
            "model_card": report["model_card"],
        }
        for name, report in raw.items()
    }
    return {
        "schema_version": 2,
        "methodology": "retrieval-v2-model-reranker-comparison",
        "runs": first["runs"],
        "queries": first["queries"],
        "portfolio": first["portfolio"],
        "corpus": first["corpus"],
        "environment": first["environment"],
        "reranker_pool": reranker_pool,
        "profiles": summaries,
        "deltas": {
            delta: _delta(summaries[candidate], summaries[control])
            for delta, (candidate, control) in DELTAS.items()
            if candidate in summaries and control in summaries
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--reranker-pool", type=int, default=20)
    parser.add_argument(
        "--profiles",
        default=",".join(PROFILES),
        help=f"comma-separated profiles to run (default: all of {', '.join(PROFILES)})",
    )
    parser.add_argument("--run-profile", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.run_profile is not None:
        result = run_profile(args.run_profile, runs=args.runs, reranker_pool=args.reranker_pool)
        args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0
    profiles = tuple(name.strip() for name in args.profiles.split(",") if name.strip())
    report = compare(runs=args.runs, reranker_pool=args.reranker_pool, profiles=profiles)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({report['queries']} queries, {report['runs']} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
