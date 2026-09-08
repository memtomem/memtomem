"""Run the CPU thread/batch matrix serially, with independent workers."""

import argparse
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--corpus", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--artifact")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
for threads in (1, 2, 4):
    for batch in (1, 4, 8):
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("benchmark.py")),
            "run",
            "--corpus",
            str(args.corpus),
            "--output",
            str(args.output / f"t{threads}-b{batch}"),
            "--threads",
            str(threads),
            "--batch",
            str(batch),
        ]
        if args.artifact:
            cmd += ["--artifact", args.artifact]
        subprocess.run(cmd, check=True, timeout=3600)
