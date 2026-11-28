"""Every file the pinned MiniLM snapshot downloads is checked against a sha256 pin (#3689).

The revision pin alone leaves a corrupted or swapped cached copy of any of the five files
to be loaded by FastEmbed without complaint, as #2465 found for E5.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("huggingface_hub")

import huggingface_hub

from memtomem.embedding import profiles

CONTENTS = {
    "model.onnx": b"fake model",
    "config.json": b'{"fake": "config"}',
    "tokenizer.json": b'{"fake": "tokenizer"}',
    "tokenizer_config.json": b'{"fake": "tokenizer_config"}',
    "special_tokens_map.json": b'{"fake": "special_tokens_map"}',
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    """A fake pinned snapshot, and the ``allow_patterns`` each download asked for."""
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", str(tmp_path / "cache"))
    # Held here because a test may replace ``profiles._verify_file`` with a spy.
    verify = profiles._verify_file
    verify.cache_clear()
    root = tmp_path / "snapshot"
    root.mkdir()
    for name, data in CONTENTS.items():
        (root / name).write_bytes(data)

    requested: list[list[str]] = []

    def snapshot_download(*_args, allow_patterns, **_kwargs):
        requested.append(list(allow_patterns))
        return str(root)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    yield root, requested
    verify.cache_clear()


@pytest.fixture
def fake_pins(monkeypatch):
    # raising=False: the constant does not exist before #3689, and the baseline run
    # of the mismatch cases must reach their assertion rather than fail in setup.
    monkeypatch.setattr(
        profiles,
        "MINILM_SHA256",
        {name: _digest(data) for name, data in CONTENTS.items()},
        raising=False,
    )


@pytest.mark.parametrize("name", CONTENTS)
def test_an_altered_file_stops_the_load(snapshot, fake_pins, name):
    root, _requested = snapshot
    (root / name).write_bytes(b'{"swapped": true}')
    with pytest.raises(ValueError, match="checksum mismatch"):
        profiles.minilm_snapshot()


def test_a_matching_snapshot_loads(snapshot, fake_pins):
    root, _requested = snapshot
    assert profiles.minilm_snapshot() == root


def test_every_downloaded_file_is_verified_against_its_shipped_pin(snapshot, monkeypatch):
    # The shipped pin table is left in place: replacing it would let a file missing from
    # it pass, since the replacement would list every file.
    root, requested = snapshot
    seen: dict[str, str] = {}

    def spy(path, _mtime, _size, expected):
        seen[Path(path).relative_to(root).as_posix()] = expected

    monkeypatch.setattr(profiles, "_verify_file", spy)
    profiles.minilm_snapshot()

    assert len(requested) == 1
    assert set(seen) == set(requested[0])
    assert seen == {name: profiles.MINILM_SHA256[name] for name in requested[0]}
