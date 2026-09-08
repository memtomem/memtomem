"""Refuse accidentally benchmarking an installed package from another checkout."""

import hashlib
from pathlib import Path


def runtime_identity():
    import memtomem

    actual = Path(memtomem.__file__).resolve()
    roots = [
        Path(__file__).resolve().parents[1] / "src",
        Path(__file__).resolve().parents[2] / "packages/memtomem/src",
    ]
    if not any(actual.is_relative_to(root.resolve()) for root in roots if root.exists()):
        raise RuntimeError(
            "Benchmark imported another checkout; set PYTHONPATH to this checkout src"
        )
    package = actual.parent
    files = [
        "config.py",
        "embedding/onnx.py",
        "embedding/profiles.py",
        "chunking/bounded.py",
        "indexing/engine.py",
        "indexing/source_receipt.py",
    ]
    return {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in files}
