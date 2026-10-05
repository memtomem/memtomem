"""Importing memtomem turns ONNX Runtime's telemetry switch on before the runtime loads (#2664)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_VAR = "ORT_DISABLE_TELEMETRY"

# A child process, so neither this process's environment nor its already
# imported modules can stand in for what the import itself does.
_CHILD = (
    "import json, os, sys\n"
    "import memtomem\n"
    "print(json.dumps({\n"
    f"    'value': os.environ.get({_VAR!r}),\n"
    "    'loaded': sorted(\n"
    "        name for name in ('onnxruntime', 'fastembed', 'sentence_transformers')\n"
    "        if name in sys.modules\n"
    "    ),\n"
    "}))\n"
)


def _import_memtomem(preset: str | None) -> dict:
    env = os.environ.copy()
    env.pop(_VAR, None)
    if preset is not None:
        env[_VAR] = preset
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


def test_import_sets_the_switch_when_unset():
    assert _import_memtomem(None)["value"] == "1"


@pytest.mark.parametrize("preset", ["0", "false"])
def test_explicit_value_is_kept(preset):
    assert _import_memtomem(preset)["value"] == preset


def test_import_does_not_load_the_runtime():
    assert _import_memtomem(None)["loaded"] == []
