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
- The Claude guide must describe automation as a separate opt-in plugin and
  must not reintroduce shell interpolation of hook event fields.
- Public guides must not use `````jsonc`` fences for
  ``config.d`` examples — the fragment loader at
  ``packages/memtomem/src/memtomem/config.py:1157`` calls strict
  ``json.loads`` and a ``//`` comment drops the fragment with only a
  startup-log WARNING (see #854).
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
import types
import typing
from pathlib import Path
from urllib.parse import unquote

import click
import pydantic
import pytest

from memtomem.cli import cli as _CLI
from memtomem.config import Mem2MemConfig
from memtomem.server import _ALL_REGISTERED_TOOLS, _CORE_TOOLS, _STANDARD_PACKS
from memtomem.server.tool_registry import ACTIONS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUIDES = _REPO_ROOT / "docs" / "guides"
_INTEGRATIONS = _GUIDES / "integrations"
_README = _REPO_ROOT / "README.md"
_PYPI_README = _REPO_ROOT / "packages" / "memtomem" / "README.md"
_PLUGIN_README = _REPO_ROOT / "packages" / "memtomem-claude-plugin" / "README.md"
_NOTEBOOKS_README = _REPO_ROOT / "examples" / "notebooks" / "README.md"
_PLUGIN_CONTRACT = _REPO_ROOT / "packages" / "memtomem-plugin-assets" / "contract.toml"
_VIBE_GUIDE = _GUIDES / "vibe-coding-getting-started-ko.md"
_SRC = _REPO_ROOT / "packages" / "memtomem" / "src" / "memtomem"
_AUTOMATION_HOOKS_JSON = (
    _REPO_ROOT / "packages" / "memtomem-claude-automation-plugin" / "hooks" / "hooks.json"
)

_ASTERISK_TOOLS = ("mem_config", "mem_embedding_reset", "mem_reset")
_FOOTNOTE_PREFIX = r"\* Exposed as an individual tool only under `MEMTOMEM_TOOL_MODE=full`"


def _public_markdown() -> list[Path]:
    """Tracked public docs whose links and command examples are contractual."""
    roots = [
        _README,
        _PYPI_README,
        _PLUGIN_README,
        _NOTEBOOKS_README,
        _REPO_ROOT / "SECURITY.md",
        _REPO_ROOT / "CONTRIBUTING.md",
        _REPO_ROOT / "CLA.md",
    ]
    return sorted([*roots, *_GUIDES.rglob("*.md")])


def _read(path: Path) -> str:
    assert path.exists(), f"Doc file missing: {path}"
    return path.read_text(encoding="utf-8")


def _plugin_contract() -> dict:
    with _PLUGIN_CONTRACT.open("rb") as handle:
        return tomllib.load(handle)


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
def operations() -> str:
    return _read(_GUIDES / "reference" / "operations.md")


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

    def test_no_public_doc_gates_the_capability_on_full_mode(self) -> None:
        """No public doc may pair an asterisk tool with ``full`` mode without
        saying the action still runs through ``mem_do``.

        The two Config tables were not the only place claiming these features
        need ``full`` — ``reference/data-config-cli.md`` and
        ``configuration.md`` said it too. Scoping the guard to the tables would
        have left the misconception in print, so scan every public markdown
        file instead of enumerating the known ones.

        Matching is per **paragraph**, not per line. A line-scoped version of
        this check inspected zero lines in this repo: the claim wraps across
        lines in both prose passages, and the two Config-table footnotes carry
        no tool name at all (the asterisk does that job). It would only have
        caught the exact single-line phrasing it replaced — the sweep scoped by
        one enumeration and then guarded by the same enumeration.

        Note for whoever edits the footnotes: ``canonical_footnote`` extracts a
        single *line*, so the reference.md / mcp-clients.md footnotes must stay
        on one line for the parity test above.
        """
        offenders: list[str] = []
        for path in _public_markdown():
            text = path.read_text(encoding="utf-8")
            for block in re.split(r"\n\s*\n", text):
                flat = " ".join(block.split())
                if "MEMTOMEM_TOOL_MODE=full" not in flat:
                    continue
                if not any(name in flat for name in _ASTERISK_TOOLS):
                    continue
                if "mem_do(" in flat:
                    continue
                lineno = text[: text.index(block)].count("\n") + 1
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
        assert not offenders, (
            "these lines gate mem_config / mem_embedding_reset / mem_reset on "
            f"MEMTOMEM_TOOL_MODE=full without noting the mem_do route: {offenders}. "
            "Only the individual tool name requires full mode; the actions are "
            "@register-ed and reachable in core/standard via mem_do."
        )

    def test_footnote_states_mem_do_remains_available(self, canonical_footnote: str) -> None:
        """The asterisk gates the individual *tool name*, not the capability.

        All three tools are ``@register``-ed, so ``mem_do(action="config")``
        works in core and standard mode — an earlier footnote read as if the
        feature itself required ``full`` and pointed users at the CLI as the
        only alternative.
        """
        assert "mem_do(" in canonical_footnote, (
            "the tool-mode footnote must say the actions stay reachable through "
            "mem_do in core/standard mode, not just that full mode is required"
        )
        for name in _ASTERISK_TOOLS:
            action = name.removeprefix("mem_")
            assert f'"{action}"' in canonical_footnote, (
                f"footnote must name the mem_do action '{action}' for {name}"
            )


class TestWebRemoteAccessDocs:
    """#1618: the ``mm web`` remote-access flags are security-critical
    (they gate off-loopback exposure and startup refuses without them),
    so the operations guide must document them — and the doc must track
    the live CLI, not a remembered spelling. ``TestDocumentedCliExists``
    strips flags when validating ``mm ...`` snippets, so this guard
    checks the flag surface explicitly."""

    _REMOTE_FLAGS = ("--allow-remote-ui", "--trusted-origin", "--trusted-host")

    def test_flags_exist_on_live_cli(self) -> None:
        web = _CLI.commands["web"]
        live = {p for param in web.params for p in param.opts}
        for flag in self._REMOTE_FLAGS:
            assert flag in live, (
                f"{flag} disappeared from `mm web` — update the Remote access "
                "section in docs/guides/reference/operations.md in the same PR."
            )

    def test_operations_documents_every_remote_flag(self, operations: str) -> None:
        assert "### Remote access" in operations
        for flag in self._REMOTE_FLAGS:
            assert flag in operations, (
                f"operations.md Remote access section lost {flag} — it must "
                "name every off-loopback opt-in flag (#1618)."
            )

    def test_operations_names_the_refusal_and_proxy_guidance(self, operations: str) -> None:
        # The two security-load-bearing statements: startup refuses
        # off-loopback binds, and public exposure needs an authenticating
        # reverse proxy (no first-party auth, ADR-0029).
        assert "refuses to start" in operations
        assert "reverse proxy" in operations
        assert "0029-mcp-network-transport-auth-stance.md" in operations


class TestOptionalClaudeAutomationDocs:
    def test_guide_names_the_separate_automation_plugin(self, claude_code: str) -> None:
        assert "/plugin install memtomem-automation@memtomem" in claude_code
        assert "separate opt-in" in claude_code or "second plugin" in claude_code

    def test_guide_does_not_publish_broken_shell_interpolation(self, claude_code: str) -> None:
        assert "${prompt}" not in claude_code
        assert "${tool_input" not in claude_code
        assert "mm session end --auto" not in claude_code

    def test_automation_hooks_use_dispatcher_and_second_timeouts(self) -> None:
        hooks = json.loads(_AUTOMATION_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
        assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
        for event, rules in hooks.items():
            for rule in rules:
                for handler in rule["hooks"]:
                    assert handler["command"] == "uv"
                    assert handler["args"][:3] == ["run", "--no-project", "python"]
                    assert handler["args"][-1] == event
                    assert 0 < handler["timeout"] <= 120


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
        if not in_fence and "no top-level" in line.lower():
            continue
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


_CLI_DOCS = tuple(_public_markdown())


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

    def test_documented_mm_flags_resolve(self) -> None:
        """Flags shown on one-line or backslash-continued calls exist live."""
        offenders: list[str] = []
        for doc in _CLI_DOCS:
            text = _read(doc).replace("\\\n", " ")
            for line, in_fence in _iter_code_context(text):
                segments: list[str] = []
                if in_fence:
                    segments = re.findall(r"(?<![\w-])(?:uv run )?mm ([^\n`#;|&]+)", line)
                else:
                    if "future" in line.lower():
                        continue
                    for span in re.findall(r"`([^`]+)`", line):
                        segments.extend(re.findall(r"(?<![\w-])(?:uv run )?mm ([^;|&]+)", span))
                for segment in segments:
                    try:
                        tokens = shlex.split(segment)
                    except ValueError:
                        continue
                    node: click.Command = _CLI
                    walked: list[str] = []
                    for token in tokens:
                        if token.startswith("-"):
                            opt = token.split("=", 1)[0]
                            if opt in {"--", "--help", "-h"}:
                                continue
                            live = {o for param in node.params for o in param.opts}
                            if opt not in live:
                                command = " ".join(["mm", *walked])
                                offenders.append(f"{doc.name}: `{command} {opt}`")
                            continue
                        if isinstance(node, click.Group) and token in node.commands:
                            node = node.commands[token]
                            walked.append(token)
        assert not offenders, (
            "Docs reference flags that do not exist on the resolved live CLI command:\n  "
            + "\n  ".join(sorted(set(offenders)))
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

    def test_every_settings_leaf_is_documented(self) -> None:
        expected = _pydantic_env_vars(Mem2MemConfig)
        used = set(re.findall(r"MEMTOMEM_[A-Z0-9_]+", _read(_GUIDES / "configuration.md")))
        missing = expected - used
        assert not missing, (
            "configuration.md must name every pydantic settings leaf, including "
            "deprecated compatibility fields (mark them deprecated rather than "
            f"silently omitting them): {sorted(missing)}"
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
_GITHUB_BLOB_PREFIX = "https://github.com/memtomem/memtomem/blob/main/"
_GITHUB_TREE_PREFIX = "https://github.com/memtomem/memtomem/tree/main/"


def _same_repo_absolute_target(raw: str) -> str | None:
    for prefix in (_GITHUB_BLOB_PREFIX, _GITHUB_TREE_PREFIX):
        if raw.startswith(prefix):
            return raw.removeprefix(prefix)
    return None


class TestInternalDocLinksResolve:
    """Internal markdown links and #anchors across the guides must resolve."""

    def test_links_and_anchors_resolve(self) -> None:
        docs = _public_markdown()
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
                    same_repo = _same_repo_absolute_target(target)
                    if same_repo is not None:
                        target = same_repo
                    elif target.startswith(("http://", "https://", "mailto:", "tel:")):
                        continue
                    file_part, _, anchor = target.partition("#")
                    if file_part:
                        tgt = (
                            (_REPO_ROOT / unquote(file_part)).resolve()
                            if same_repo is not None
                            else (doc.parent / unquote(file_part)).resolve()
                        )
                        try:
                            tgt.relative_to(_REPO_ROOT)
                        except ValueError:
                            offenders.append(f"{doc.name}: target escapes repository -> {target}")
                            continue
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

    def test_no_duplicate_generated_heading_slugs(self) -> None:
        offenders: list[str] = []
        for doc in _public_markdown():
            text = _read(doc)
            seen: set[str] = set()
            for line, in_fence in _iter_code_context(text):
                if in_fence:
                    continue
                match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
                if not match:
                    continue
                slug = _slug(match.group(1))
                if slug in seen:
                    offenders.append(f"{doc.relative_to(_REPO_ROOT)}: {slug}")
                seen.add(slug)
        assert not offenders, (
            "Duplicate public heading slugs make generated anchors ambiguous:\n  "
            + "\n  ".join(offenders)
        )

    def test_restructured_entrypoints_keep_compatibility_anchors(self) -> None:
        required = {
            _README: {"3-use", "4-web-ui-optional"},
            _GUIDES / "README.md": {
                "set-up",
                "tune",
                "power-features",
                "reference--lifecycle",
            },
            _GUIDES / "getting-started.md": {
                "pick-an-embedding-path-optional",
                "choose-your-setup",
                "claude-code",
                "cursor-windsurf-claude-desktop-antigravity-cli-gemini-cli",
                "verify-connection",
                "1-index-your-notes",
                "2-search",
                "3-add-a-memory",
                "4-recall-recent-memories",
            },
            _GUIDES / "mcp-clients.md": {
                "available-mcp-tools-96",
                "verify-connection",
                "verify-connection-1",
                "verify-connection-2",
            },
        }
        for doc, expected in required.items():
            missing = expected - _anchors(_read(doc))
            assert not missing, f"{doc.name} lost compatibility anchors: {sorted(missing)}"


def _quick_start(text: str) -> str:
    match = re.search(r"^## Quick Start\s*$\n(.*?)(?=^##\s)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "README lost its `## Quick Start` section"
    return match.group(1)


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)\n```", text, re.DOTALL)


class TestPublicReadmeAndExamples:
    _QUICK_START_COMMANDS = (
        "mm init",
        "mm status",
        'mm add "Deployment checklist uses blue-green rollout" --tags ops',
        'mm search "blue-green"',
    )

    @pytest.mark.parametrize("readme", [_README, _PYPI_README])
    def test_readmes_share_quick_start_contract(self, readme: Path) -> None:
        section = _quick_start(_read(readme))
        positions = [section.find(command) for command in self._QUICK_START_COMMANDS]
        assert all(position >= 0 for position in positions), (
            f"{readme.name} must contain the shared deterministic Quick Start: "
            f"{self._QUICK_START_COMMANDS}"
        )
        assert positions == sorted(positions), f"{readme.name} Quick Start command order drifted"

    @pytest.mark.parametrize("readme", [_README, _PYPI_README])
    def test_readmes_state_hook_and_stm_boundaries(self, readme: Path) -> None:
        text = _read(readme).lower()
        assert "hook-free by default" in text
        assert "memtomem-stm" in text
        assert "optional" in text

    def test_pypi_readme_uses_absolute_markdown_links(self) -> None:
        offenders = []
        for raw in _LINK.findall(_read(_PYPI_README)):
            target = raw.strip().strip("<>")
            file_part = target.partition("#")[0]
            if file_part.endswith(".md") and not target.startswith("https://"):
                offenders.append(target)
        assert not offenders, f"PyPI README has relative Markdown links: {offenders}"

    def test_normal_mcp_examples_do_not_override_saved_memory_dirs(self) -> None:
        mcp_clients = _read(_GUIDES / "mcp-clients.md")
        ordinary, marker, overrides = mcp_clients.partition("## 11. Environment Variable Overrides")
        assert marker
        assert "MEMTOMEM_INDEXING__MEMORY_DIRS" not in ordinary
        assert "MEMTOMEM_INDEXING__MEMORY_DIRS" in overrides
        for name in ("claude-code.md", "cursor.md", "claude-desktop.md"):
            assert "MEMTOMEM_INDEXING__MEMORY_DIRS" not in _read(_INTEGRATIONS / name)

    def test_mcp_json_and_toml_examples_parse(self) -> None:
        docs = [_GUIDES / "mcp-clients.md", *_INTEGRATIONS.glob("*.md")]
        for doc in docs:
            for index, block in enumerate(_fenced_blocks(_read(doc), "json"), start=1):
                try:
                    json.loads(block)
                except json.JSONDecodeError as exc:
                    pytest.fail(f"{doc.name} JSON block {index} is not copy-paste valid: {exc}")
            for index, block in enumerate(_fenced_blocks(_read(doc), "toml"), start=1):
                try:
                    tomllib.loads(block)
                except tomllib.TOMLDecodeError as exc:
                    pytest.fail(f"{doc.name} TOML block {index} is not copy-paste valid: {exc}")

    def test_hidden_qa_commands_stay_out_of_public_docs(self) -> None:
        blob = "\n".join(_read(doc) for doc in _public_markdown())
        assert "mm context seed-validation" not in blob
        assert "mm agent debug-resolve" not in blob

    def test_add_file_help_matches_scope_aware_path_resolution(self) -> None:
        add = _CLI.commands["add"]
        file_param = next(param for param in add.params if param.name == "file_name")
        assert "selected scope's memory directory" in (file_param.help or "")
        assert "~/.memtomem/memories" not in (file_param.help or "")


class TestVibeCodingQuickstart:
    def test_public_entrypoints_link_quickstart(self) -> None:
        relative = "vibe-coding-getting-started-ko.md"
        entrypoints = (
            _README,
            _PYPI_README,
            _GUIDES / "README.md",
            _GUIDES / "getting-started.md",
            _GUIDES / "reference.md",
            _INTEGRATIONS / "claude-code.md",
            _INTEGRATIONS / "codex.md",
        )
        for entrypoint in entrypoints:
            assert relative in _read(entrypoint), (
                f"{entrypoint.relative_to(_REPO_ROOT)} must link the Korean vibe-coding quickstart"
            )
        assert (f"https://github.com/memtomem/memtomem/blob/main/docs/guides/{relative}") in _read(
            _PYPI_README
        )

    def test_plugin_install_commands_match_supported_public_flow(self) -> None:
        guide = _read(_VIBE_GUIDE)
        commands = (
            "/plugin marketplace add memtomem/memtomem",
            "/plugin install memtomem@memtomem",
            "codex plugin marketplace add memtomem/memtomem",
            "codex plugin add memtomem@memtomem",
        )
        for command in commands:
            assert command in guide

    def test_first_success_workflows_match_plugin_contract(self) -> None:
        guide = _read(_VIBE_GUIDE)
        workflows = {row["id"]: row for row in _plugin_contract()["workflows"]}
        for workflow_id in ("status", "remember", "search"):
            row = workflows[workflow_id]
            assert f"/memtomem:{workflow_id}" in guide
            assert f"${row['codex_name']}" in guide
        assert workflows["remember"]["effect"] == "write"
        assert workflows["remember"]["implicit"] is False
        assert workflows["status"]["effect"] == "read"
        assert workflows["search"]["effect"] == "read"

    def test_public_quickstart_keeps_beginner_scope_and_safety_boundaries(self) -> None:
        guide = _read(_VIBE_GUIDE)
        for required in ("BM25-only", "클라우드 동기화", "API key", "명시적"):
            assert required in guide
        for advanced_or_private in (
            "docs/reports/",
            "project_shared",
            "mm ingest codex-memory",
            "memtomem-automation",
        ):
            assert advanced_or_private not in guide


class TestRegistryAndInstallDocs:
    def test_cli_reference_names_every_top_level_command(self) -> None:
        text = _read(_GUIDES / "reference" / "data-config-cli.md")
        section = text.split("## CLI Reference", 1)[1]
        missing = [name for name in sorted(_CLI.commands) if f"`{name}`" not in section]
        assert not missing, f"CLI reference lost top-level commands: {missing}"

    def test_full_mcp_table_matches_current_registry(self) -> None:
        text = _read(_GUIDES / "mcp-clients.md")
        section = text.split("### Available MCP Tools", 1)[1].split("### STM Proxy Tools", 1)[0]
        table = "\n".join(line for line in section.splitlines() if line.startswith("|"))
        documented = set(re.findall(r"\bmem_[a-z0-9_]+\b", table))
        current = set(_ALL_REGISTERED_TOOLS) - {"mem_context_migrate"}
        assert documented == current, (
            f"MCP table drifted; missing={sorted(current - documented)}, "
            f"extra={sorted(documented - current)}"
        )
        assert len(current) == 99
        assert len(_ALL_REGISTERED_TOOLS) == 100
        assert len(_CORE_TOOLS) == 9
        standard = set(_CORE_TOOLS) | {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _STANDARD_PACKS
        }
        assert len(standard & set(_ALL_REGISTERED_TOOLS)) == 38

    def test_optional_extras_table_matches_package_metadata(self) -> None:
        with (_REPO_ROOT / "packages" / "memtomem" / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        expected = set(project["optional-dependencies"])
        guide = _read(_GUIDES / "getting-started.md")
        section = guide.split("#### Optional extras", 1)[1].split("### Option B", 1)[0]
        documented = set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))
        assert documented == expected

    def test_public_docs_do_not_use_floating_bare_uvx_server(self) -> None:
        blob = "\n".join(_read(doc) for doc in _public_markdown())
        floating_from = re.compile(
            r"--from(?:\s+|[\"']?\s*,\s*[\"'])memtomem"
            r"(?:\s+|[\"']?\s*,\s*[\"'])memtomem-server"
        )
        assert floating_from.search(blob) is None

        with (_REPO_ROOT / "packages" / "memtomem" / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        pins = set(re.findall(r"memtomem\[all\]==([0-9]+\.[0-9]+\.[0-9]+)", blob))
        assert pins == {version}

    def test_runtime_setup_surfaces_use_exact_pinned_uvx(self) -> None:
        with (_REPO_ROOT / "packages" / "memtomem" / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]

        portal = _read(
            _REPO_ROOT
            / "packages"
            / "memtomem"
            / "src"
            / "memtomem"
            / "web"
            / "static"
            / "context-portal.js"
        )
        pin = f"memtomem[all]=={version}"
        assert f"--from '{pin}' memtomem-server" in portal
        assert portal.count(f'"{pin}"') == 2
        assert "<code>claude mcp add memtomem -- memtomem-server</code>" not in portal
        assert '"command": "memtomem-server"' not in portal
        assert 'command = "memtomem-server"' not in portal


_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_LINT_DOCS = (_REPO_ROOT / "CLAUDE.md", _REPO_ROOT / "CONTRIBUTING.md")

# Anchored at the bare ``ruff`` token, not ``uv run ruff``: a doc teaching
# ``uvx ruff check src`` (or ``uv  run`` with doubled spaces) narrows the lint
# just as effectively, so the launcher must not be part of the match. The
# optional ``@…`` absorbs uv's version-qualified requests (``uvx
# ruff@0.15.21 check``), which are the same call in a different coat.
# ``[^\S\n]`` (horizontal space only) keeps a call from swallowing the lines
# below it; continuations are folded to spaces before this runs.
_RUFF_CALL = re.compile(r"(?<![\w-])ruff(?:@[^\s`#;|&\\]+)?((?:[^\S\n]+[^\s`#;|&\\]+)+)")

# Flags known not to change which files ruff walks NOR mask violations, per
# verb. Everything else is reported rather than dropped: modelling ruff's
# option arity here would mean tracking its whole CLI, and the interesting
# flags are exactly the ones that re-scope a call that otherwise carries CI's
# paths (``--exclude``; ``--config``, which can carry an exclude of its own).
# An allowlist is fail-closed for the flags nobody has thought of yet, at the
# cost of a deliberate edit here when the docs legitimately grow a new one.
# ``--diff`` is format-only: for ``check`` it implies fix-only mode and exits
# 0 on violations that have no autofix, so it weakens the documented gate.
_INERT_FLAGS = {
    "check": frozenset({"--fix", "--no-fix"}),
    "format": frozenset({"--check", "--diff"}),
}


class _RuffCall(typing.NamedTuple):
    verb: str  # "check" | "format"
    paths: tuple[str, ...]
    other_flags: tuple[str, ...]


def _ruff_calls(text: str) -> list[_RuffCall]:
    """Every ``ruff check|format`` call, in order, whatever the launcher.

    Backslash continuations are folded first so a call split across lines
    reads as one invocation (same normalisation as
    ``test_documented_mm_flags_resolve``). The first non-flag token after
    ``ruff`` is the verb; matches whose verb is not ``check``/``format``
    (prose like "ruff and tests must pass", other subcommands) are skipped.
    Flags before the verb — ruff's global-option slot, ``ruff --config x
    check`` — are collected the same as flags after it.

    Only the verb's inert flags are dropped; any other flag is kept in
    ``other_flags`` for the caller to reject. A flag's value may therefore
    land in ``paths`` (``--select E`` reads "E" as a path) — harmless,
    because a call carrying an un-inert flag is already an offender.

    ``paths`` is never empty: ruff defaults ``[FILES]`` to ``.``, so a
    pathless call is recorded as ``(".",)`` rather than dropped, which
    would hide ``ruff check --exclude tests``.
    """
    calls: list[_RuffCall] = []
    for args in _RUFF_CALL.findall(text.replace("\\\n", " ")):
        try:
            tokens = shlex.split(args)
        except ValueError as exc:
            raise AssertionError(
                f"Unparseable ruff invocation in a guarded file — {exc}: `ruff{args}`"
            ) from exc
        verb: str | None = None
        paths: list[str] = []
        flags: list[str] = []
        prev_was_flag = False
        for tok in tokens:
            if tok.startswith("-"):
                flags.append(tok.split("=", 1)[0])
                prev_was_flag = True
                continue
            if verb is None:
                if tok in _INERT_FLAGS:
                    verb = tok
                elif not prev_was_flag:
                    # Bare word where the verb should be: prose ("ruff can
                    # check your code") or another subcommand. Not a lint call.
                    break
                # else: a pre-verb flag's space-separated value ("--config
                # custom.toml check"). Skip it — the flag itself is already
                # recorded, and no pre-verb flag is inert, so the call is an
                # offender regardless of what the value says.
            else:
                paths.append(tok)
            prev_was_flag = False
        if verb is None:
            continue
        kept = tuple(f for f in flags if f not in _INERT_FLAGS[verb])
        calls.append(_RuffCall(verb, tuple(paths) or (".",), kept))
    return calls


class TestLintPathsMatchCI:
    """Documented ruff paths must be the paths CI actually lints.

    The `lint` check is required to merge, so a doc that under-scopes it
    hands contributors a command that passes locally and still fails CI,
    with nothing local to reproduce against. That is not hypothetical: an
    external contributor's PR (#1678) went red on a UTF-8 BOM in a file
    under ``packages/memtomem/tests/`` while the then-documented
    ``ruff ... packages/memtomem/src`` reported clean.

    ``ci.yml`` is the source of truth — comparing the two docs against
    each other would pass while both are wrong together.
    """

    def test_ci_still_lints_with_explicit_paths(self) -> None:
        """Anchor: if CI's ruff calls move, the parity test below is void.

        Asserting the shape (one ``check``, one ``format``, same paths, no
        exclusions) is what lets the parity test read CI's path list off a
        single call. Without it, dropping ``format`` from the workflow
        would leave parity passing against a contract CI no longer has.
        """
        calls = _ruff_calls(_read(_CI_WORKFLOW))
        verbs = sorted(call.verb for call in calls)
        assert verbs == ["check", "format"], (
            f"Expected exactly one `ruff check` and one `ruff format` in "
            f"{_CI_WORKFLOW.name}, found {verbs or 'none'}. Update this guard "
            "to match the workflow."
        )
        distinct = {call.paths for call in calls}
        assert len(distinct) == 1, (
            f"ruff check and ruff format --check lint different paths in "
            f"{_CI_WORKFLOW.name}: {sorted(distinct)}. The docs can't mirror both."
        )
        flagged = [call for call in calls if call.other_flags]
        assert not flagged, (
            f"{_CI_WORKFLOW.name} now passes ruff {list(flagged[0].other_flags)}, which may "
            "re-scope the lint. The parity test below compares paths only and would miss it — "
            f"teach {Path(__file__).name} what the flag does before adding it here."
        )

    def test_docs_lint_the_same_paths_as_ci(self) -> None:
        expected = _ruff_calls(_read(_CI_WORKFLOW))[0].paths
        offenders: list[str] = []
        for doc in _LINT_DOCS:
            calls = _ruff_calls(_read(doc))
            if not calls:
                offenders.append(f"{doc.name}: documents no `uv run ruff` command at all")
                continue
            for call in calls:
                shown = " ".join([*call.paths, *call.other_flags])
                if call.paths != expected:
                    offenders.append(f"{doc.name}: `uv run ruff {call.verb} ... {shown}`")
                elif call.other_flags:
                    offenders.append(
                        f"{doc.name}: `uv run ruff {call.verb} ... {shown}` carries CI's paths "
                        f"but adds {list(call.other_flags)}, which may re-scope them"
                    )
        assert not offenders, (
            "Documented ruff paths drifted from the required `lint` CI check.\n"
            f"{_CI_WORKFLOW.name} lints: {' '.join(expected)}\n"
            "Offending call(s):\n  " + "\n  ".join(offenders)
        )

    # The parser is the guard's entire attack surface: every bypass found in
    # review (five rounds' worth) was a parse gap, not a comparison bug. This
    # table pins each one so a parser refactor can't quietly reopen them.
    @pytest.mark.parametrize(
        ("snippet", "expected"),
        [
            # The two real commands the guarded docs actually teach.
            (
                "uv run ruff check packages/memtomem/src packages/memtomem/tests tools",
                [
                    _RuffCall(
                        "check", ("packages/memtomem/src", "packages/memtomem/tests", "tools"), ()
                    )
                ],
            ),
            (
                "uv run ruff format --check a b && echo ok",
                [_RuffCall("format", ("a", "b"), ())],
            ),
            # Inert flags per verb stay invisible; --diff is only inert for
            # format (for check it exits 0 on unfixable violations).
            ("uv run ruff check a --fix", [_RuffCall("check", ("a",), ())]),
            ("uv run ruff check a --no-fix", [_RuffCall("check", ("a",), ())]),
            ("uv run ruff format a --diff", [_RuffCall("format", ("a",), ())]),
            ("uv run ruff check a --diff", [_RuffCall("check", ("a",), ("--diff",))]),
            # Scope-changing flags surface in other_flags whatever the spelling.
            ("ruff check a --exclude=b", [_RuffCall("check", ("a",), ("--exclude",))]),
            ("ruff check a --config=x.toml", [_RuffCall("check", ("a",), ("--config",))]),
            ("ruff --config x.toml check a", [_RuffCall("check", ("a",), ("--config",))]),
            ("ruff check a --select E", [_RuffCall("check", ("a", "E"), ("--select",))]),
            # Pathless calls default to ".", never vanish.
            ("ruff check --exclude=tests", [_RuffCall("check", (".",), ("--exclude",))]),
            # Launcher and spacing variants are all the same call.
            ("uvx ruff check a", [_RuffCall("check", ("a",), ())]),
            ("uvx ruff@0.15.21 check a", [_RuffCall("check", ("a",), ())]),
            ("uv tool run ruff@0.15.21 check a", [_RuffCall("check", ("a",), ())]),
            ("uv  run ruff check a", [_RuffCall("check", ("a",), ())]),
            # Continuations fold into one call.
            ("uv run ruff check \\\n    a \\\n    b", [_RuffCall("check", ("a", "b"), ())]),
            # Prose and non-lint subcommands are not calls.
            ("ruff and tests must pass to merge", []),
            ("ruff rule F821", []),
            ("`ruff` alone in backticks", []),
            ("run ruff check locally", [_RuffCall("check", ("locally",), ())]),
        ],
    )
    def test_parser_pins_reviewed_spellings(self, snippet: str, expected: list[_RuffCall]) -> None:
        assert _ruff_calls(snippet) == expected

    def test_parser_reports_unparseable_call_with_fragment(self) -> None:
        with pytest.raises(AssertionError, match=r"No closing quotation.*unterminated"):
            _ruff_calls('uv run ruff check "unterminated')
