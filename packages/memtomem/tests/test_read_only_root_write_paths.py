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
from types import SimpleNamespace

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


def test_a_case_alias_of_a_protected_root_is_protected_when_the_fs_folds_case(tmp_path):
    """Skipped where it would be a wrong answer rather than a cautious one.

    On a case-sensitive filesystem ``/vault`` and ``/Vault`` really are two
    directories; folding there would refuse writes to a directory nobody
    protected. So the probe decides, and this test asks the same question the
    production code does rather than assuming a platform.
    """
    from memtomem.storage.sqlite_helpers import _root_is_case_insensitive, is_under_any_root

    vault = tmp_path / "Vault"
    vault.mkdir()
    if not _root_is_case_insensitive(str(vault.resolve())):
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    assert (tmp_path / "vault").resolve().samefile(vault), "fixture assumption"
    assert is_under_any_root(tmp_path / "vault", [vault])
    assert is_under_any_root(tmp_path / "VAULT" / "note.md", [vault])
    # Still not a blanket refusal: a prefix-sharing sibling stays writable.
    assert not is_under_any_root(tmp_path / "Vault2" / "note.md", [vault])


def test_a_root_configured_before_it_exists_still_folds_case(tmp_path):
    """A read-only root may be declared before the directory is synced in.

    The probe cannot ask a path that does not exist, and answering
    "case-sensitive" for it left every differently cased spelling of the root
    unprotected — for the life of the process, because that non-answer was
    cached. The question now goes to the nearest existing directory, which is
    the one the root will be created inside.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    # Asked of the filesystem directly, not of the code under test: the bug being
    # pinned makes that code answer "case-sensitive" here, which would turn this
    # test's failure into a skip.
    (tmp_path / "CaseProbe").mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault = tmp_path / "Vault"
    alias = tmp_path / "VAULT" / "note.md"

    assert is_under_any_root(alias, [vault]), (
        "a root that does not exist yet answered case-sensitive; the alias bypassed protection"
    )
    vault.mkdir()
    assert is_under_any_root(alias, [vault]), (
        "the pre-creation non-answer was cached; the alias bypassed protection"
    )


def test_a_root_whose_name_carries_no_case_is_still_protected(tmp_path):
    """The probe must not depend on the root's own name having a case.

    Re-spelling the root's name meant `Vault/2024` — a perfectly ordinary
    year-named directory — had nothing to flip, so the probe gave up and the
    root went unprotected against `vAULT/2024`. The probe now writes a name it
    chooses itself, so the root's spelling stops mattering.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    (tmp_path / "CaseProbe").mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("filesystem is case-sensitive; the alias is a different directory")

    vault = tmp_path / "Vault" / "2024"
    vault.mkdir(parents=True)
    alias = tmp_path / "vAULT" / "2024" / "note.md"

    assert is_under_any_root(alias, [vault]), (
        "a root with no cased component of its own went unprotected"
    )


def test_the_probe_does_not_need_the_directorys_name_to_carry_a_case(tmp_path):
    """Pinned on the helper, because the walk above would mask it.

    `_probe_case_insensitive` climbs when a directory cannot answer, so deriving
    the probe name from the directory still produces the right answer for
    `Vault/2024` — the parent rescues it. That makes the end-to-end test above
    silent about *why* it works. Asked of `_probe_by_writing` directly, a
    directory whose own name has no case must still answer without help.
    """
    from memtomem.storage.sqlite_helpers import _probe_by_writing

    numeric = tmp_path / "2024"
    numeric.mkdir()

    assert _probe_by_writing(numeric) is not None, (
        "the probe name was derived from the directory, which has no case to flip"
    )


def test_the_probe_ignores_what_the_directory_already_contains(tmp_path):
    """Pre-existing entries are not evidence, however they are spelled.

    Re-spelling an entry that was already there cannot tell case folding from
    two names that merely resolve to one file: a hard link, or two symlinks to
    one target, make `Note.md` and `nOTE.MD` the same file on a case-sensitive
    filesystem while the directory still keeps the names apart. Answering from a
    freshly created name makes the trap unreachable — so the answer must not
    move when such entries are present.
    """
    from memtomem.storage.sqlite_helpers import _probe_case_insensitive

    plain = tmp_path / "plain"
    plain.mkdir()
    baseline = _probe_case_insensitive(str(plain))
    assert baseline is not None, "an empty directory could not be asked"

    trapped = tmp_path / "trapped"
    trapped.mkdir()
    target = trapped / "Note.md"
    target.write_text("x\n", encoding="utf-8")
    alias = trapped / "nOTE.MD"
    try:
        os.link(target, alias)
    except FileExistsError:
        # This filesystem folds case, so the two spellings are already one entry
        # and the trap cannot be built. That is the consistent answer, not a gap:
        # the misreading being pinned only exists where the names stay distinct,
        # which is where this assertion's `else` branch runs (Linux CI).
        assert baseline is True
        return
    except (OSError, NotImplementedError):
        alias.symlink_to(target)

    assert _probe_case_insensitive(str(trapped)) is baseline, (
        "a link pair in the directory changed the filesystem's reported case semantics"
    )


def test_a_symlink_cannot_forge_directory_identity(tmp_path):
    """The read-only arm compares directories, which is why it is sound — and
    the one way that identity can still be forged.

    A directory cannot be hard-linked (measured: `EPERM`), so two directory
    names resolving to one inode really are one entry. A *symlink* is the
    exception: it resolves to its target while remaining a separate name, so
    `samefile` would report a fold that the directory does not perform.
    """
    from memtomem.storage.sqlite_helpers import _same_directory

    real = tmp_path / "Real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert _same_directory(real, link) is None, "a symlink was accepted as evidence of folding"
    assert _same_directory(link, real) is None, "a symlink base was accepted as evidence"


def test_a_base_that_is_gone_is_inconclusive_not_case_sensitive(tmp_path):
    """`samefile` raises the same error for both halves of the question.

    `FileNotFoundError` means "the re-spelled alias is absent" — conclusive
    case-sensitive — or "the directory I was asking about has gone", which
    answers nothing. The caller caches what it is told, so reporting the second
    as a filesystem property would leave a root unprotected against alias
    spellings for the life of the process.
    """
    from memtomem.storage.sqlite_helpers import _same_directory

    present = tmp_path / "Present"
    present.mkdir()
    assert _same_directory(present, tmp_path / "definitely-absent") is False

    assert _same_directory(tmp_path / "Vanished", tmp_path / "vANISHED") is None, (
        "a base that does not exist was reported as a filesystem property"
    )


def test_the_probe_never_reads_the_directory_listing(tmp_path):
    """The platform-independent half of the rule above.

    The link trap can only be built on a case-sensitive filesystem — on a
    folding one the two spellings are already one entry — so the test above has
    no witness on macOS or Windows and only bites on Linux CI. This one holds
    everywhere by pinning the mechanism instead of the symptom: if the answer
    never consults the listing, no arrangement of entries can move it.
    """
    from memtomem.storage.sqlite_helpers import _probe_by_writing

    directory = tmp_path / "root"
    directory.mkdir()
    (directory / "Note.md").write_text("x\n", encoding="utf-8")

    def refuse_listing(self, *args, **kwargs):
        raise AssertionError(f"the probe read the directory listing of {self}")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", refuse_listing)
        assert _probe_by_writing(directory) is not None


def test_the_probe_writes_nothing_that_outlives_it(tmp_path):
    """The answer costs one file, and only for the moment it is asked.

    Worth pinning because the directory being probed may be an indexed memory
    root: a leftover would be picked up as content, and a leftover in a
    *protected* root would be the exact thing this feature exists to prevent.
    """
    from memtomem.storage.sqlite_helpers import _probe_by_writing

    directory = tmp_path / "root"
    directory.mkdir()
    before = sorted(p.name for p in directory.iterdir())

    # Asked of the writing arm directly: through `_probe_case_insensitive` the
    # read-only arm answers first and nothing is ever created, so the check
    # would pass without the cleanup it is pinning.
    assert _probe_by_writing(directory) is not None
    assert sorted(p.name for p in directory.iterdir()) == before, (
        f"the probe left a file behind: {sorted(p.name for p in directory.iterdir())}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_read_only_directory_is_climbed_past_not_written_to(tmp_path):
    """The root itself is the one place the probe must not write.

    A read-only root is the normal case for this feature, so refusing the write
    cannot mean refusing the answer: the walk continues to a writable directory
    on the same filesystem. Pinned together because "does not write there" and
    "still answers" are the two halves of the same requirement.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; mode bits do not deny")

    from memtomem.storage.sqlite_helpers import _probe_case_insensitive

    vault = tmp_path / "Vault"
    vault.mkdir()
    os.chmod(vault, 0o555)
    try:
        assert _probe_case_insensitive(str(vault)) is not None, (
            "a read-only root could not be answered by climbing to a writable parent"
        )
        assert sorted(p.name for p in vault.iterdir()) == [], (
            "the probe wrote into the read-only root"
        )
    finally:
        os.chmod(vault, 0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_an_unreadable_root_does_not_break_checks_for_other_targets(tmp_path):
    """`Path.is_dir()` propagates `PermissionError`; only ENOENT-ish is swallowed.

    Measured on CPython 3.12: a stat under a mode-000 parent raises errno 13.
    With the walk's existence check outside the handler, one unreadable
    protected root made every write-target check raise — including checks about
    unrelated writable paths, which is a protection feature taking down
    ordinary writes.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; mode bits do not deny")

    locked = tmp_path / "Locked"
    locked.mkdir()
    unreadable_root = locked / "Vault"
    writable = tmp_path / "w"
    writable.mkdir()
    plain = writable / "note.md"
    plain.write_text("ours\n", encoding="utf-8")

    os.chmod(locked, 0o000)
    try:
        from memtomem.storage.sqlite_helpers import _root_is_case_insensitive, is_under_any_root

        assert _root_is_case_insensitive(str(unreadable_root)) is False
        assert not is_under_any_root(plain, [unreadable_root])
    finally:
        os.chmod(locked, 0o755)


def test_nothing_is_created_inside_a_protected_root_that_the_os_would_allow(tmp_path):
    """The promise is memtomem's, not the mode bits'.

    A read-only root is normally OS-writable — it is protected by this
    application refusing to write, not by the filesystem. So a containment check
    about an unrelated target must not create anything inside it: that would
    change its mtime and raise filesystem events in a directory another tool
    owns, which is the whole thing the setting exists to prevent.
    """
    from memtomem.storage import sqlite_helpers as helpers

    vault = (tmp_path / "Vault").resolve()
    vault.mkdir()
    os.chmod(vault, 0o755)

    created: list[str] = []
    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(str(Path(path).parent.resolve()))
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        # Every directory a tmp_path test builds has a name with a case, so the
        # read-only arm would answer and the writing arm — the one this pin is
        # about — would never run. Silencing it is what reaches the code.
        patch.setattr(helpers, "_probe_by_spelling", lambda directory: None)
        patch.setattr(os, "open", spy_open)
        helpers._CASE_PROBE_CACHE.clear()
        try:
            helpers.is_under_any_root(tmp_path / "elsewhere" / "note.md", [vault])
        finally:
            helpers._CASE_PROBE_CACHE.clear()

    assert created, "the writing arm never ran; this pin proved nothing"
    assert str(vault) not in created, f"the probe wrote inside the protected root: {created}"
    assert sorted(p.name for p in vault.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_root_that_accepts_no_new_entries_is_still_answered(tmp_path):
    """Refusing to write cannot mean refusing to protect.

    When no directory on the filesystem will accept a new entry the writing arm
    has nothing to offer, and treating that silence as "case-sensitive" drops
    folding — so a differently cased spelling of the protected root passes the
    guard. Measured before the read-only arm existed: `folds=False` and the
    alias went unprotected while the control answered `True`. The name-spelling
    arm answers here without creating anything.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; mode bits do not deny")

    from memtomem.storage.sqlite_helpers import _probe_case_insensitive

    from memtomem.storage import sqlite_helpers as helpers

    sealed = tmp_path / "Sealed"
    sealed.mkdir()
    os.chmod(sealed, 0o555)
    try:
        with pytest.MonkeyPatch.context() as patch:
            # The measured case was a protected root on its own mount with no
            # writable directory reachable on that device. Here the writing arm
            # is simply given nothing to offer, which is the same question:
            # can the answer come without creating anything?
            patch.setattr(helpers, "_probe_by_writing", lambda directory: None)
            assert _probe_case_insensitive(str(sealed)) is not None, (
                "a root that accepts no new entries could not be answered at all"
            )
    finally:
        os.chmod(sealed, 0o755)


def test_a_mount_boundary_stops_the_climb_for_a_writable_directory(tmp_path, monkeypatch):
    """Climbing past a read-only root must not leave the filesystem being asked.

    A case-sensitive volume mounted inside a case-insensitive parent would
    otherwise answer "folds case" for the volume, and writes to a genuinely
    distinct `VAULT` would be refused — a wrong answer, not a cautious one.
    """
    from memtomem.storage import sqlite_helpers as helpers

    # A name with no case to flip, so the read-only arm cannot answer here and
    # both walks are forced to consider the parent — which is where the boundary
    # is. With a flippable name the first arm answers and neither walk runs.
    vault = tmp_path / "2024"
    vault.mkdir()

    real_stat = Path.stat

    def stat_across_a_boundary(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self != vault:
            return SimpleNamespace(st_dev=result.st_dev + 1, st_mode=result.st_mode)
        return result

    # The parent *would* answer. Without that asymmetry the guarded and unguarded
    # versions both return `None` and the pin proves nothing.
    monkeypatch.setattr(
        helpers, "_probe_by_writing", lambda directory: None if directory == vault else True
    )
    monkeypatch.setattr(Path, "stat", stat_across_a_boundary)

    assert helpers._probe_case_insensitive(str(vault)) is None, (
        "the climb crossed a mount boundary and adopted another filesystem's answer"
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
