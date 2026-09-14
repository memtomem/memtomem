"""Package only reviewed first-user assets, with deterministic ZIP metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
START = """# Slateharbor: 여기서 시작하세요

1. [설치 안내](examples/onboarding/slateharbor/README.md)를 따라 Python 환경을 준비합니다.
2. [첫 체험 노트북](examples/notebooks/00_start_here.ipynb)을 열고 Run All을 실행합니다.
   첫 검색만 체험하려면 4번까지 실행한 뒤 9번 정리 셀로 이동하세요.
3. 실제 Claude 연결은 노트북 10번의 선택 단계입니다. 노트북의 임시 기억은 자동 이전되지 않습니다.

API 키·임베딩 모델은 기본 실습에 필요 없습니다. 설치 시에는 인터넷이 필요합니다.
이 파일, examples/, tools/를 함께 유지하세요. 노트북 하나만 복사하지 마세요.
기존 .venv가 있다면 지우거나 다시 만들지 말고 Python 3.12 이상인지 확인하세요.

[검증 기록](examples/onboarding/slateharbor/VALIDATION.md)
"""
NOTEBOOKS = """# 이 묶음의 노트북

[00 — 지난 결정을 다시 설명하지 않고 작업 이어가기](00_start_here.ipynb)가 시작점입니다.
[설치 안내](../onboarding/slateharbor/README.md)와 샘플 폴더를 함께 사용하세요.

이 ZIP에는 00만 포함됩니다. 다른 노트북은
[공식 저장소의 노트북 안내](https://github.com/memtomem/memtomem/tree/main/examples/notebooks)에서
별도로 받으세요. 현재 폴더에 없는 노트북 링크를 따라갈 필요가 없습니다.
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
    entries["examples/notebooks/README.md"] = NOTEBOOKS.encode()
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
