#!/usr/bin/env python3
"""Export a pinned official ONNX model to a checksummed CPU artifact.

Run in an isolated environment with onnx==1.19.1 onnxruntime==1.23.2.
No network calls; --source must be an already downloaded official snapshot.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import onnx
import numpy as np
import onnxruntime
from onnxruntime.quantization import QuantType, quantize_dynamic


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model", required=True, choices=["intfloat/multilingual-e5-small", "BAAI/bge-m3"]
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--variant",
        required=True,
        choices=["int8-arm64", "int8-avx2", "int8-avx512", "int8-avx512-vnni"],
    )
    parser.add_argument(
        "--quantize-embeddings",
        action="store_true",
        help="Also quantize Gather embedding tables; evaluate quality separately",
    )
    args = parser.parse_args()
    if args.source.name != args.revision or len(args.revision) != 40:
        parser.error("--source must be the pinned 40-character revision snapshot directory")
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.source / "onnx/model.onnx"
    # U8/U8 avoids x86 non-VNNI saturation; ARM and VNNI use signed weights.
    signed = args.variant in ("int8-arm64", "int8-avx512-vnni")
    operators = ["MatMul", "Gather"] if args.quantize_embeddings else ["MatMul"]
    quantize_dynamic(
        str(source),
        str(args.output / "model.onnx"),
        op_types_to_quantize=operators,
        per_channel=True,
        weight_type=QuantType.QInt8 if signed else QuantType.QUInt8,
        use_external_data_format=True,
        extra_options={"MatMulConstBOnly": True},
    )
    for name in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        shutil.copy2(args.source / name, args.output / name)
    manifest = {
        "schema": 1,
        "model": args.model,
        "revision": args.revision,
        "variant": args.variant,
        "precision": "int8",
        "recipe": {
            "op_types": operators,
            "per_channel": True,
            "weight_type": "QInt8" if signed else "QUInt8",
            "MatMulConstBOnly": True,
        },
        "tools": {
            "onnx": onnx.__version__,
            "onnxruntime": onnxruntime.__version__,
            "numpy": np.__version__,
        },
        "source_sha256": sha(source),
        "files": {p.name: sha(p) for p in sorted(args.output.iterdir()) if p.is_file()},
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(args.output / "manifest.json")


if __name__ == "__main__":
    main()
