"""``indexing.read_only_memory_dirs``: the configuration contract behind the gate.

Two claims are pinned here because the mutation gates rest on them and neither
is visible from the gate's own code:

* **Disjointness is enforced.** This started out as the *whole* argument for
  guarding only the chunk-mutation surfaces, and it was not sound: a writable
  root can hold a symlink into a protected one, so neither configured root
  contains the other and the write still lands in the vault. Enforcement now
  happens at each write target instead (see
  ``test_read_only_root_write_paths.py`` and the parity guard in
  ``test_read_only_target_gate_parity.py``), so this validator is no longer
  load-bearing for correctness. It is kept because an overlapping configuration
  is almost certainly a mistake, and saying so at load time beats refusing
  every write into a directory the user also listed as writable.
* **Read-only roots are still index roots.** They must appear in
  ``all_index_roots()`` or the watcher, the exclusion check and re-index would
  treat the vault as unowned, which is a different feature (exclusion) wearing
  this one's name.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
from pydantic import ValidationError

from memtomem.config import IndexingConfig


def _cfg(writable: list[Path], read_only: list[Path]) -> IndexingConfig:
    return IndexingConfig(memory_dirs=writable, read_only_memory_dirs=read_only)


@pytest.fixture
def roots(tmp_path):
    """A writable root, a read-only root, a nested dir and a prefix-sharing sibling."""
    layout = {
        "writable": tmp_path / "writable",
        "nested": tmp_path / "writable" / "nested",
        "vault": tmp_path / "vault",
        "vault2": tmp_path / "vault2",
    }
    for p in layout.values():
        p.mkdir(parents=True, exist_ok=True)
    return layout


def test_disjoint_roots_are_accepted(roots):
    cfg = _cfg([roots["writable"]], [roots["vault"]])
    assert cfg.read_only_memory_dirs == [roots["vault"]]


@pytest.mark.parametrize(
    "writable_key,read_only_key,why",
    [
        ("writable", "writable", "identical roots"),
        ("writable", "nested", "read-only nested inside a writable root"),
        ("nested", "writable", "writable nested inside a read-only root"),
    ],
)
def test_an_overlapping_root_is_refused(roots, writable_key, read_only_key, why):
    with pytest.raises(ValidationError, match="overlaps the writable index root"):
        _cfg([roots[writable_key]], [roots[read_only_key]])


def test_a_sibling_sharing_the_prefix_is_not_an_overlap(roots):
    """``/vault`` and ``/vault2`` are disjoint. Without the trailing-separator
    rule in ``norm_dir_prefix`` this pair would be refused as nested (#647)."""
    cfg = _cfg([roots["vault2"]], [roots["vault"]])
    assert cfg.read_only_memory_dirs == [roots["vault"]]


def test_project_memory_dirs_count_as_writable(tmp_path):
    """The validator must read *both* writable lists, not just ``memory_dirs``."""
    project = tmp_path / "proj" / ".memtomem" / "memories"
    project.mkdir(parents=True)
    with pytest.raises(ValidationError, match="overlaps the writable index root"):
        IndexingConfig(
            memory_dirs=[],
            project_memory_dirs=[project],
            read_only_memory_dirs=[project.parent],
        )


def test_overlap_is_detected_through_a_symlinked_alias(tmp_path):
    """Comparison resolves symlinks, so an alias cannot smuggle an overlap past
    the validator by spelling the same directory a second way."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(ValidationError, match="overlaps the writable index root"):
        _cfg([real], [alias])


def test_overlap_is_detected_across_unicode_normalisation(tmp_path):
    """NFC and NFD spellings of one directory are one directory (#235)."""
    nfc = tmp_path / "볼트"
    nfc.mkdir()
    nfd = Path(unicodedata.normalize("NFD", str(nfc)))
    with pytest.raises(ValidationError, match="overlaps the writable index root"):
        _cfg([nfc], [nfd])


def test_a_user_path_is_expanded_before_comparison(tmp_path, monkeypatch):
    """``~/vault`` and its absolute spelling are the same root. Comparing the
    raw ``~`` string would let the overlap through."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows' home variable
    (tmp_path / "vault").mkdir()
    with pytest.raises(ValidationError, match="overlaps the writable index root"):
        _cfg([tmp_path / "vault"], [Path("~/vault")])


def test_read_only_roots_are_index_roots(roots):
    cfg = _cfg([roots["writable"]], [roots["vault"]])
    assert cfg.all_index_roots() == [roots["writable"], roots["vault"]]


def test_the_default_config_declares_no_read_only_roots():
    """Default-off, and the validator must not pay for paths nobody configured:
    with the list empty it returns before resolving anything."""
    assert IndexingConfig().read_only_memory_dirs == []
