"""The write paths that root-level disjointness alone does not protect.

Config validation refuses a read-only root that overlaps a writable one, and for
a while that was the whole argument for guarding only the chunk-mutation
surfaces. It does not hold: a writable root can contain a *symlink* into a
protected root, so neither configured root contains the other and the write
still lands in the vault. These tests pin the refusal at the write instead —
plus the three places where the protection used to be silently dropped rather
than enforced.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memtomem.config import IndexingConfig, Mem2MemConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.source_provenance import refuse_replace_target


def _engine(writable: list[Path], read_only: list[Path]) -> IndexEngine:
    engine = IndexEngine.__new__(IndexEngine)
    engine._config = IndexingConfig(memory_dirs=writable, read_only_memory_dirs=read_only)
    return engine


@pytest.fixture
def vault_behind_a_writable_symlink(tmp_path):
    """Writable ``w`` holding ``w/_fetched -> vault``, with ``vault`` protected.

    The configuration is legal: ``w`` and ``vault`` are disjoint directories, so
    ``check_read_only_roots_disjoint`` accepts it. A writer that derives a
    destination under ``w`` therefore passes every root-level check and still
    replaces a file inside ``vault``.
    """
    writable, vault = tmp_path / "w", tmp_path / "vault"
    writable.mkdir()
    vault.mkdir()
    (writable / "_fetched").symlink_to(vault)
    victim = vault / "note.md"
    victim.write_text("owned by another tool\n", encoding="utf-8")
    return _engine([writable], [vault]), writable / "_fetched" / "note.md", victim


def test_the_config_accepts_a_writable_root_that_links_into_a_protected_one(
    vault_behind_a_writable_symlink,
):
    """The premise, pinned: if this ever starts raising, the write-site guard
    below is no longer the only thing standing between the writer and the vault,
    and the reasoning in these tests needs revisiting."""
    engine, through_link, victim = vault_behind_a_writable_symlink
    assert through_link.resolve() == victim.resolve()


def test_refuse_replace_target_refuses_through_a_symlinked_parent(
    vault_behind_a_writable_symlink,
):
    """The target itself is an ordinary file; its *parent* is the link.

    ``refuse_replace_target``'s symlink arm only looks at the target, so this
    case reaches the predicates — and ``is_read_only_source`` resolves the whole
    path, which is what makes the answer right.
    """
    engine, through_link, _victim = vault_behind_a_writable_symlink

    assert refuse_replace_target(through_link, engine) == "read_only"


def test_refuse_replace_target_still_allows_an_ordinary_writable_target(tmp_path):
    """The control: a third reason must not turn the guard into a blanket no."""
    writable, vault = tmp_path / "w", tmp_path / "vault"
    writable.mkdir()
    vault.mkdir()
    target = writable / "note.md"
    target.write_text("ours\n", encoding="utf-8")

    assert refuse_replace_target(target, _engine([writable], [vault])) is None


def test_a_symlinked_target_is_still_reported_as_a_symlink(tmp_path):
    """Order matters: for a link the write replaces the link, so what the
    predicates say about its destination is not what the write would do. The
    read-only arm must not steal that refusal's label (#2488)."""
    writable, vault = tmp_path / "w", tmp_path / "vault"
    writable.mkdir()
    vault.mkdir()
    (vault / "real.md").write_text("theirs\n", encoding="utf-8")
    link = writable / "link.md"
    link.symlink_to(vault / "real.md")

    assert refuse_replace_target(link, _engine([writable], [vault])) == "symlink"


def test_upload_promotion_refuses_a_protected_candidate_by_name(tmp_path):
    """``promote_no_overwrite`` asks about each candidate *before* linking.

    Raising rather than trying the next name is the same rule the excluded arm
    follows: probing further would keep hunting for a writable spelling inside a
    directory whose bytes belong to another tool.
    """
    from memtomem.source_provenance import ReadOnlySourceError
    from memtomem.web.upload_quarantine import promote_no_overwrite

    vault = tmp_path / "vault"
    vault.mkdir()
    source = tmp_path / "quarantine.md"
    source.write_text("uploaded\n", encoding="utf-8")

    with pytest.raises(ReadOnlySourceError):
        promote_no_overwrite(
            source, vault, "notes.md", index_guard=_engine([tmp_path / "w"], [vault])
        )

    # Nothing was linked: the refusal happens before the first os.link.
    assert not (vault / "notes.md").exists()
    assert source.exists()


def _upload_app(read_only: object):
    """A web app whose engine answers ``read_only`` for ``is_read_only_source``."""
    from unittest.mock import AsyncMock, MagicMock

    from memtomem.web.app import create_app
    from memtomem.web.deps import require_configured

    app = create_app(lifespan=None, mode="dev")
    engine = AsyncMock()
    engine.is_excluded = MagicMock(return_value=False)
    engine.is_read_only_source = MagicMock(
        side_effect=read_only if callable(read_only) else lambda _p: read_only
    )
    app.state.index_engine = engine
    app.state.config = Mem2MemConfig()
    app.state.storage = AsyncMock()
    app.state.search_pipeline = MagicMock()
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def test_a_protected_upload_dir_is_refused_before_anything_is_created(tmp_path, monkeypatch):
    """The whole request is refused before the destination is touched.

    ``quarantine_uploads`` chmods the upload directory, creates a
    ``.quarantine-*`` subdirectory inside it and writes the uploaded bytes
    there — all before promotion. Refusing only at promotion time would leave a
    protected directory modified by a request that then declined to write.
    """
    from helpers import set_home
    from httpx import ASGITransport, AsyncClient

    set_home(monkeypatch, tmp_path)
    upload_dir = Path("~/.memtomem/uploads").expanduser()

    # The REAL predicate, protecting exactly the upload directory. Mocking it
    # true — which this test used to do — cannot catch a containment rule that
    # fails to see a root as under itself, which is what it did.
    app = _upload_app(_engine([tmp_path / "w"], [upload_dir]).is_read_only_source)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload", files=[("files", ("notes.md", b"plain\n", "text/markdown"))]
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("read_only_target")
    # Not created, not chmodded, not quarantined into.
    assert not upload_dir.exists()


async def test_a_protected_candidate_name_is_reported_per_file(tmp_path, monkeypatch):
    """The per-file arm still matters when the *directory* is writable.

    ``promote_no_overwrite`` asks about each candidate name, so a protected
    candidate raises ``ReadOnlySourceError`` mid-batch. Reaching the route's
    ``except Exception`` arm would log a traceback and answer "Upload processing
    failed", telling the user to report a bug about their own configuration.
    """
    from helpers import set_home
    from httpx import ASGITransport, AsyncClient

    set_home(monkeypatch, tmp_path)

    # Writable directory, protected file candidates.
    app = _upload_app(lambda p: Path(p).suffix == ".md")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload", files=[("files", ("notes.md", b"plain\n", "text/markdown"))]
        )

    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["files"]
    assert result["error"] == "source_read_only"
    assert result["indexed_chunks"] == 0


# ---------------------------------------------------------------------------
# Which path a symlink puts in the root: the entry, or what it points at
# ---------------------------------------------------------------------------


def test_a_link_living_inside_a_protected_root_is_protected(tmp_path):
    """The mirror of the parent-symlink case, and the one resolution gets wrong.

    ``os.replace`` and ``unlink`` act on the directory *entry*. An entry inside
    the vault whose target is outside it resolves out of the root, so a
    resolve-only rule calls it unprotected — and the write then replaces or
    removes a file sitting in the vault.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault, outside = tmp_path / "vault", tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    real = outside / "target.md"
    real.write_text("lives outside\n", encoding="utf-8")
    entry = vault / "block.md"
    entry.symlink_to(real)

    assert entry.resolve() == real.resolve(), "fixture must actually point out of the root"
    assert is_under_any_root(entry, [vault])


def test_a_link_pointing_into_a_protected_root_is_still_protected(tmp_path):
    """The case the resolving half covers. Kept beside its mirror so a future
    change cannot fix one direction by breaking the other."""
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault, writable = tmp_path / "vault", tmp_path / "w"
    vault.mkdir()
    writable.mkdir()
    (writable / "into").symlink_to(vault)

    assert is_under_any_root(writable / "into" / "note.md", [vault])


def test_a_link_outside_pointing_into_a_protected_root_is_protected(tmp_path):
    """The third arrangement, and the one only the *resolving* half catches.

    The entry lives outside the vault, so the entry check says nothing; what it
    points at is inside. A write that goes through ``open()`` — which is how the
    chunk-mutation path rewrites a source — follows the link and lands on the
    protected bytes. Pinned separately from the other two so removing either
    half of the rule fails a test.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault, writable = tmp_path / "vault", tmp_path / "w"
    vault.mkdir()
    writable.mkdir()
    real = vault / "real.md"
    real.write_text("theirs\n", encoding="utf-8")
    link = writable / "link.md"
    link.symlink_to(real)

    assert is_under_any_root(link, [vault])


def test_the_protected_root_itself_is_protected(tmp_path):
    """``norm_dir_prefix`` ends in a separator, so it cannot match the bare root.

    Callers do ask about the directory — the upload route checks its whole
    destination before creating anything inside it — and without this, protecting
    exactly that directory protected every file in it except the act of
    preparing it.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault = tmp_path / "vault"
    vault.mkdir()

    assert is_under_any_root(vault, [vault])
    assert is_under_any_root(Path(str(vault) + os.sep), [vault])


def _folds_case(tmp_path) -> bool:
    """Ask the filesystem directly — never the code under test.

    A skip guard that calls the containment rule lets the very regression it
    guards certify itself: the bug makes the rule answer "case-sensitive", the
    test skips, and the suite stays green.
    """
    (tmp_path / "CaseProbe").mkdir()
    return (tmp_path / "caseprobe").exists()


def test_a_case_alias_of_a_protected_root_is_protected(tmp_path):
    """Skipped where folding would be a wrong answer rather than a cautious one.

    On a case-sensitive filesystem ``/vault`` and ``/Vault`` are two directories
    with two inodes, so the identity comparison answers no on its own and
    nothing anybody left unprotected is refused.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    if not _folds_case(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault = tmp_path / "Vault"
    vault.mkdir()

    assert (tmp_path / "vault").resolve().samefile(vault), "fixture assumption"
    assert is_under_any_root(tmp_path / "vault", [vault])
    assert is_under_any_root(tmp_path / "VAULT" / "note.md", [vault])
    # Still not a blanket refusal: a prefix-sharing sibling stays writable.
    assert not is_under_any_root(tmp_path / "Vault2" / "note.md", [vault])


def test_two_directories_with_one_name_are_told_apart_by_inode(tmp_path):
    """The control the old rule could not express.

    A filesystem property answered once for a whole path cannot distinguish two
    directories that merely share a name. Identity can: only the configured root
    has the root's inode, so a same-named directory elsewhere is not protected
    however the filesystem treats case.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    protected = tmp_path / "a" / "Vault"
    protected.mkdir(parents=True)
    impostor = tmp_path / "b" / "Vault"
    impostor.mkdir(parents=True)

    assert is_under_any_root(protected / "note.md", [protected])
    assert not is_under_any_root(impostor / "note.md", [protected]), (
        "a directory that only shares the root's name was treated as the root"
    )


def test_a_root_whose_name_carries_no_case_is_still_protected(tmp_path):
    """The root's own spelling stops mattering once identity decides.

    Re-spelling the root's name meant `Vault/2024` — an ordinary year-named
    directory — had nothing to flip, so the probe gave up and the root went
    unprotected against `vAULT/2024`.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    if not _folds_case(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault = tmp_path / "Vault" / "2024"
    vault.mkdir(parents=True)

    assert is_under_any_root(tmp_path / "vAULT" / "2024" / "note.md", [vault]), (
        "a root with no cased component of its own went unprotected"
    )


def test_a_root_that_does_not_exist_yet_is_covered_only_literally(tmp_path):
    """The one deliberate narrowing, pinned so it cannot happen by accident.

    A directory that does not exist has no inode, so nothing can be asked about
    it. The literal prefix still covers the spelling the user configured; a
    differently cased spelling of an **uncreated** root is no longer refused.

    What this replaced guessed the answer from a neighbouring directory — and
    on the arm that ran when the name had no case to flip, guessed it by
    creating a file inside a directory memtomem had promised not to write into.
    Losing the guess is the price of never writing there.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    if not _folds_case(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault = tmp_path / "Vault"  # deliberately not created

    assert is_under_any_root(vault / "note.md", [vault]), (
        "the configured spelling must stay covered by the literal prefix rule"
    )
    assert not is_under_any_root(tmp_path / "VAULT" / "note.md", [vault]), (
        "an uncreated root gained case-alias protection; the narrowing is undeclared"
    )

    # Once it exists it has an identity, and the alias is covered again.
    vault.mkdir()
    assert is_under_any_root(tmp_path / "VAULT" / "note.md", [vault])


def test_the_containment_rule_creates_nothing_and_lists_nothing(tmp_path):
    """Replaces the probe-era pins for cleanup and for not reading entries.

    Both are now structural rather than maintained: there is no arm that writes
    and none that lists. Pinned at the surface anyway, because the reason those
    arms existed — a protected root is usually OS-writable, so nothing stops a
    check from modifying it — has not gone away.
    """
    from memtomem.storage import sqlite_helpers as helpers

    vault = (tmp_path / "Vault").resolve()
    vault.mkdir()
    os.chmod(vault, 0o755)

    created: list[str] = []
    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    def refuse_listing(self, *args, **kwargs):
        raise AssertionError(f"the containment rule listed {self}")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", spy_open)
        patch.setattr(Path, "iterdir", refuse_listing)
        helpers.is_under_any_root(tmp_path / "elsewhere" / "note.md", [vault])
        helpers.is_under_any_root(vault / "note.md", [vault])

    assert created == [], f"the containment rule created files: {created}"
    assert sorted(p.name for p in vault.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_root_that_accepts_no_new_entries_is_still_protected(tmp_path):
    """Refusing the write cannot mean refusing the protection.

    The probe needed somewhere to create a file; where nothing on the device
    would accept one it answered "case-sensitive" and folding was dropped
    entirely, so an alias of the protected root passed the guard. Identity needs
    no such favour.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; mode bits do not deny")
    if not _folds_case(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    from memtomem.storage.sqlite_helpers import is_under_any_root

    sealed = tmp_path / "Sealed"
    sealed.mkdir()
    os.chmod(sealed, 0o555)
    try:
        assert is_under_any_root(tmp_path / "sEALED" / "note.md", [sealed])
    finally:
        os.chmod(sealed, 0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_an_unreadable_root_does_not_break_checks_for_other_targets(tmp_path):
    """A stat failure must answer, not raise.

    `Path.exists()` and `Path.stat()` propagate `PermissionError`; only
    ENOENT-ish errors are swallowed (measured on CPython 3.12: errno 13 under a
    mode-000 parent). One unreadable protected root used to make every
    write-target check raise, including checks about unrelated writable paths —
    a protection feature taking down ordinary writes.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; mode bits do not deny")

    from memtomem.storage.sqlite_helpers import is_under_any_root

    locked = tmp_path / "Locked"
    locked.mkdir()
    unreadable_root = locked / "Vault"
    writable = tmp_path / "w"
    writable.mkdir()
    plain = writable / "note.md"
    plain.write_text("ours\n", encoding="utf-8")

    os.chmod(locked, 0o000)
    try:
        assert not is_under_any_root(plain, [unreadable_root])
    finally:
        os.chmod(locked, 0o755)


def test_a_link_living_inside_a_protected_root_is_still_protected(tmp_path):
    """Symlink handling survives the replacement, and is why two spellings exist.

    `os.replace` and `unlink` act on the directory entry, so a link that *lives*
    in the protected root is protected even though it points out — the resolved
    spelling says the file is elsewhere, and the entry spelling is what catches
    it. Kept as its own pin because the identity arm reads both spellings and a
    regression there would be silent.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault, outside = tmp_path / "Vault", tmp_path / "out"
    vault.mkdir()
    outside.mkdir()
    (outside / "real.md").write_text("theirs\n", encoding="utf-8")
    (vault / "link.md").symlink_to(outside / "real.md")

    assert is_under_any_root(vault / "link.md", [vault])


def test_the_entry_spelling_reaches_the_identity_arm_too(tmp_path):
    """Both spellings must be offered to the identity comparison, not just one.

    A link that *lives* in the protected root and points out is caught by the
    entry spelling — parent resolved, leaf left literal — because `os.replace`
    and `unlink` act on that entry. Spell the root's own directory differently
    as well and the literal prefix no longer matches either spelling, so the
    identity arm is the only thing left. Testing the link alone cannot show
    this: the literal prefix catches it before identity is consulted.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    if not _folds_case(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault, outside = tmp_path / "Vault", tmp_path / "out"
    vault.mkdir()
    outside.mkdir()
    (outside / "real.md").write_text("theirs\n", encoding="utf-8")
    (vault / "link.md").symlink_to(outside / "real.md")

    # Resolved, this is `.../out/real.md` — outside the root. Only the entry
    # spelling places it in the vault, and only by identity, since `vAULT` is
    # not a literal prefix match for `Vault`.
    through_alias = tmp_path / "vAULT" / "link.md"
    assert through_alias.resolve() == (outside / "real.md").resolve(), "fixture assumption"

    assert is_under_any_root(through_alias, [vault]), (
        "the entry spelling never reached the identity comparison"
    )


def test_an_ordinary_path_outside_every_root_is_not_protected(tmp_path):
    """The control for both halves: checking two spellings must not start
    refusing paths that are simply elsewhere."""
    from memtomem.storage.sqlite_helpers import is_under_any_root

    vault, writable = tmp_path / "vault", tmp_path / "w"
    vault.mkdir()
    writable.mkdir()
    plain = writable / "note.md"
    plain.write_text("ours\n", encoding="utf-8")

    assert not is_under_any_root(plain, [vault])
    assert not is_under_any_root(writable / "does-not-exist-yet.md", [vault])


# ---------------------------------------------------------------------------
# Pinned context store
# ---------------------------------------------------------------------------


def test_pinned_set_and_delete_refuse_a_protected_destination(tmp_path):
    """Neither needs a symlink: the pinned path is *derived*, so a protected
    directory can be written without ever appearing in a writable root list.

    Reads stay total (the #2322 / #1768 rule), so only the two mutating methods
    refuse — that asymmetry is the point and is asserted here too.
    """
    from memtomem.errors import ConfigError
    from memtomem.pinned import PinnedContextStore

    user_base = tmp_path / "memories"
    (user_base / "pinned").mkdir(parents=True)
    # A *validated* configuration: the protected root is a project-local tier,
    # disjoint from the registered writable root. An earlier version of this
    # test protected ``user_base / "pinned"`` and called it legal — it is not,
    # the validator refuses a root inside a writable one, and only assignment
    # bypassing validation made it look fine. The derived-tier case below is the
    # real scenario: pinned resolves into a directory nobody registered.
    project = tmp_path / "proj"
    protected = project / ".memtomem" / "memories.local"
    (protected / "pinned").mkdir(parents=True)
    config = Mem2MemConfig()
    config.indexing = IndexingConfig(
        memory_dirs=[user_base],
        project_memory_dirs=[project / ".memtomem" / "memories"],
        read_only_memory_dirs=[protected],
    )
    store = PinnedContextStore(config, project_root=project)

    # ``project_local`` derives into the protected tier, which appears in no
    # writable root list — the bypass root-level disjointness cannot see.
    with pytest.raises(ConfigError, match="read_only_target"):
        store.set("b1", "content", scope="project_local")
    with pytest.raises(ConfigError, match="read_only_target"):
        store.delete("b1", scope="project_local")

    # Nothing was written.
    assert list((protected / "pinned").iterdir()) == []
    # The writable tier is untouched by the refusal.
    assert store.set("ok", "content", scope="user").source_path.exists()
    # And the reads that ``mem_context_compose`` depends on still answer rather
    # than raising — the asymmetry the helper's docstring claims.
    assert [b.block_id for b in store.list()] == ["ok"]
    assert store.get("b1", scope="project_local") is None
    assert store.search_exclusion_roots() is not None


def test_pinned_set_still_writes_an_unprotected_destination(tmp_path):
    """The control: a configured-but-unrelated read-only root must not freeze
    the pinned store."""
    from memtomem.pinned import PinnedContextStore

    user_base = tmp_path / "memories"
    (user_base / "pinned").mkdir(parents=True)
    (tmp_path / "vault").mkdir()
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [user_base]
    config.indexing.read_only_memory_dirs = [tmp_path / "vault"]
    store = PinnedContextStore(config)

    block = store.set("b1", "content", scope="user")

    assert block.source_path.exists()


# ---------------------------------------------------------------------------
# Fail-closed config loading
# ---------------------------------------------------------------------------


def _write_config(home: Path, payload: dict) -> None:
    cfg_dir = home / ".memtomem"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_an_invalid_indexing_section_that_asked_for_protection_refuses_startup(
    tmp_path, monkeypatch
):
    """Tolerating this section would *remove* protection the user asked for.

    The loader normally warns and ignores a rejected section, which restores the
    previous value — an empty read-only list — leaving the writable roots intact
    and the process writing to the directory the user tried to protect. Silence
    in that direction is the failure, so this one request is fail-closed.
    """
    from memtomem.config import load_config_overrides
    from memtomem.errors import ConfigError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shared = tmp_path / "memories"
    shared.mkdir()
    # Overlapping: the same directory as both writable and protected.
    _write_config(
        tmp_path,
        {"indexing": {"memory_dirs": [str(shared)], "read_only_memory_dirs": [str(shared)]}},
    )

    config = Mem2MemConfig()
    with pytest.raises(ConfigError, match="read_only_memory_dirs"):
        load_config_overrides(config)


def _write_fragment(home: Path, name: str, payload: dict) -> None:
    frag_dir = home / ".memtomem" / "config.d"
    frag_dir.mkdir(parents=True, exist_ok=True)
    (frag_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_a_config_d_fragment_asking_for_protection_also_fails_closed(tmp_path, monkeypatch):
    """The fragment loader is a separate code path from ``config.json``.

    Both are tolerant, so both had to be taught this, and only a test per path
    proves the second one was not forgotten.
    """
    from memtomem.config import load_config_d
    from memtomem.errors import ConfigError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shared = tmp_path / "memories"
    shared.mkdir()
    _write_fragment(
        tmp_path,
        "10-overlap.json",
        {"indexing": {"memory_dirs": [str(shared)], "read_only_memory_dirs": [str(shared)]}},
    )

    with pytest.raises(ConfigError, match="read_only_memory_dirs"):
        load_config_d(Mem2MemConfig())


def test_a_malformed_protection_value_fails_closed_before_validation(tmp_path, monkeypatch):
    """A non-list value is dropped during per-key merging, *before* the section
    is validated — so the section-level arm never sees it.

    Left tolerant, a typo like ``"read_only_memory_dirs": "/vault"`` would start
    a server with no protection and only a warning in the log, which is the
    exact outcome fail-closed exists to prevent.
    """
    from memtomem.config import load_config_d
    from memtomem.errors import ConfigError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_fragment(
        tmp_path, "10-malformed.json", {"indexing": {"read_only_memory_dirs": str(vault)}}
    )

    with pytest.raises(ConfigError, match="must be a list"):
        load_config_d(Mem2MemConfig())


def test_an_unrelated_invalid_fragment_does_not_discard_earlier_protection(tmp_path, monkeypatch):
    """Regression test for an overreach the fail-closed rule introduced.

    The decision keys on *this layer's* keys, not on the accumulated section.
    Keyed on the accumulated payload, a later fragment with an unrelated typo
    raised ``ConfigError`` — it never mentioned ``read_only_memory_dirs``, and
    rolling it back would have preserved the protection an earlier fragment had
    legitimately installed. Fail-closed must protect the request, not convert
    every tolerated typo into a dead server.
    """
    from memtomem.config import load_config_d

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    writable, vault = tmp_path / "memories", tmp_path / "vault"
    writable.mkdir()
    vault.mkdir()
    _write_fragment(
        tmp_path,
        "05-protect.json",
        {"indexing": {"memory_dirs": [str(writable)], "read_only_memory_dirs": [str(vault)]}},
    )
    # Invalid, and about something else entirely.
    _write_fragment(
        tmp_path, "10-bad.json", {"indexing": {"min_chunk_tokens": 999, "max_chunk_tokens": 10}}
    )

    config = Mem2MemConfig()
    load_config_d(config)  # must not raise

    assert [Path(p) for p in config.indexing.read_only_memory_dirs] == [vault]


@pytest.mark.parametrize(
    "value,label",
    [(None, "null"), (False, "false"), ("", "empty string"), ({}, "object"), ("/vault", "string")],
)
def test_a_falsey_or_malformed_protection_value_fails_closed(tmp_path, monkeypatch, value, label):
    """``null``, ``false``, ``""`` and ``{}`` are malformed, not "asked for nothing".

    The first fail-closed rule keyed on truthiness, so all four slipped through
    and the process ran with protection silently empty — the outcome the rule
    exists to prevent, reached by a different door.
    """
    from memtomem.config import load_config_overrides
    from memtomem.errors import ConfigError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "memories").mkdir()
    _write_config(tmp_path, {"indexing": {"read_only_memory_dirs": value}})

    with pytest.raises(ConfigError):
        load_config_overrides(Mem2MemConfig())


def test_an_explicitly_empty_protection_list_is_accepted(tmp_path, monkeypatch):
    """``[]`` is the one falsey value that genuinely means "no read-only roots".

    The control for the parametrised refusals above: without it, "fail closed on
    anything falsey" would look equally correct and would break every config
    that writes the field out explicitly.
    """
    from memtomem.config import load_config_overrides

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "memories").mkdir()
    _write_config(tmp_path, {"indexing": {"read_only_memory_dirs": []}})

    config = Mem2MemConfig()
    load_config_overrides(config)  # must not raise

    assert config.indexing.read_only_memory_dirs == []


def test_an_invalid_section_without_a_protection_request_stays_tolerant(tmp_path, monkeypatch):
    """The control, and the reason this is not just ``strict=True``.

    One bad key must still not stop the server. Only a rejected section that
    asked for read-only roots is fatal; everything else keeps the tolerance the
    loader exists to provide.
    """
    from memtomem.config import load_config_overrides

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # ``min_chunk_tokens > max_chunk_tokens`` is a cross-field invariant failure.
    _write_config(tmp_path, {"indexing": {"min_chunk_tokens": 999, "max_chunk_tokens": 10}})

    config = Mem2MemConfig()
    load_config_overrides(config)  # must not raise

    assert config.indexing.min_chunk_tokens == 128  # the default, section ignored


# ---------------------------------------------------------------------------
# mm memory doctor --fix
# ---------------------------------------------------------------------------


def test_doctor_fix_refuses_a_protected_root_instead_of_rewriting_its_index(tmp_path):
    """``--fix`` rewrites the provider index file, which is the write these
    roots exist to prevent. The root is still returned, not dropped: the caller
    asked for a repair that will not happen."""
    from memtomem.cli.memory_doctor_cmd import _collect_fixable

    claude = tmp_path / ".claude" / "projects" / "proj" / "memory"
    claude.mkdir(parents=True)
    (claude / "MEMORY.md").write_text("- [x](gone.md)\n", encoding="utf-8")

    fixable, refused = _collect_fixable([claude], read_only_roots=[claude])
    assert fixable == []
    assert refused == [claude.resolve()]

    # Control: the same dir is fixable when it is not protected.
    fixable, refused = _collect_fixable([claude], read_only_roots=[])
    assert [row[0] for row in fixable] == [claude.resolve()]
    assert refused == []


def test_doctor_fix_refusal_is_not_defeated_by_a_symlinked_root(tmp_path):
    """Same resolve-the-whole-path rule as the write guard: the inspected dir may
    be spelled through a link the protected root does not literally contain."""
    from memtomem.cli.memory_doctor_cmd import _collect_fixable

    real = tmp_path / "real" / ".claude" / "projects" / "proj" / "memory"
    real.mkdir(parents=True)
    (real / "MEMORY.md").write_text("- [x](gone.md)\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "real")
    aliased_dir = alias / ".claude" / "projects" / "proj" / "memory"

    fixable, refused = _collect_fixable([aliased_dir], read_only_roots=[real])
    assert fixable == []
    assert refused == [aliased_dir.resolve()]
