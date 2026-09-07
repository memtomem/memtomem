"""#2322 — ``require_user_base`` must actually return the *user* tier.

ADR-0011 §5 gives a ``project_shared`` write two gates: Gate A scans the
content, Gate B takes an explicit confirmation and records
``project_shared.confirmed_via=``. A whole family of writers never takes a
destination from the caller — the session-end summary archive, the Notion /
Obsidian importers, ``mem_fetch``, scratch promote, ``mm review approve``,
``mm agent share``, ``mm shell``'s ``add`` — they derive it from
``indexing.memory_dirs[0]``. They therefore have no confirmation argument and
no human to ask, which is exactly why the tier of that base has to be settled
before they write: ``memory_dirs`` and ``project_memory_dirs`` can overlap, and
nothing in config validation forbids it.

The registration check is load-bearing here rather than incidental: the
*default* user directory ``~/.memtomem/memories`` matches the project path
pattern at a positive offset, so a classification that skipped registration
would refuse every default install. That is the
``test_default_user_dir_shaped_like_a_project_path_passes`` pin below.
"""

from __future__ import annotations

import pytest

from memtomem.errors import ConfigError
from memtomem.memory_scope import EMPTY_MEMORY_DIRS_ERROR, require_user_base


def _tier(tmp_path, name: str = "memories"):
    """A canonical ``<project>/.memtomem/<name>`` directory."""
    d = tmp_path / "proj" / ".memtomem" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_plain_user_dir_passes(tmp_path):
    base = tmp_path / "memories"
    base.mkdir()
    assert require_user_base([base], []) == base.resolve()


def test_default_user_dir_shaped_like_a_project_path_passes(tmp_path):
    """``~/.memtomem/memories`` matches the project pattern; registration is
    the only thing that separates it from a real project tier.

    If this helper classified with ``project_memory_dirs=None`` — the mode
    that skips the registration check — the *default* install would refuse.
    """
    home_like = _tier(tmp_path)  # <root>/proj/.memtomem/memories
    assert require_user_base([home_like], []) == home_like.resolve()


def test_project_shaped_but_unregistered_passes(tmp_path):
    """Registration governs: an unregistered sibling tier does not make the
    configured base a project tier."""
    base = _tier(tmp_path)
    other = tmp_path / "other" / ".memtomem" / "memories"
    other.mkdir(parents=True)
    assert require_user_base([base], [other]) == base.resolve()


def test_registered_project_shared_base_refuses(tmp_path):
    base = _tier(tmp_path)
    with pytest.raises(ConfigError) as exc:
        require_user_base([base], [base])
    message = str(exc.value)
    # Positive assertions: the operator has to be able to act on this without
    # reading the source. Both config fields, the tier, and the path.
    assert "indexing.memory_dirs[0]" in message
    assert "indexing.project_memory_dirs" in message
    assert "project_shared" in message
    assert str(base.resolve()) in message
    assert "ADR-0011" in message


def test_registered_project_local_base_refuses(tmp_path):
    base = _tier(tmp_path, "memories.local")
    with pytest.raises(ConfigError) as exc:
        require_user_base([base], [base])
    assert "project_local" in str(exc.value)


def test_project_local_refusal_does_not_claim_git_or_gate_b(tmp_path):
    """``project_local`` is gitignored and has no Gate B, so the shared
    tier's rationale is simply false there.

    Told the wrong reason, an operator goes looking for a confirmation flag
    that does not exist on these surfaces or on that tier.
    """
    shared = _tier(tmp_path, "memories")
    local = _tier(tmp_path, "memories.local")

    with pytest.raises(ConfigError) as shared_exc:
        require_user_base([shared], [shared])
    with pytest.raises(ConfigError) as local_exc:
        require_user_base([local], [local])

    shared_msg, local_msg = str(shared_exc.value), str(local_exc.value)

    # The shared tier keeps the git / Gate B rationale …
    assert "git-tracked" in shared_msg
    assert "ADR-0011 §5" in shared_msg
    # … and project_local must not borrow it.
    assert "git-tracked" not in local_msg
    assert "ADR-0011" not in local_msg
    # It states its own reason instead, rather than merely omitting the wrong one.
    assert "a default user-scope read does not see" in local_msg
    # Both still name the way out.
    for msg in (shared_msg, local_msg):
        assert "indexing.project_memory_dirs" in msg


def test_refusal_looks_past_the_first_registered_entry(tmp_path):
    """The overlap does not have to be the first ``project_memory_dirs``
    entry — a scan that only compared index 0 would pass this."""
    base = _tier(tmp_path)
    unrelated = tmp_path / "elsewhere" / ".memtomem" / "memories"
    unrelated.mkdir(parents=True)
    with pytest.raises(ConfigError):
        require_user_base([base], [unrelated, base])


def test_only_the_first_memory_dir_decides(tmp_path):
    """A project tier *later* in ``memory_dirs`` is not the write target, so
    it does not refuse — only ``memory_dirs[0]`` is the derived base."""
    user = tmp_path / "memories"
    user.mkdir()
    proj = _tier(tmp_path)
    assert require_user_base([user, proj], [proj]) == user.resolve()


def test_empty_memory_dirs_still_names_the_config_field(tmp_path):
    """#1768's refusal survives — the new check runs after it, not instead."""
    with pytest.raises(ConfigError) as exc:
        require_user_base([], [])
    assert str(exc.value) == EMPTY_MEMORY_DIRS_ERROR


def test_allow_project_tier_defaults_to_refusing(tmp_path):
    """The escape hatch errs toward protection: omitting it refuses.

    A caller who forgets the flag gets the guard, not a silent bypass —
    the opposite of what a defaulted ``project_memory_dirs`` would have
    done, and the reason this one is safe to have at all.
    """
    base = _tier(tmp_path)
    with pytest.raises(ConfigError):
        require_user_base([base], [base])
    assert require_user_base([base], [base], allow_project_tier=True) == base.resolve()


def test_allow_project_tier_is_keyword_only():
    """Positionally reachable, it would be one argument-order slip from
    disabling the guard at a call site nobody edited."""
    import inspect

    param = inspect.signature(require_user_base).parameters["allow_project_tier"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_allow_project_tier_still_refuses_an_empty_memory_dirs(tmp_path):
    """It waives the tier check, not the #1768 refusal — there is no base
    to hand back at all."""
    with pytest.raises(ConfigError) as exc:
        require_user_base([], [], allow_project_tier=True)
    assert str(exc.value) == EMPTY_MEMORY_DIRS_ERROR


def test_project_memory_dirs_is_required():
    """Not a defaulted parameter: a caller that omits it fails loudly rather
    than silently opting out of the guard."""
    with pytest.raises(TypeError):
        require_user_base(["/tmp/memories"])  # type: ignore[call-arg]
