"""Source-scan guards for public-doc cross-file invariants.

These guards protect invariants that code cannot enforce directly:

- Every editor integration's Verify Connection section must surface the
  `mm status` CLI — it's the terminal mirror of `mem_status` for users
  whose editor has not reconnected yet.
- Every editor integration's First Indexing example must use the same
  multiline `Indexing complete:` block, so users comparing editors see
  the same expected output shape.
- `mem_config` / `mem_embedding_reset` / `mem_reset` live in the Config
  tool group in both ``reference.md`` and ``mcp-clients.md``; both files
  must mark them with the ``\\*`` + ``MEMTOMEM_TOOL_MODE=full`` footnote,
  or users reading one file won't know they are gated.
- The ``hooks.json`` snippet rendered in ``claude-code.md`` Hooks
  Automation Setup must declare byte-identical ``command`` strings to
  the plugin's shipped ``hooks.json`` for every event the snippet
  covers. Drift between the two sites silently ships an outdated
  user-facing recipe.
- Public guides must not use `````jsonc`` fences for
  ``config.d`` examples — the fragment loader at
  ``packages/memtomem/src/memtomem/config.py:1157`` calls strict
  ``json.loads`` and a ``//`` comment drops the fragment with only a
  startup-log WARNING (see #854).
"""

from __future__ import annotations

import json
import re
import types
import typing
from pathlib import Path

import click
import pydantic
import pytest

from memtomem.cli import cli as _CLI
from memtomem.config import Mem2MemConfig

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUIDES = _REPO_ROOT / "docs" / "guides"
_INTEGRATIONS = _GUIDES / "integrations"
_README = _REPO_ROOT / "README.md"
_SRC = _REPO_ROOT / "packages" / "memtomem" / "src" / "memtomem"
_PLUGIN_HOOKS_JSON = _REPO_ROOT / "packages" / "memtomem-claude-plugin" / "hooks" / "hooks.json"
_HOOKS_SNIPPET_ANCHOR = "Add the following to `~/.claude/settings.json`:"

_ASTERISK_TOOLS = ("mem_config", "mem_embedding_reset", "mem_reset")
_FOOTNOTE_PREFIX = r"\* Requires `MEMTOMEM_TOOL_MODE=full`"


def _read(path: Path) -> str:
    assert path.exists(), f"Doc file missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claude_code() -> str:
    return _read(_INTEGRATIONS / "claude-code.md")


@pytest.fixture(scope="module")
def claude_desktop() -> str:
    return _read(_INTEGRATIONS / "claude-desktop.md")


@pytest.fixture(scope="module")
def cursor() -> str:
    return _read(_INTEGRATIONS / "cursor.md")


@pytest.fixture(scope="module")
def mcp_clients() -> str:
    return _read(_GUIDES / "mcp-clients.md")


@pytest.fixture(scope="module")
def reference() -> str:
    return _read(_GUIDES / "reference.md")


@pytest.fixture(scope="module")
def canonical_footnote(reference: str) -> str:
    """The tool-mode footnote line, extracted from reference.md.

    reference.md is the canonical source; other docs (mcp-clients.md)
    must carry this line verbatim. Extracting it here keeps parity
    failures scoped to "target file drifted" — if reference.md itself
    loses the footnote, this fixture fails and parity tests never run,
    so a reference-side regression can't be mistaken for a target-side one.
    """
    for line in reference.splitlines():
        if line.startswith(_FOOTNOTE_PREFIX):
            return line
    pytest.fail(
        f"reference.md lost its tool-mode footnote line (no line starts with {_FOOTNOTE_PREFIX!r})"
    )


class TestIntegrationsMmStatus:
    def test_claude_code_surfaces_mm_status(self, claude_code: str) -> None:
        assert "mm status" in claude_code

    def test_claude_desktop_surfaces_mm_status(self, claude_desktop: str) -> None:
        assert "mm status" in claude_desktop

    def test_cursor_surfaces_mm_status(self, cursor: str) -> None:
        assert "mm status" in cursor


class TestIntegrationsIndexingBlock:
    def test_claude_code_indexing_block(self, claude_code: str) -> None:
        assert "Indexing complete:" in claude_code

    def test_claude_desktop_indexing_block(self, claude_desktop: str) -> None:
        assert "Indexing complete:" in claude_desktop

    def test_cursor_indexing_block(self, cursor: str) -> None:
        assert "Indexing complete:" in cursor, (
            "cursor.md First Indexing example must use the multiline "
            "'Indexing complete:' block (Files scanned / Total chunks / "
            "Indexed / Skipped / Deleted) — parity with claude-code.md "
            "and claude-desktop.md."
        )


class TestToolModeFootnoteParity:
    def test_reference_marks_tools(self, reference: str) -> None:
        for name in _ASTERISK_TOOLS:
            assert f"`{name}`\\*" in reference, (
                f"reference.md Config table must tag `{name}` with `\\*`."
            )

    def test_mcp_clients_marks_tools(self, mcp_clients: str) -> None:
        for name in _ASTERISK_TOOLS:
            assert f"`{name}`\\*" in mcp_clients, (
                f"mcp-clients.md Config table must tag `{name}` with `\\*` "
                f"(parity with reference.md so users see the tool-mode gate)."
            )

    def test_mcp_clients_matches_reference_footnote(
        self, canonical_footnote: str, mcp_clients: str
    ) -> None:
        assert canonical_footnote in mcp_clients, (
            "mcp-clients.md must carry reference.md's tool-mode footnote "
            "line verbatim so the CLI / Web UI alternate-access hint stays "
            "in sync across the two Config-table entry points."
        )


def _extract_hooks_snippet(claude_code_md: str) -> dict:
    """Extract the ``Add the following to ~/.claude/settings.json`` JSON
    block from claude-code.md. Returns the parsed dict.

    The Hooks Automation Setup section embeds a fenced ``json`` block that
    users copy-paste into their Claude Code settings; this helper returns
    that block as the parsed dict so parity tests can compare commands
    against the plugin's shipped hooks.json.
    """
    anchor_idx = claude_code_md.find(_HOOKS_SNIPPET_ANCHOR)
    if anchor_idx == -1:
        pytest.fail(f"claude-code.md lost its hooks-snippet anchor: {_HOOKS_SNIPPET_ANCHOR!r}")
    fence_re = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
    match = fence_re.search(claude_code_md, anchor_idx)
    if match is None:
        pytest.fail("claude-code.md has the hooks-snippet anchor but no ```json fence after it")
    return json.loads(match.group(1))


def _commands_by_event_matcher(hooks_doc: dict) -> dict[tuple[str, str], str]:
    """Flatten a hooks.json shape into ``{(event, matcher): command}``.

    Only entries with a single command are included; multi-command entries
    fail loudly because the parity test isn't designed for them yet.
    """
    out: dict[tuple[str, str], str] = {}
    for event, entries in hooks_doc.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            commands = entry.get("hooks", [])
            assert len(commands) == 1, (
                f"hooks parity helper expected exactly one command per entry, "
                f"got {len(commands)} at {event}/{matcher!r}"
            )
            out[(event, matcher)] = commands[0]["command"]
    return out


class TestPluginHooksDocsParity:
    """The hooks.json snippet in claude-code.md must declare byte-identical
    ``command`` strings to the plugin's shipped hooks.json for every
    (event, matcher) pair the docs cover. The docs intentionally show a
    subset (the ``activity log`` PostToolUse entry is omitted to keep the
    copy-paste recipe tight), so we iterate over the docs entries and
    require each to match the plugin file — not the other way around.
    """

    @pytest.fixture(scope="class")
    def plugin_commands(self) -> dict[tuple[str, str], str]:
        plugin_hooks = json.loads(_PLUGIN_HOOKS_JSON.read_text(encoding="utf-8"))
        return _commands_by_event_matcher(plugin_hooks)

    @pytest.fixture(scope="class")
    def docs_commands(self, claude_code: str) -> dict[tuple[str, str], str]:
        snippet = _extract_hooks_snippet(claude_code)
        return _commands_by_event_matcher(snippet)

    def test_docs_snippet_is_subset_of_plugin(
        self,
        plugin_commands: dict[tuple[str, str], str],
        docs_commands: dict[tuple[str, str], str],
    ) -> None:
        missing = [k for k in docs_commands if k not in plugin_commands]
        assert not missing, (
            f"claude-code.md hooks snippet declares (event, matcher) entries "
            f"that the plugin hooks.json does not ship: {missing}. Either add "
            f"them to packages/memtomem-claude-plugin/hooks/hooks.json or "
            f"remove them from the docs."
        )

    def test_docs_snippet_commands_match_plugin(
        self,
        plugin_commands: dict[tuple[str, str], str],
        docs_commands: dict[tuple[str, str], str],
    ) -> None:
        diffs = [
            (event_matcher, plugin_commands[event_matcher], docs_cmd)
            for event_matcher, docs_cmd in docs_commands.items()
            if plugin_commands.get(event_matcher) != docs_cmd
        ]
        assert not diffs, (
            "claude-code.md hooks snippet drifted from the plugin's "
            "hooks.json. The two sites must declare byte-identical commands "
            "for every (event, matcher) the docs render. Diffs:\n"
            + "\n".join(f"  {em}:\n    plugin: {p}\n    docs:   {d}" for em, p, d in diffs)
        )


class TestNoJsoncFenceInPublicGuides:
    """Public guides must not use ```` ```jsonc ```` fences.

    The fragment loader at
    ``packages/memtomem/src/memtomem/config.py:1157`` calls
    ``json.loads`` strictly; ``//`` comments and trailing commas raise
    ``JSONDecodeError`` which the surrounding ``except`` swallows with
    only a startup-log WARNING (lines 1158-1160). A user who copy-pastes
    a ``jsonc`` block from a guide ends up with a fragment that never
    loads and an "exclude_patterns aren't applied" symptom that's hard
    to trace back to that warn line. The canonical post-fix shape is
    the pure-JSON fence + prose lead-in + per-row table established by
    PR #853 in ``multi-device-sync.md`` and applied to
    ``configuration.md`` in #854.
    """

    def test_no_jsonc_fence_in_any_public_guide(self) -> None:
        offenders = sorted(
            str(md.relative_to(_REPO_ROOT))
            for md in _GUIDES.rglob("*.md")
            if "```jsonc" in md.read_text(encoding="utf-8")
        )
        assert not offenders, (
            "Public guides use ```jsonc fences which the strict "
            "json.loads fragment loader cannot parse "
            "(packages/memtomem/src/memtomem/config.py:1157). Use "
            "```json + pure JSON inside the fence and move any //-style "
            "annotations to surrounding prose or a per-row table — see "
            "PR #853 / multi-device-sync.md:262-268 for the canonical "
            f"shape, and #854 for the trap. Offenders: {offenders}"
        )


# ===========================================================================
# Doc <-> source drift guards.
#
# These three guards catch the class of documentation bug fixed in
# #1453-#1459 the moment it is reintroduced: a CLI command/flag that no
# longer exists (e.g. the nonexistent ``mm server``), a ``MEMTOMEM_*`` env
# var that is a typo or was removed, and an internal link/anchor that no
# longer resolves. The source of truth is introspected live (the Click tree,
# the pydantic settings model, the on-disk headings), so the guards update
# themselves -- there is no hand-maintained list to drift.
#
# Direction is doc -> source (every *documented* item must exist). The
# reverse direction (every command/var must be documented) is a separate,
# noisier completeness concern and is intentionally not enforced here.
# ===========================================================================


def _iter_code_context(text: str):
    """Yield ``(line, in_fence)`` so callers can scope to code, not prose.

    A prose mention such as an inline-code ``mm`` followed by ordinary words
    must not be validated as an invocation; only fenced blocks and inline-code
    spans that actually contain ``mm <word>`` are.
    """
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        yield line, in_fence


def _doc_mm_paths(text: str) -> set[tuple[str, ...]]:
    """Leading bare-word token sequence of every ``mm ...`` call in code.

    Collects every leading token that looks like a command/subcommand name
    (``[a-z][a-z0-9-]*``) and stops at the first argument, flag, or
    placeholder (``<name>`` / ``--apply`` / ``~/notes`` / ``key.path``), so
    what remains is the candidate command path to walk against the Click
    tree -- at full depth, not just two levels.
    """
    paths: set[tuple[str, ...]] = set()
    for line, in_fence in _iter_code_context(text):
        segments: list[str] = []
        if in_fence:
            segments = re.findall(r"(?<![\w-])(?:uv run )?mm ([a-z][^\n`#]*)", line)
        else:
            for span in re.findall(r"`([^`]+)`", line):
                if span.startswith(("mm ", "uv run mm ")):
                    segments.append(re.sub(r"^(?:uv run )?mm ", "", span))
        for seg in segments:
            toks: list[str] = []
            for word in seg.split():
                if re.fullmatch(r"[a-z][a-z0-9-]*", word):
                    toks.append(word)
                else:
                    break
            if toks:
                paths.add(tuple(toks))
    return paths


_CLI_DOCS = (
    _README,
    _GUIDES / "getting-started.md",
    _GUIDES / "reference.md",
    _GUIDES / "reference" / "core-memory-tools.md",
    _GUIDES / "reference" / "organization-maintenance.md",
    _GUIDES / "reference" / "automation.md",
    _GUIDES / "reference" / "data-config-cli.md",
    _GUIDES / "reference" / "operations.md",
    _GUIDES / "configuration.md",
)


class TestDocumentedCliExists:
    """Every ``mm <cmd ...>`` shown in the docs must resolve in ``memtomem.cli``."""

    def test_documented_mm_commands_resolve(self) -> None:
        offenders: list[str] = []
        for doc in _CLI_DOCS:
            for path in _doc_mm_paths(_read(doc)):
                node: click.Command = _CLI
                walked: list[str] = []
                for tok in path:
                    if not isinstance(node, click.Group):
                        break  # reached a leaf command; remaining tokens are args
                    if tok not in node.commands:
                        where = (
                            "a command" if not walked else f"a subcommand of `{' '.join(walked)}`"
                        )
                        offenders.append(
                            f"{doc.name}: `mm {' '.join([*walked, tok])}` -- `{tok}` is not {where}"
                        )
                        break
                    node = node.commands[tok]
                    walked.append(tok)
        assert not offenders, (
            "Docs reference CLI commands/subcommands that no longer exist in "
            "memtomem.cli (fix the doc or the command):\n  " + "\n  ".join(sorted(set(offenders)))
        )


def _settings_class(annotation: object) -> type[pydantic.BaseModel] | None:
    """The nested settings model an annotation points at, unwrapping
    ``Annotated[...]`` and ``Optional`` / ``X | None`` wrappers; else ``None``.
    """
    ann = annotation
    if hasattr(ann, "__metadata__"):  # Annotated[T, ...]
        ann = typing.get_args(ann)[0]
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        ann = non_none[0] if len(non_none) == 1 else None
    if isinstance(ann, type) and issubclass(ann, pydantic.BaseModel):
        return ann
    return None


def _pydantic_env_vars(model: type[pydantic.BaseModel], prefix: str = "MEMTOMEM_") -> set[str]:
    """All ``MEMTOMEM_*`` names derivable from a pydantic settings model."""
    out: set[str] = set()
    for name, field in model.model_fields.items():
        sub = _settings_class(field.annotation)
        if sub is not None:
            out |= _pydantic_env_vars(sub, f"{prefix}{name.upper()}__")
        else:
            out.add(f"{prefix}{name.upper()}")
    return out


# ``MEMTOMEM_*`` vars read straight from ``os.environ`` rather than declared as
# pydantic settings fields. A new env-only knob that gets documented must be
# added here; ``test_env_only_allowlist_is_real`` asserts every entry is an
# actual literal in the source so this list cannot itself drift into fiction.
_ENV_ONLY_VARS = frozenset(
    {
        "MEMTOMEM_TOOL_MODE",  # server/__init__.py
        "MEMTOMEM_WEB__MODE",  # web/app.py (_WEB_MODE_ENV)
        "MEMTOMEM_WEB__HOST",  # web/app.py
        "MEMTOMEM_WEB__PORT",  # web/app.py
        "MEMTOMEM_WEB__CSRF_ENFORCE",  # web/app.py (_CSRF_ENFORCE_ENV) + middleware/csrf.py
        "MEMTOMEM_LOG_LEVEL",  # server/lifespan.py
        "MEMTOMEM_LOG_FORMAT",  # server/lifespan.py
        "MEMTOMEM_WIKI_PATH",  # wiki/store.py
        "MEMTOMEM_FASTEMBED_CACHE",  # embedding/fastembed_cache.py
        "MEMTOMEM_INDEX_DEBOUNCE_QUEUE",  # indexing/debounce.py
    }
)


def _source_env_literals() -> set[str]:
    """Every ``MEMTOMEM_*`` literal present anywhere in src (used only to
    sanity-check that the env-only allowlist names are real)."""
    blob = "\n".join(p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py"))
    return set(re.findall(r"MEMTOMEM_[A-Z0-9_]+", blob))


class TestDocumentedEnvVarsExist:
    """Every ``MEMTOMEM_*`` in configuration.md must exist in source."""

    def test_env_only_allowlist_is_real(self) -> None:
        bogus = _ENV_ONLY_VARS - _source_env_literals()
        assert not bogus, f"_ENV_ONLY_VARS names not found as literals in src: {sorted(bogus)}"

    def test_configuration_env_vars_resolve(self) -> None:
        valid = _pydantic_env_vars(Mem2MemConfig) | _ENV_ONLY_VARS
        used = set(re.findall(r"MEMTOMEM_[A-Z0-9_]+", _read(_GUIDES / "configuration.md")))
        unknown = used - valid
        assert not unknown, (
            "configuration.md documents MEMTOMEM_* variables that are neither a "
            "pydantic settings field nor a known os.environ read "
            f"(typo, removed, or missing from _ENV_ONLY_VARS?): {sorted(unknown)}"
        )


def _slug(text: str) -> str:
    """GitHub-style heading anchor slug (no collapse of repeated separators)."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return s.replace(" ", "-")


def _anchors(md_text: str) -> set[str]:
    """Heading slugs (with -1/-2 dedup) plus explicit HTML ``<a id|name>``."""
    out: set[str] = set()
    seen: dict[str, int] = {}
    for line in md_text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        base = _slug(m.group(1))
        n = seen.get(base, 0)
        out.add(base if n == 0 else f"{base}-{n}")
        seen[base] = n + 1
    for aid in re.findall(r"<a[^>]+(?:id|name)=\"([^\"]+)\"", md_text):
        out.add(aid.lower())
    return out


_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class TestInternalDocLinksResolve:
    """Internal markdown links and #anchors across the guides must resolve."""

    def test_links_and_anchors_resolve(self) -> None:
        docs = sorted(_GUIDES.rglob("*.md")) + [_README]
        anchor_cache: dict[Path, set[str]] = {}
        offenders: list[str] = []
        for doc in docs:
            text = _read(doc)
            for line, in_fence in _iter_code_context(text):
                if in_fence:
                    continue
                # Drop inline-code spans so a `[title](target)` shown as a
                # literal example (reference.md:692) is not read as a link.
                line = re.sub(r"`[^`]*`", "", line)
                for raw in _LINK.findall(line):
                    target = raw.strip().strip("<>")
                    if target.startswith(("http://", "https://", "mailto:", "tel:")):
                        continue
                    file_part, _, anchor = target.partition("#")
                    if file_part:
                        tgt = (doc.parent / file_part).resolve()
                        if not tgt.exists():
                            offenders.append(f"{doc.name}: missing target -> {target}")
                            continue
                        anchor_src = tgt
                    else:
                        anchor_src = doc
                    if anchor and anchor_src.suffix == ".md":
                        if anchor_src not in anchor_cache:
                            anchor_cache[anchor_src] = _anchors(_read(anchor_src))
                        if anchor.lower() not in anchor_cache[anchor_src]:
                            offenders.append(f"{doc.name}: broken anchor -> {target}")
        assert not offenders, "Broken internal doc links/anchors:\n  " + "\n  ".join(offenders)
