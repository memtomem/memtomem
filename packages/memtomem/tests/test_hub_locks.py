"""huggingface_hub's shared blob store is turned off where ``flock`` does not work (#3261)."""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("huggingface_hub")

import huggingface_hub
from huggingface_hub import constants

from memtomem.embedding import hub_locks, profiles
from memtomem.embedding.hub_locks import _flock_works, prepare_hub_locks

posix_only = pytest.mark.skipif(os.name == "nt", reason="fcntl.flock is POSIX-only")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_locks, "_checked", set())
    # Both settings turn the shared store off; an ambient one would make the
    # "store in use" assertions below fail for the wrong reason.
    for name in ("HF_HUB_DISABLE_SHARED_BLOBS", "HF_HUB_DISABLE_XET"):
        if hasattr(constants, name):
            monkeypatch.setattr(constants, name, False)
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", str(tmp_path / "cache"))


def _flock_raising(code: int):
    def flock(*_args, **_kwargs):
        raise OSError(code, os.strerror(code))

    return flock


# --- the probe ---------------------------------------------------------------------


@posix_only
def test_probe_passes_where_flock_works(tmp_path):
    assert _flock_works(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


@posix_only
@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EIO])
def test_probe_fails_when_flock_raises(monkeypatch, tmp_path, code):
    import fcntl

    monkeypatch.setattr(fcntl, "flock", _flock_raising(code))
    assert _flock_works(tmp_path) is False


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anyway")
def test_probe_fails_when_the_root_is_not_writable(tmp_path):
    # The Hub writes under the root, not in it: a writable child does not make the
    # probe's verdict "flock works", because the probe could not test it.
    root = tmp_path / "cache"
    (root / ".locks").mkdir(parents=True)
    root.chmod(0o500)
    try:
        assert _flock_works(root) is False
    finally:
        root.chmod(0o700)


def test_probe_passes_without_fcntl(monkeypatch, tmp_path):
    # Windows: filelock locks with msvcrt there and has no soft-lock fallback.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    assert _flock_works(tmp_path) is True


# --- the flip ----------------------------------------------------------------------


def test_failed_probe_turns_shared_blobs_off_once(monkeypatch, tmp_path, caplog):
    probes: list[Path] = []
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: probes.append(d) or False)

    with caplog.at_level(logging.WARNING, logger="memtomem.embedding.hub_locks"):
        prepare_hub_locks(tmp_path)
        prepare_hub_locks(tmp_path)

    assert constants.HF_HUB_DISABLE_SHARED_BLOBS is True
    assert probes == [tmp_path]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert str(tmp_path) in caplog.records[0].getMessage()


def test_working_flock_leaves_shared_blobs_on(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: True)
    with caplog.at_level(logging.WARNING, logger="memtomem.embedding.hub_locks"):
        prepare_hub_locks(tmp_path)
    assert constants.HF_HUB_DISABLE_SHARED_BLOBS is False
    assert caplog.records == []


def test_each_directory_is_probed_once_across_threads(monkeypatch, tmp_path):
    probes: list[Path] = []
    gate = threading.Barrier(8)

    def probe(directory):
        probes.append(directory)
        return True

    monkeypatch.setattr(hub_locks, "_flock_works", probe)

    def run():
        gate.wait()
        prepare_hub_locks(tmp_path)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    prepare_hub_locks(tmp_path / "other")

    assert probes == [tmp_path, tmp_path / "other"]


def test_a_setting_already_on_is_left_alone(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SHARED_BLOBS", True)
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: False)
    with caplog.at_level(logging.WARNING, logger="memtomem.embedding.hub_locks"):
        prepare_hub_locks(tmp_path)
    assert constants.HF_HUB_DISABLE_SHARED_BLOBS is True
    assert caplog.records == []


def test_a_hub_without_the_setting_is_left_alone(monkeypatch, tmp_path, caplog):
    # huggingface-hub before the shared store (the onnx extra allows >=0.20).
    monkeypatch.delattr(constants, "HF_HUB_DISABLE_SHARED_BLOBS", raising=False)
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: False)
    with caplog.at_level(logging.WARNING, logger="memtomem.embedding.hub_locks"):
        prepare_hub_locks(tmp_path)
    assert not hasattr(constants, "HF_HUB_DISABLE_SHARED_BLOBS")
    assert caplog.records == []


# --- what the flip does inside the locked huggingface_hub ---------------------------


def test_the_hub_reads_the_setting_on_each_call(monkeypatch, tmp_path):
    _shared_blobs = pytest.importorskip("huggingface_hub.utils._shared_blobs")
    assert _shared_blobs.shared_blobs_enabled() is True
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: False)
    prepare_hub_locks(tmp_path)
    assert _shared_blobs.shared_blobs_enabled() is False


def _download_through_the_hub(monkeypatch, cache_dir: Path) -> list[str]:
    """Run the Hub's cache download with the network stubbed; return the store calls.

    Patches huggingface_hub internals on purpose: a release that moves the shared-store
    gate fails here, which is the signal this test exists for.
    """
    from huggingface_hub import file_download
    from huggingface_hub.utils._xet import XetFileData

    if not file_download.are_symlinks_supported(str(cache_dir)):
        pytest.skip("the Hub uses the shared store only where symlinks work")
    calls: list[str] = []
    xet = XetFileData(file_hash="ab" * 32, refresh_route="/refresh")

    def metadata(**_kwargs):
        return ("https://example.invalid/f", "etag1", "a" * 40, 3, xet, None)

    def download(*, incomplete_path, destination_path, **_kwargs):
        Path(destination_path).write_bytes(b"abc")

    def try_link(**_kwargs):
        calls.append("reuse")
        return False

    def publish(**_kwargs):
        calls.append("publish")
        return False

    monkeypatch.setattr(file_download, "_get_metadata_or_catch_error", metadata)
    monkeypatch.setattr(file_download, "_download_to_tmp_and_move", download)
    monkeypatch.setattr(file_download, "try_link_from_shared_store", try_link)
    monkeypatch.setattr(file_download, "publish_blob_to_shared_store", publish)
    monkeypatch.setattr(file_download, "is_xet_available", lambda: True)

    path = file_download._hf_hub_download_to_cache_dir(
        cache_dir=str(cache_dir),
        repo_id="org/model",
        filename="model.onnx",
        repo_type="model",
        revision="main",
        endpoint=None,
        etag_timeout=1,
        headers={},
        token=None,
        local_files_only=False,
        force_download=False,
        tqdm_class=None,
        dry_run=False,
    )
    assert Path(path).read_bytes() == b"abc"
    return calls


def test_the_download_path_uses_the_store_when_flock_works(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    prepare_hub_locks(cache_dir)
    assert _download_through_the_hub(monkeypatch, cache_dir) == ["reuse", "publish"]


def test_the_download_path_skips_the_store_after_a_failed_probe(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: False)
    prepare_hub_locks(cache_dir)
    assert _download_through_the_hub(monkeypatch, cache_dir) == []


_SHARED_BLOB_LOCK_CHILD = textwrap.dedent(
    """
    import errno, fcntl, sys
    from pathlib import Path

    def no_flock(*_a, **_k):
        raise OSError(errno.ENOSYS, "Function not implemented")

    fcntl.flock = no_flock
    from huggingface_hub.utils._shared_blobs import _shared_blob_lock

    store = Path(sys.argv[1]) / ("ab" * 32)
    store.parent.mkdir(parents=True, exist_ok=True)
    print("entering", flush=True)
    with _shared_blob_lock(store):
        print("acquired", flush=True)
    """
)


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


@posix_only
def test_why_the_guard_exists(tmp_path):
    """Without flock, the installed filelock cannot take the Hub's shared-blob lock.

    From filelock 4.2.1 the lock reads the empty file the Hub creates as a held soft
    lock and waits with no timeout; 4.2.0 and earlier acquire. When a filelock or
    huggingface_hub release makes 4.2.1+ acquire too, this test fails: the guard and
    its call sites can then go.
    """
    import filelock
    from packaging.version import Version

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _SHARED_BLOB_LOCK_CHILD, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except subprocess.TimeoutExpired as timed_out:
        # A hang counts only once the child reached the lock; a child that broke
        # earlier (an import error, a moved Hub helper) must not pass as one.
        assert _text(timed_out.stdout).splitlines() == ["entering"], _text(timed_out.stderr)
        acquired = False
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["entering", "acquired"], result.stderr
        acquired = True
    assert acquired is (Version(filelock.__version__) < Version("4.2.1"))


# --- the call sites ----------------------------------------------------------------


@pytest.fixture
def recorder(monkeypatch):
    """Record prepare_hub_locks calls and the order against the load they guard."""
    events: list[tuple[str, object]] = []

    def record(cache_dir):
        events.append(("prepare", cache_dir))

    monkeypatch.setattr(hub_locks, "prepare_hub_locks", record)
    return events


def test_tokenizer_download_is_guarded(monkeypatch, recorder):
    monkeypatch.setattr(profiles, "_verify_file", lambda *args: None)

    def download(*_args, cache_dir, **_kwargs):
        recorder.append(("download", cache_dir))
        path = Path(cache_dir) / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    profiles.resolve_tokenizer(profiles.E5_TOKENIZER)

    cache = Path(os.environ["MEMTOMEM_FASTEMBED_CACHE"])
    assert recorder == [("prepare", cache), ("download", str(cache))]


@pytest.mark.parametrize("helper", ["e5_snapshot", "minilm_snapshot"])
def test_snapshot_download_is_guarded(monkeypatch, tmp_path, recorder, helper):
    monkeypatch.setattr(profiles, "_verify_file", lambda *args: None)
    monkeypatch.setattr(profiles, "resolve_tokenizer", lambda path: None)
    snapshot = tmp_path / "snapshot"
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx/model.onnx").write_bytes(b"model")

    def download(*_args, cache_dir, **_kwargs):
        recorder.append(("download", cache_dir))
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    getattr(profiles, helper)()

    cache = Path(os.environ["MEMTOMEM_FASTEMBED_CACHE"])
    assert recorder == [("prepare", cache), ("download", str(cache))]


def test_embedder_load_is_guarded(monkeypatch, tmp_path):
    import memtomem.embedding.onnx as onnx_mod
    from memtomem.config import EmbeddingConfig

    events: list[tuple[str, object]] = []
    monkeypatch.setattr(onnx_mod, "prepare_hub_locks", lambda d: events.append(("prepare", d)))
    monkeypatch.setattr(onnx_mod, "resolve_fastembed_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(onnx_mod, "_register_custom_models_if_needed", lambda: None)

    class Loaded(Exception):
        pass

    def construct(**kwargs):
        events.append(("construct", kwargs["cache_dir"]))
        raise Loaded

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=construct))
    embedder = onnx_mod.OnnxEmbedder(
        EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024)
    )
    with pytest.raises(Loaded):
        embedder._get_model()

    assert events == [("prepare", tmp_path), ("construct", str(tmp_path))]


def test_reranker_load_is_guarded(monkeypatch, tmp_path):
    import memtomem.search.reranker.fastembed as fe_mod
    from memtomem.config import RerankConfig

    events: list[tuple[str, object]] = []
    monkeypatch.setattr(fe_mod, "prepare_hub_locks", lambda d: events.append(("prepare", d)))
    monkeypatch.setattr(fe_mod, "resolve_fastembed_cache_dir", lambda: tmp_path)

    class Constructor:
        def __init__(self, model_name, cache_dir):
            events.append(("construct", cache_dir))

    fake = SimpleNamespace(TextCrossEncoder=Constructor)
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(rerank=fake))
    monkeypatch.setitem(sys.modules, "fastembed.rerank", fake)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", fake)
    monkeypatch.setattr(fe_mod, "ensure_custom_reranker_registered", lambda model: None)

    reranker = fe_mod.FastEmbedReranker(RerankConfig(enabled=True))
    try:
        reranker._get_model()
    finally:
        reranker._close_sync()

    assert events == [("prepare", tmp_path), ("construct", str(tmp_path))]


# --- the local (sentence-transformers) reranker (#3266) -------------------------------

_ST_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@pytest.fixture
def local_load(monkeypatch, tmp_path):
    """Load ``LocalReranker`` against a stub ``CrossEncoder`` and record what ran, in order."""
    import memtomem.search.reranker.local as local_mod
    from memtomem.config import RerankConfig

    events: list[tuple[str, object]] = []

    class CrossEncoder:
        def __init__(self, model):
            events.append(("construct", model))

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=CrossEncoder)
    )
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    hub_cache = tmp_path / "hub"
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(hub_cache))

    def load(model: str = _ST_MODEL) -> None:
        reranker = local_mod.LocalReranker(RerankConfig(provider="local", model=model))
        try:
            reranker._get_model()
        finally:
            reranker._close_sync()

    return SimpleNamespace(events=events, load=load, module=local_mod, hub_cache=hub_cache)


@pytest.fixture
def record_prepare(monkeypatch, local_load):
    def record(cache_dir):
        local_load.events.append(("prepare", cache_dir))

    monkeypatch.setattr(local_load.module, "prepare_hub_locks", record)
    return local_load


def test_local_reranker_load_is_guarded_in_the_hub_cache(record_prepare):
    record_prepare.load()

    hub_cache = record_prepare.hub_cache
    assert record_prepare.events == [("prepare", hub_cache.resolve()), ("construct", _ST_MODEL)]
    # A first install has no cache yet; the probe needs the directory to exist.
    assert hub_cache.is_dir()


def test_both_caches_are_probed_when_sentence_transformers_home_is_set(
    monkeypatch, tmp_path, record_prepare
):
    # sentence-transformers 6.3.0 downloads into SENTENCE_TRANSFORMERS_HOME; 5.2.2
    # and earlier ignore it in CrossEncoder and use the Hub cache.
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(tmp_path / "st"))

    record_prepare.load()

    assert record_prepare.events == [
        ("prepare", (tmp_path / "st").resolve()),
        ("prepare", record_prepare.hub_cache.resolve()),
        ("construct", _ST_MODEL),
    ]


def test_an_empty_sentence_transformers_home_probes_the_working_directory(
    monkeypatch, tmp_path, record_prepare
):
    # sentence-transformers 6.3.0 keeps the empty value and the Hub resolves it to
    # the current directory, which is where the model then lands.
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "")
    monkeypatch.chdir(tmp_path)

    record_prepare.load()

    assert record_prepare.events[0] == ("prepare", tmp_path.resolve())


def test_a_relative_cache_is_probed_again_after_a_chdir(monkeypatch, tmp_path, local_load):
    probes: list[Path] = []
    monkeypatch.setattr(hub_locks, "_flock_works", lambda d: probes.append(d) or True)
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "models")
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        monkeypatch.chdir(tmp_path / name)
        local_load.load()

    assert probes == [
        (tmp_path / "a/models").resolve(),
        local_load.hub_cache.resolve(),
        (tmp_path / "b/models").resolve(),
    ]


def test_a_local_model_directory_is_not_probed(tmp_path, record_prepare):
    record_prepare.load(str(tmp_path))

    assert record_prepare.events == [("construct", str(tmp_path))]


def test_an_unresolvable_cache_still_loads(monkeypatch, record_prepare):
    def unresolvable(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", unresolvable)

    record_prepare.load()

    assert record_prepare.events == [("construct", _ST_MODEL)]


def test_an_unresolvable_candidate_does_not_skip_the_next(monkeypatch, record_prepare):
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "~unresolvable")
    real_expanduser = Path.expanduser

    def expanduser(self):
        if str(self) == "~unresolvable":
            raise RuntimeError("Could not determine home directory.")
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", expanduser)

    record_prepare.load()

    assert record_prepare.events == [
        ("prepare", record_prepare.hub_cache.resolve()),
        ("construct", _ST_MODEL),
    ]
