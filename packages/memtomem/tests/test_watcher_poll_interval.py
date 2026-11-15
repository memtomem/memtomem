"""The polling watcher rescans at ``indexing.watcher_poll_interval_ms``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from watchdog.observers.polling import PollingObserver

from memtomem.config import IndexingConfig
from memtomem.config_signature import build_fresh_config
from memtomem.indexing import watcher as watcher_mod


def test_polling_observer_polls_at_the_configured_interval() -> None:
    config = IndexingConfig(watcher_backend="polling", watcher_poll_interval_ms=250)

    observer = watcher_mod._create_observer(config)

    assert isinstance(observer, PollingObserver)
    assert observer.timeout == 0.25


def test_default_interval_is_ten_seconds() -> None:
    observer = watcher_mod._create_observer(IndexingConfig(watcher_backend="polling"))

    assert observer.timeout == 10.0


@pytest.mark.parametrize("interval_ms", [100, 60000])
def test_interval_bounds_are_inclusive(interval_ms: int) -> None:
    config = IndexingConfig(watcher_poll_interval_ms=interval_ms)

    assert config.watcher_poll_interval_ms == interval_ms


@pytest.mark.parametrize("interval_ms", [99, 60001])
def test_interval_outside_the_bounds_is_rejected(interval_ms: int) -> None:
    with pytest.raises(ValidationError):
        IndexingConfig(watcher_poll_interval_ms=interval_ms)


def test_config_file_sets_the_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for key in list(__import__("os").environ):
        if key.startswith("MEMTOMEM_"):
            monkeypatch.delenv(key, raising=False)
    config_dir = tmp_path / ".memtomem"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"indexing": {"watcher_poll_interval_ms": 10000}}), encoding="utf-8"
    )

    config = build_fresh_config(migrate=False)

    assert config.indexing.watcher_poll_interval_ms == 10000
