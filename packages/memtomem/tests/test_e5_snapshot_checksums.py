"""Every file the pinned E5 snapshot downloads is checked against a sha256 pin (#2465).

The revision pin alone leaves a corrupted or swapped cached copy of an auxiliary file
(``config.json``, ``tokenizer_config.json``, ``special_tokens_map.json``) to be loaded
by FastEmbed without complaint.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("huggingface_hub")

import huggingface_hub

from memtomem.embedding import profiles

AUXILIARY = ("config.json", "tokenizer_config.json", "special_tokens_map.json")
CONTENTS = {
    "onnx/model.onnx": b"fake model",
    "tokenizer.json": b'{"fake": "tokenizer"}',
    "config.json": b'{"fake": "config"}',
    "tokenizer_config.json": b'{"fake": "tokenizer_config"}',
    "special_tokens_map.json": b'{"fake": "special_tokens_map"}',
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    """A fake pinned snapshot whose files match pins patched to their contents."""
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", str(tmp_path / "cache"))
    # Held here because a test may replace ``profiles._verify_file`` with a spy.
    verify = profiles._verify_file
    verify.cache_clear()
    root = tmp_path / "snapshot"
    for name, data in CONTENTS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    requested: list[list[str]] = []

    def snapshot_download(*_args, allow_patterns, **_kwargs):
        requested.append(list(allow_patterns))
        return str(root)

    def hf_hub_download(_repo, filename, **_kwargs):
        return str(root / filename)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    monkeypatch.setattr(profiles, "E5_MODEL_SHA256", _digest(CONTENTS["onnx/model.onnx"]))
    monkeypatch.setattr(profiles, "E5_TOKENIZER_SHA256", _digest(CONTENTS["tokenizer.json"]))
    # raising=False: the constant does not exist before #2465, and the baseline run
    # of the mismatch cases must reach their assertion rather than fail in setup.
    monkeypatch.setattr(
        profiles,
        "E5_AUXILIARY_SHA256",
        {name: _digest(CONTENTS[name]) for name in AUXILIARY},
        raising=False,
    )
    yield root, requested
    verify.cache_clear()


@pytest.mark.parametrize("name", AUXILIARY)
def test_an_altered_auxiliary_file_stops_the_load(snapshot, name):
    root, _requested = snapshot
    (root / name).write_bytes(b'{"swapped": true}')
    with pytest.raises(ValueError, match="checksum mismatch"):
        profiles.e5_snapshot()


def test_a_matching_snapshot_loads(snapshot):
    root, _requested = snapshot
    assert profiles.e5_snapshot() == root


def test_every_downloaded_file_is_verified_against_its_own_pin(snapshot, monkeypatch):
    root, requested = snapshot
    seen: dict[str, str] = {}
    real_verify = profiles._verify_file

    def spy(path, mtime, size, expected):
        seen[Path(path).relative_to(root).as_posix()] = expected
        return real_verify(path, mtime, size, expected)

    monkeypatch.setattr(profiles, "_verify_file", spy)
    profiles.e5_snapshot()

    assert len(requested) == 1
    assert set(seen) == set(requested[0])
    expected = {
        "onnx/model.onnx": profiles.E5_MODEL_SHA256,
        "tokenizer.json": profiles.E5_TOKENIZER_SHA256,
        **profiles.E5_AUXILIARY_SHA256,
    }
    assert seen == expected
