"""Package only reviewed first-user assets, with deterministic ZIP metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
START = """# Slateharbor first-user lab

Start with examples/onboarding/slateharbor/README.md, then open
examples/notebooks/00_start_here.ipynb. Keep this folder structure intact.
Install instructions and all 150 synthetic files are included.
No API key or model is required for the default lab.
"""


def build(output: Path):
    sample = ROOT / "examples/onboarding/slateharbor"
    files = [
        ROOT / "LICENSE",
        ROOT / "examples/notebooks/00_start_here.ipynb",
        ROOT / "tools/check_beginner_notebooks.py",
        ROOT / "tools/build_slateharbor_bundle.py",
    ]
    files += [
        sample / name
        for name in (
            "README.md",
            "VALIDATION.md",
            "validation-results.json",
            "scenarios.py",
            "generate.py",
            "build_notebook.py",
            "lab.py",
            "validate.py",
            "manifest.json",
            "evaluation.json",
        )
    ]
    manifest = json.loads((sample / "manifest.json").read_text(encoding="utf-8"))
    if len(manifest["files"]) != 150:
        raise ValueError("Expected exactly 150 reviewed corpus files")
    for name, digest in manifest["files"].items():
        path = sample / "project" / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
            raise ValueError(f"Invalid corpus path: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Corpus changed: {name}")
        files.append(path)
    entries = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in files}
    entries["START_HERE.md"] = START.encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"{build(args.output)}  {args.output}")


if __name__ == "__main__":
    main()
