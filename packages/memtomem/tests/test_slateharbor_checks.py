"""The Slateharbor sample's checks after the sample was indexed in place (#2680).

``mm index`` leaves an empty ``.{name}.lock`` sidecar next to every file it
indexes and never deletes it. Pointing ``mm index`` at the sample folder is
the obvious first thing to try, so the sample's own checks and its lab copy
must treat those sidecars as absent, while still catching any other file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "examples/onboarding/slateharbor"
SIDECAR_FOR = ("auth/src/policy.py", "billing/docs/decisions/decision-current.md")


@pytest.fixture
def sample(tmp_path):
    copy = tmp_path / "slateharbor"
    shutil.copytree(SAMPLE, copy, ignore=shutil.ignore_patterns("__pycache__", ".*.lock"))
    return copy


def _drop_sidecars(sample: Path) -> None:
    for name in SIDECAR_FOR:
        source = sample / "project" / name
        assert source.is_file(), name
        (source.parent / f".{source.name}.lock").write_bytes(b"")


def _run(sample: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, script, *args],
        cwd=sample,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


CHECKS = [("generate.py", "--check"), ("validate.py", "--static")]


@pytest.mark.parametrize(("script", "flag"), CHECKS, ids=["generate", "validate"])
def test_checks_pass_with_index_sidecars(sample, script, flag):
    _drop_sidecars(sample)
    result = _run(sample, script, flag)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("script", "flag"), CHECKS, ids=["generate", "validate"])
@pytest.mark.parametrize(
    "stray", ["auth/extra.md", "auth/src/policy.lock"], ids=["extra-source", "undotted-lock"]
)
def test_checks_still_catch_any_other_file(sample, script, flag, stray):
    (sample / "project" / stray).write_text("unexpected\n", encoding="utf-8")
    result = _run(sample, script, flag)
    assert result.returncode != 0, result.stdout + result.stderr


def test_lab_copy_leaves_the_sidecars_behind(sample, monkeypatch):
    _drop_sidecars(sample)
    spec = importlib.util.spec_from_file_location("slateharbor_lab", sample / "lab.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(sample))
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Lab, "run", lambda self, *args, **kwargs: None)

    lab = module.Lab(sample)
    try:
        copied = [p for p in lab.project.rglob("*") if p.is_file()]
        assert not [p for p in copied if p.name.endswith(".lock")]
        assert len(copied) == 150
    finally:
        lab.close()
