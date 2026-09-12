"""The suite must not inherit the developer's ``MEMTOMEM_*`` config env (#2386).

Isolating the ``~/.memtomem`` config *files* is only half of hermeticity. Every
loader yields to the environment — ``load_config_overrides`` skips a
``config.json`` entry whose field an env var owns — so a shell that exports
``MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE`` hands its value to a test that redirected
the whole config layer into a temp directory. That is #2386's failure reproduced
one axis over, and this repo's own ``scripts/setup-worktree.sh`` writes an
``.envrc`` exporting ``MEMTOMEM_*``, so the shell is a realistic source.

``conftest._scrub_ambient_memtomem_env`` removes that env for the session. What
it must *not* remove is the flat ``MEMTOMEM_*`` switches this package reads
straight from the environment: an earlier revision scrubbed by prefix and took
``MEMTOMEM_UPDATE_WIRE_GOLDENS`` with it, disabling the documented golden-file
regeneration workflow without a word.

The pins below cover the selection rule, the hazard it answers, and — from a
child pytest process with the environment actually seeded — that the fixture
does the thing. That last one matters because every in-process pin here passes
vacuously on a clean CI machine, where there was no ambient env to remove.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from memtomem.config_signature import build_fresh_config
from .helpers import (
    MEMTOMEM_ENV_NESTED_DELIMITER,
    MEMTOMEM_ENV_PREFIX,
    ambient_memtomem_config_env,
    isolate_config_paths,
    memtomem_config_section_names,
)

#: Set by the parent test below to enable the canary. Flat, so the scrub the
#: canary is checking cannot switch the canary off.
CANARY_ENV = "MEMTOMEM_TEST_SCRUB_CANARY"

#: ``…/tests`` → ``…/memtomem`` → ``…/packages`` → checkout root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "packages" / "memtomem" / "src"

#: A whole string literal that *is* a ``MEMTOMEM_*`` name. The quotes are the
#: load-bearing part — see :func:`_flat_names_in_src`.
_MEMTOMEM_NAME_LITERAL = re.compile(r"[\"'](MEMTOMEM_[A-Z0-9_]+)[\"']")

#: Flat ``MEMTOMEM_*`` names the *harness* owns. These live in ``tests/``, which
#: cannot be scanned the way ``src/`` is below — this file's own cases spell out
#: section names (``MEMTOMEM_EMBEDDING``, ``MEMTOMEM_STORAGE``) as sample data,
#: and a scan would read those back as switches.
#:
#: ``_MEMTOMEM_TEST_RUNTIME_DIR`` is deliberately absent: it carries a leading
#: underscore, so it is not a ``MEMTOMEM_``-prefixed name at all and the
#: selector could never have taken it.
_HARNESS_FLAT_SWITCHES = frozenset(
    {
        "MEMTOMEM_UPDATE_WIRE_GOLDENS",
        "MEMTOMEM_TEST_HOME_GUARD",
        CANARY_ENV,
    }
)


def _flat_names_in_src() -> frozenset[str]:
    """Every flat ``MEMTOMEM_*`` name the shipped package writes as a literal.

    Read out of ``src/`` rather than written down here, so a switch added
    tomorrow is covered without anyone remembering this file. Flat means "no
    nested delimiter": those are the names that are *not* config bindings, and
    the ones the round-2 prefix sweep was deleting.

    Matching whole string literals — quote to quote — rather than the bare word
    is what keeps this an inventory of *names* instead of an inventory of text.
    Two things it excludes, both of which a bare-word scan collected:

    * Python identifiers that merely end in one. ``_MEMTOMEM_MARKER``,
      ``_MEMTOMEM_NAME_PREFIX``, ``_MEMTOMEM_STATUS_PREFIX``,
      ``_MEMTOMEM_SERVER_IDS`` are module constants, not environment reads, and
      ``_TEST_RUNTIME_DIR_ENV = "_MEMTOMEM_TEST_RUNTIME_DIR"`` is a name whose
      leading underscore puts it outside the prefix entirely — the bare scan
      reported its *tail* as a switch that does not exist.
    * A name mentioned in prose. A docstring that documents
      ``MEMTOMEM_EMBEDDING={"onnx_batch_size": 7}`` would otherwise enter the
      inventory as a flat switch and turn the disjointness pin below red
      against a section that had not moved.

    It is still textual, so it sees only names spelled out in full: a switch
    assembled at runtime is invisible here.
    """
    names: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _MEMTOMEM_NAME_LITERAL.findall(text):
            if MEMTOMEM_ENV_NESTED_DELIMITER not in match[len(MEMTOMEM_ENV_PREFIX) :]:
                names.add(match)
    return frozenset(names)


_SRC_FLAT_SWITCHES = _flat_names_in_src()


class TestInventory:
    def test_the_repo_root_is_the_checkout_root(self):
        """``_REPO_ROOT`` anchors both the source scan and the child's cwd.

        It was ``parents[4]`` — one level too high — and every test still
        passed, because the child's node path was built relative to the same
        wrong root and pytest re-derived its rootdir from the argument. Nothing
        was asserting what the constant actually pointed at.
        """
        assert (_REPO_ROOT / "pyproject.toml").is_file()
        assert (_SRC / "memtomem" / "config.py").is_file()

    def test_the_source_scan_finds_the_known_switches(self):
        """A scan that silently matched nothing would make its pins vacuous."""
        assert {"MEMTOMEM_TOOL_MODE", "MEMTOMEM_LOG_LEVEL"} <= _SRC_FLAT_SWITCHES

    def test_the_source_scan_skips_identifiers_that_only_end_in_a_name(self):
        """Precision, not just coverage — the other half of the scan's claim.

        ``_MEMTOMEM_MARKER`` and friends are module constants, and the bare-word
        scan this replaced collected them as switches. An inventory that
        over-collects turns the disjointness pin into a false alarm the day
        somebody names a constant after a config section.
        """
        assert "MEMTOMEM_MARKER" not in _SRC_FLAT_SWITCHES
        assert "MEMTOMEM_TEST_RUNTIME_DIR" not in _SRC_FLAT_SWITCHES

    def test_the_model_is_shaped_the_way_the_selector_assumes(self):
        """Schema drift: the selector reads `model_fields`, and that is enough
        only while sections stay plain models with no aliases.

        A top-level validation alias binds a name the prefix rule never sees; a
        section that is itself a ``BaseSettings`` reads its own environment
        rather than the parent's. Neither exists today. This is where that
        stops being true loudly instead of the scrub quietly missing a binding.
        """
        from pydantic_settings import BaseSettings

        from memtomem.config import Mem2MemConfig

        # Non-vacuity: an empty reading would pass this loop, and would also
        # make the disjointness pin above pass against nothing.
        assert len(memtomem_config_section_names()) >= 20

        for name, field in Mem2MemConfig.model_fields.items():
            assert field.validation_alias is None, f"{name} binds an alias"
            annotation = field.annotation
            assert not (isinstance(annotation, type) and issubclass(annotation, BaseSettings)), (
                f"{name} is its own settings source"
            )

    def test_the_env_prefix_and_delimiter_are_what_the_scrub_assumes(self):
        """Both are read off ``Mem2MemConfig.model_config`` rather than typed.

        That makes them follow the model, which is the point — and makes a
        change to either silently redefine what the scrub considers config.
        This is the loud half: the values are stated once, here.
        """
        assert MEMTOMEM_ENV_PREFIX == "MEMTOMEM_"
        assert MEMTOMEM_ENV_NESTED_DELIMITER == "__"


class TestAmbientSelection:
    """What ``ambient_memtomem_config_env`` picks out of an environment."""

    def test_selects_a_config_field_binding(self):
        assert ambient_memtomem_config_env({"MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE": "4"}) == [
            "MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE"
        ]

    def test_selects_a_lowercase_binding(self):
        """pydantic-settings binds env names case-insensitively.

        A scrub that only matched the documented uppercase spelling would leave
        the lowercase variant owning the field, which reads as a clean run.
        """
        assert ambient_memtomem_config_env({"memtomem_search__default_top_k": "20"}) == [
            "memtomem_search__default_top_k"
        ]

    def test_selects_a_whole_section_json_binding(self):
        """The flat shape pydantic also accepts for a nested model.

        ``MEMTOMEM_EMBEDDING={"onnx_batch_size": 7}`` carries no delimiter, so a
        rule that read only the delimiter would leave the whole section owned by
        the developer's shell. What it does to an isolated ``config.json`` is
        pinned in :class:`TestWhyTheScrubExists` — it is not simply "the same as
        the delimiter spelling".
        """
        assert ambient_memtomem_config_env({"MEMTOMEM_EMBEDDING": "{}"}) == ["MEMTOMEM_EMBEDDING"]

    @pytest.mark.parametrize("name", sorted(_SRC_FLAT_SWITCHES | _HARNESS_FLAT_SWITCHES))
    def test_spares_a_flat_switch(self, name: str):
        """Every flat ``MEMTOMEM_*`` name this repository spells out.

        Not a sample and not a hand-list: the production half is scanned out of
        ``src/`` at collection time. A prefix-shaped rule swallows all of them —
        ``MEMTOMEM_UPDATE_WIRE_GOLDENS`` is the one with teeth, since
        ``test_web_wire_fixtures`` documents exporting it to regenerate the
        goldens and a scrubbed run can only ever compare them.
        """
        assert ambient_memtomem_config_env({name: "1"}) == []

    def test_no_production_flat_switch_is_a_section_name(self):
        """The two populations have to stay disjoint for the rule to be safe.

        Sparing flat names while scrubbing section names is coherent only while
        no flat switch *is* a section name. Nothing in the product enforces
        that, so it is asserted against the scanned inventory: add a
        ``MEMTOMEM_POLICY`` switch to ``src/`` and this goes red, rather than
        the switch being scrubbed as config and the feature going quiet.

        Scanned, because the guarantee is only worth as much as the inventory
        behind it — a hardcoded set would stay green for exactly the new switch
        that needed catching.
        """
        sections = memtomem_config_section_names()
        flat = {name[len(MEMTOMEM_ENV_PREFIX) :] for name in _SRC_FLAT_SWITCHES}
        assert not (flat & sections)

    def test_ignores_names_outside_the_prefix(self):
        """The prefix match is anchored at the start, not a substring search."""
        environ = {"PATH": "/usr/bin", "NOT_MEMTOMEM_EMBEDDING__DIMENSION": "1"}
        assert ambient_memtomem_config_env(environ) == []


class TestWhyTheScrubExists:
    def test_ambient_env_outranks_an_isolated_config_file(self, monkeypatch, tmp_path):
        """Env beats the redirected ``config.json`` — the hazard, stated live.

        This asserts *production* precedence, not the fixture: it is the reason
        the session scrub has to exist. If this ever goes red, the environment
        stopped owning config fields and the scrub's rationale needs rewriting
        rather than the test.
        """
        root = isolate_config_paths(monkeypatch, tmp_path / "memtomem-home")
        (root / "config.json").write_text(
            json.dumps({"embedding": {"onnx_batch_size": 6}}), encoding="utf-8"
        )
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE", "4")

        config = build_fresh_config(migrate=False)

        assert config.embedding.onnx_batch_size == 4

    def test_a_whole_section_binding_fills_a_field_the_file_is_silent_on(
        self, monkeypatch, tmp_path
    ):
        """The flat JSON shape reaches exactly the state our fixtures create.

        A redirected config layer is empty or nearly so, which is precisely
        where this shape lands: nothing in ``config.json`` claims the field, so
        the value pydantic built from the environment survives into the app.
        """
        root = isolate_config_paths(monkeypatch, tmp_path / "memtomem-home")
        (root / "config.json").write_text(
            json.dumps({"search": {"default_top_k": 11}}), encoding="utf-8"
        )
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps({"onnx_batch_size": 7}))

        config = build_fresh_config(migrate=False)

        assert config.embedding.onnx_batch_size == 7

    def test_a_whole_section_binding_beats_a_pinned_file_field(self, monkeypatch, tmp_path):
        """And it outranks the file too, so the scrub cannot skip this shape.

        This row used to assert the file won. ``load_config_overrides`` yields
        to the environment through :func:`memtomem.config.env_var_owning`,
        which recognised only ``memtomem_<section>__<field>`` — so a field the
        file pinned was written back over the environment's value, the
        opposite of what the delimiter spelling did (issue #2390). Both
        spellings now rank alike, which is why the redirected ``config.json``
        an isolated test writes is no defence on its own: whichever shape a
        developer exported reaches the run unless the scrub removes it.
        """
        root = isolate_config_paths(monkeypatch, tmp_path / "memtomem-home")
        (root / "config.json").write_text(
            json.dumps({"embedding": {"onnx_batch_size": 6}}), encoding="utf-8"
        )
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps({"onnx_batch_size": 7}))

        config = build_fresh_config(migrate=False)

        assert config.embedding.onnx_batch_size == 7


class TestWiring:
    def test_the_session_scrub_is_active(self, request: pytest.FixtureRequest):
        """Tripwire for the autouse session fixture being removed or renamed."""
        assert "_scrub_ambient_memtomem_env" in request.fixturenames


@pytest.mark.skipif(
    os.environ.get(CANARY_ENV) != "1",
    reason="canary for the child-process pin below; not part of an ordinary run",
)
def test_canary_reads_a_scrubbed_environment():
    """Runs in the child process, which was started with the env seeded.

    Four claims in one place because they are one behaviour: the config
    bindings are gone in all three spellings, the flat switch survived, and a
    value a function-scoped fixture sets after the session scrub still stands.
    """
    assert "MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE" not in os.environ
    assert not [k for k in os.environ if k.lower() == "memtomem_search__default_top_k"]
    assert "MEMTOMEM_STORAGE" not in os.environ
    assert os.environ.get("MEMTOMEM_UPDATE_WIRE_GOLDENS") == "1"
    assert os.environ.get("MEMTOMEM_WEB__CSRF_ENFORCE") == "0"


#: Inherited pytest options must not reach the child. ``PYTEST_ADDOPTS`` is the
#: dangerous one: a ``-k`` in it silently deselects the canary, and an absolute
#: ``--basetemp`` makes the child wipe and recreate that directory on its way in
#: — which, inherited from a parent run, is the parent's own temp tree.
_INHERITED_PYTEST_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST")


def _child_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_PYTEST_ENV}
    env.update(overrides)
    return env


class TestChildProcess:
    def test_a_seeded_environment_is_scrubbed_for_a_real_run(self, monkeypatch, tmp_path):
        """The only pin here that cannot pass vacuously.

        Every other test in this file runs inside a session whose environment
        was already clean on CI, so an empty scrub body — or one calling the
        wrong selector — leaves them all green. Seeding the environment
        requires a fresh process, because the fixture under test runs once, at
        session setup, before any test could set a variable for it to find.

        The hostile ``PYTEST_ADDOPTS`` is not decoration: it makes
        :func:`_child_env`'s stripping load-bearing here. Leave the variable in
        and the child deselects every test, exits non-zero, and this fails —
        rather than an inherited option quietly reshaping a run nobody watches.
        """
        monkeypatch.setenv("PYTEST_ADDOPTS", "-k __no_test_matches_this__")
        env = _child_env(
            **{
                CANARY_ENV: "1",
                "MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE": "4",
                "memtomem_search__default_top_k": "20",
                "MEMTOMEM_STORAGE": json.dumps({"backend": "sqlite"}),
                "MEMTOMEM_UPDATE_WIRE_GOLDENS": "1",
            }
        )

        node = (
            Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
            + "::test_canary_reads_a_scrubbed_environment"
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(tmp_path / "child-basetemp"),
            ],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert " passed" in result.stdout
