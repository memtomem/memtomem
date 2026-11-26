"""ADR-0030 PR-H2 — the Push/Pull copy sweep's boundary, encoded as tests.

PR-E (#1854) renamed the user-facing vocabulary for the UI, CLI help, and docs
and deliberately froze every machine-readable identifier; PR-H2 finished the job
for engine / route / MCP **output copy**. The risk a rename PR carries is not
that it renames too little — it is that a later sweep renames one string too
many. Three classes must never move together, so they are pinned here:

1. **Human-facing action copy** — moved to Push/Pull.
2. **Frozen wire identifiers** — reason codes, surface ids, route paths, CLI
   command strings. Their human text changed *around* them; they did not.
3. **Shared vocabulary** — project enrollment / pause / resume is one mechanism
   for BOTH gateway Push and Hooks Sync, so it stays "sync"; relational drift
   state ("in sync" / "out of sync") is status, not the action, so it stays too.
   Renaming either leaks into a sibling feature — the concrete failure PR-E hit
   (``test_settings_hooks_sync_409``), which is why those two live-fire tests
   remain the primary guard and this file only pins the vocabulary itself.

**How the stale-copy guard reads the code.** :func:`_scan` walks every ``.py``
file under :data:`_SCAN_ROOTS` with ``ast`` — no module list, so a new route or
tool file is covered when it is added. Each string literal is read whole: the
parser has already joined implicit concatenation, so a phrase split across
literals or lines is one value. An f-string's interpolations read as ``{}``
(their own literals are read separately) and its format specs as text.
Text is classified by where it is published:

* **public** — every ``str`` / ``bytes`` literal and f-string that is not a
  docstring; the docstring of a FastAPI endpoint (decorated, or registered with
  ``add_api_route``), of an MCP tool (``@mcp.tool`` / ``@register``), and of any
  class in ``web/routes/`` (a pydantic model publishes its docstring);
* **developer** — module docstrings and every other function or class docstring;
* **internal** — ``#`` comments, which are not in the AST at all.

Exemptions are spans of a value, never a whole line or literal, because each is
a place a regression could hide: a QUOTED §-citation title (the words are the
identifier), the ``--force-unsafe-import`` flag, and the per-site spans in
:data:`_SITE_EXEMPTIONS`. A match that overlaps an exempt span is dropped; the
rest of the value is still matched. :data:`_OTHER_FEATURE_FILES` skips the
memory import/export modules whole — "Import" is their own feature's verb.

**Limits, stated honestly.** A green run means no pattern in
:data:`_STALE_ACTION_COPY` matches public text under the scan roots outside an
exempt span (the real trees carry one match per site exemption). It does not
prove the trees are free of Sync/Import copy: a spelling no pattern names still
passes (the hyphen patterns name ``re-``/``reverse-`` and ``<word>-import``, not
every compound), ``web/schemas/`` is not walked, a file in
:data:`_OTHER_FEATURE_FILES` is not read at all, and the timeout suffix
``another sync may be in progress`` is not policed — whether "sync" is wrong
there depends on which lock each site waits on.

Three resolution limits, none of whose shapes exists in the trees today: an
``add_api_route`` endpoint is resolved only when passed as a bare name (not
``handlers.listed``); imports are collected from every scope of a module
without lexical resolution, so two scopes importing different handlers under
one alias leave one of them unresolved; and a site exemption names its function
by bare name, so a same-named method in that file shares it.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import memtomem
from memtomem.context import (
    _atomic_reverse,
    commands,
    privacy_scan,
    transfer,
)
from memtomem.web.routes import (
    context_mcp_servers,
    context_skills,
    context_sync_all,
)


def _src(mod: object) -> str:
    return Path(inspect.getfile(mod)).read_text(encoding="utf-8")  # type: ignore[arg-type]


# ── 1. the action copy moved ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mod", "needle"),
    [
        (context_skills, '"Push skills"'),
        (context_mcp_servers, '"Push MCP servers"'),
        (context_sync_all, "Push All is a project-tier action"),
        (_atomic_reverse, "already pulled from"),
        (privacy_scan, "before re-running the push."),
        # Persisted into ``versions.json``: new records say "pull", older ones
        # keep "import". Nothing rewrites stored notes.
        (_atomic_reverse, "pre-overwrite snapshot (pull from {runtime})"),
        (commands, "pre-overwrite snapshot (pull from gemini)"),
        (_atomic_reverse, "pull updates dir layout"),
    ],
)
def test_action_copy_uses_push_pull(mod: object, needle: str) -> None:
    assert needle in _src(mod)


# ── the stale-copy scanner ────────────────────────────────────────────────────

_PKG_ROOT = Path(memtomem.__file__).parent

#: Package subtrees the scanner walks, every ``.py`` file in each.
_SCAN_ROOTS = ("context", "web/routes", "server/tools")

#: Action-copy shapes that must not appear in public text. Deliberately
#: PATTERNS, not the exact strings a sweep happened to replace: an exact-string
#: list is only ever as complete as the enumeration that produced it, and the
#: first enumeration missed ~20 sites. Each pattern is matched against a whole
#: literal value, so no pattern is anchored on a quote character.
_STALE_ACTION_COPY = (
    # A capitalised action label anywhere in a value: "Sync skills",
    # f"{kind}Import …", "Synced: 3", "Sync/Import". Case-sensitive, so
    # "in sync" is not hit.
    r"Sync(ed)?[:\s/]",
    r"Import(ed)?[:\s/]",
    # A bare action label, e.g. a validation-manifest ``"action": "Sync"``
    # value, and the same pair written out inside prose.
    r"^(Sync|Import)(ed)?$",
    r'"action": "(Sync|Import)"',
    r"\bto import\b",
    r"already imported from",
    r"re-run (the import|sync) to retry",
    r"re-run sync\.",
    r"before re-running sync\.",
    r"blocked this (sync|import):",
    r"tier to import into",
    # The timeout messages interpolate the kind
    # (``f"{spec.kind.capitalize()} import timed out …"``), so the literal is
    # lowercase.
    r"(?i)\b(sync|import) timed out",
    # OpenAPI descriptions — published as the endpoint's docs.
    r"(?i)preview the import\b",
    r"would-import\b",
    r"(?i)per-type sync phase|five-phase sync\b",
    # UI breadcrumbs naming a button whose label is now Push/Pull.
    r"→ Sync\)",
    r"they sync from the working file",
    r"snapshot \(import from",
    r"reverse-sync updates",
    r"section-level import\b",
    r"import routes' dry_run",
    r"\bweb import is rejected",
    r"-tier import\b",
    # Hyphenated and run-together forms of the pull action. Kept narrow on
    # purpose: a general ``<word>-sync`` would hit ``batch-sync`` and
    # ``sync-enrollment``, the enrollment vocabulary shared with Hooks Sync.
    r"(?i)\b(re|reverse)[- ]?(sync|import)(ed|s|ing)?\b",
    r"(?i)\b\w+-import(ed|s|ing)?\b",
)

#: Spans exempt wherever they appear.
_SPAN_EXEMPTIONS = (
    # A QUOTED §-citation title is a proper noun: ``ADR-0021 §"Sync
    # orchestration"`` names a section that exists under that title. Only the
    # quoted form counts — an unquoted ``(ADR-0011 §3)`` beside stale copy
    # exempts nothing.
    re.compile(r'§\s*"[^"]*"'),
    # The CLI flag of the memory-upload / context-import privacy valve. Its
    # spelling is wire (``force_unsafe_import`` is the parameter name).
    re.compile(r"(?<![\w-])--force-unsafe-import(?![\w-])"),
)


@dataclass(frozen=True)
class _SiteExemption:
    """One exact substring, exempt only inside one function of one file."""

    relpath: str
    func: str
    text: str
    reason: str


_SITE_EXEMPTIONS = (
    _SiteExemption(
        "web/routes/settings_sync.py",
        "apply_settings_sync",
        "Settings sync timed out",
        "the standalone Hooks Sync route's own timeout; Hooks Sync keeps 'sync'",
    ),
    _SiteExemption(
        "context/bundle.py",
        "load_bundle",
        "Import it with the mem_import tool",
        "points a memory-export bundle at the memory-import tool",
    ),
    _SiteExemption(
        "context/bundle.py",
        "receive_artifact_bundle",
        "Import does not overwrite",
        "names the bundle-landing command, which is `mm context import`",
    ),
)

#: Files skipped whole: memory import/export, a separate feature whose verb is
#: "Import". None of them carries Context Gateway copy.
_OTHER_FEATURE_FILES = {
    "web/routes/export.py": "memory export / import routes",
    "server/tools/export_import.py": "mem_export / mem_import tools",
    "server/tools/importers.py": "Notion / Obsidian importers",
}

_ROUTE_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}
)

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Offence:
    relpath: str
    line: int
    col: int
    pattern: str

    def __str__(self) -> str:
        return f"{self.relpath}:{self.line}: [{self.pattern}]"


@dataclass
class ScanReport:
    offences: list[Offence] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # parsed, i.e. not excluded
    excluded: list[str] = field(default_factory=list)
    public_literals: Counter[str] = field(default_factory=Counter)
    site_hits: Counter[_SiteExemption] = field(default_factory=Counter)


def _dotted(relpath: str) -> str:
    """``web/routes/x.py`` → ``memtomem.web.routes.x``; a package ``__init__`` is the package."""
    return "memtomem." + relpath.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _resolve_import(node: ast.ImportFrom, relpath: str) -> str | None:
    """The absolute dotted module an ``ImportFrom`` names, if it can be read."""
    if node.level == 0:
        return node.module
    # A module's relative imports start from its package; a package
    # ``__init__``'s start from the package itself.
    parts = _dotted(relpath).split(".")
    if not relpath.endswith("/__init__.py"):
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[: -(node.level - 1)]
    if not parts:
        return None
    return ".".join(parts + ([node.module] if node.module else []))


def _route_endpoint_names(tree: ast.Module) -> Iterator[str]:
    """Names passed as the endpoint of ``<router>.add_api_route(path, endpoint)``."""
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_api_route"
        ):
            continue
        endpoint = node.args[1] if len(node.args) >= 2 else None
        for kw in node.keywords:
            if kw.arg == "endpoint":
                endpoint = kw.value
        if isinstance(endpoint, ast.Name):
            yield endpoint.id


def _registered_endpoints(trees: dict[str, ast.Module]) -> set[tuple[str, str]]:
    """``(dotted module, def name)`` of every def registered via ``add_api_route``."""
    out: set[tuple[str, str]] = set()
    for relpath, tree in trees.items():
        module = _dotted(relpath)
        imported: dict[str, tuple[str, str]] = {}
        # Imports in any scope (a ``mount()`` function may import its
        # handlers). Not lexically scoped, so a same-named def elsewhere in the
        # module is policed too — the error is toward checking more.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = _resolve_import(node, relpath)
                if source is None:
                    continue
                for alias in node.names:
                    imported[alias.asname or alias.name] = (source, alias.name)
        for name in _route_endpoint_names(tree):
            out.add(imported.get(name, (module, name)))
    return out


def _is_published_def(node: _FuncDef, module: str, endpoints: set[tuple[str, str]]) -> bool:
    if (module, node.name) in endpoints:
        return True
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_METHODS | {"tool"}:
            return True
        if isinstance(target, ast.Name) and target.id == "register":
            return True
    return False


def _docstring_node(node: ast.AST) -> ast.Constant | None:
    body = getattr(node, "body", None)
    if not body or not isinstance(body[0], ast.Expr):
        return None
    value = body[0].value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value
    return None


def _walk_with_func(node: ast.AST, func: str | None = None) -> Iterator[tuple[ast.AST, str | None]]:
    """Every node under *node*, with the name of the def whose body holds it.

    A def's decorators, defaults and annotations are evaluated in the enclosing
    scope, so they belong to the enclosing def, not to the one being defined.
    """
    for child in ast.iter_child_nodes(node):
        yield child, func
        if isinstance(child, _FuncDef):
            for stmt in child.body:
                yield stmt, child.name
                yield from _walk_with_func(stmt, child.name)
            for header in ast.iter_child_nodes(child):
                if header not in child.body:
                    yield header, func
                    yield from _walk_with_func(header, func)
        else:
            yield from _walk_with_func(child, func)


def _render(node: ast.Constant | ast.JoinedStr) -> str:
    if isinstance(node, ast.Constant):
        value = node.value
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue):
            # The hole's expression is not text; its own literals are visited
            # on their own. A format spec is text the formatter may emit
            # (``f"{ts:Next chance: %Y}"``), so it is read as part of this one.
            spec = part.format_spec
            parts.append("{:" + _render(spec) + "}" if isinstance(spec, ast.JoinedStr) else "{}")
    return "".join(parts)


def _public_literals(
    relpath: str, tree: ast.Module, endpoints: set[tuple[str, str]]
) -> Iterator[tuple[ast.Constant | ast.JoinedStr, str | None]]:
    """Each public literal of a module once, with its enclosing def name."""
    module = _dotted(relpath)
    developer_docs: set[int] = set()
    doc = _docstring_node(tree)
    if doc is not None:
        developer_docs.add(id(doc))
    not_top_level: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _FuncDef | ast.ClassDef):
            doc = _docstring_node(node)
            if doc is None:
                continue
            if isinstance(node, ast.ClassDef):
                published = relpath.startswith("web/routes/")
            else:
                published = _is_published_def(node, module, endpoints)
            if not published:
                developer_docs.add(id(doc))
        elif isinstance(node, ast.JoinedStr):
            # Fragments and format specs are part of the f-string they sit in.
            for part in node.values:
                not_top_level.add(id(part))
                if isinstance(part, ast.FormattedValue) and part.format_spec is not None:
                    not_top_level.add(id(part.format_spec))
    for node, func in _walk_with_func(tree):
        if id(node) in not_top_level or id(node) in developer_docs:
            continue
        if isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)
        ):
            yield node, func


def _scan(
    pkg_root: Path = _PKG_ROOT,
    *,
    roots: tuple[str, ...] = _SCAN_ROOTS,
    patterns: tuple[str, ...] = _STALE_ACTION_COPY,
    span_exemptions: tuple[re.Pattern[str], ...] = _SPAN_EXEMPTIONS,
    site_exemptions: tuple[_SiteExemption, ...] = _SITE_EXEMPTIONS,
    other_feature_files: frozenset[str] | set[str] = frozenset(_OTHER_FEATURE_FILES),
) -> ScanReport:
    report = ScanReport()
    trees: dict[str, ast.Module] = {}
    for root in roots:
        for path in sorted((pkg_root / root).rglob("*.py")):
            relpath = path.relative_to(pkg_root).as_posix()
            if relpath in other_feature_files:
                report.excluded.append(relpath)
            else:
                report.files.append(relpath)
                trees[relpath] = ast.parse(path.read_text(encoding="utf-8"), filename=relpath)
    compiled = [(p, re.compile(p)) for p in patterns]
    endpoints = _registered_endpoints(trees)
    for relpath, tree in trees.items():
        root = next(r for r in roots if relpath.startswith(r + "/"))
        for node, func in _public_literals(relpath, tree, endpoints):
            report.public_literals[root] += 1
            value = _render(node)
            exempt: list[tuple[int, int]] = []
            for rx in span_exemptions:
                exempt.extend(m.span() for m in rx.finditer(value))
            for site in site_exemptions:
                if site.relpath == relpath and site.func == func:
                    start = value.find(site.text)
                    while start != -1:
                        report.site_hits[site] += 1
                        exempt.append((start, start + len(site.text)))
                        start = value.find(site.text, start + 1)
            for pattern, rx in compiled:
                for m in rx.finditer(value):
                    if not any(m.start() < end and start < m.end() for start, end in exempt):
                        report.offences.append(
                            Offence(relpath, node.lineno, node.col_offset, pattern)
                        )
                        break
    return report


# ── 1b. the stale-copy scan over the real trees ───────────────────────────────

#: The modules the line-based predecessor of :func:`_scan` read. Used only as a
#: floor: the walk must reach every one of them.
_FORMERLY_SWEPT = (
    "context/_atomic_reverse.py",
    "context/_gate_a.py",
    "context/commands.py",
    "context/privacy_scan.py",
    "context/skills.py",
    "context/mcp_servers_copy.py",
    "context/_validation_seed.py",
    "server/tools/context.py",
    "web/routes/context_skills.py",
    "web/routes/context_mcp_servers.py",
    "web/routes/context_sync_all.py",
    "web/routes/_atomic_kind.py",
    "web/routes/context_agents.py",
    "web/routes/context_commands.py",
    "web/routes/_errors.py",
    "web/routes/context_transfer.py",
)


@pytest.fixture(scope="module")
def real_scan() -> ScanReport:
    return _scan()


def test_no_stale_sync_import_action_copy_remains(real_scan: ScanReport) -> None:
    """No public text under the scan roots carries Sync/Import *action* copy."""
    assert not real_scan.offences, "stale action copy:\n" + "\n".join(
        str(o) for o in real_scan.offences
    )


def test_scan_reaches_every_root_and_former_module(real_scan: ScanReport) -> None:
    assert sorted(real_scan.excluded) == sorted(_OTHER_FEATURE_FILES)
    assert not set(real_scan.files) & set(real_scan.excluded)
    for root in _SCAN_ROOTS:
        assert any(f.startswith(root + "/") for f in real_scan.files), root
        assert real_scan.public_literals[root] > 0, root
    missing = set(_FORMERLY_SWEPT) - set(real_scan.files)
    assert not missing, f"scan no longer reaches {sorted(missing)}"


def test_each_site_exemption_fires_exactly_once(real_scan: ScanReport) -> None:
    """A site exemption that matches nothing is stale; one that matches twice
    — two literals, or twice in one — is wider than the text it was written for."""
    for site in _SITE_EXEMPTIONS:
        assert real_scan.site_hits[site] == 1, (site, real_scan.site_hits[site])


def test_other_feature_files_exist() -> None:
    for relpath in _OTHER_FEATURE_FILES:
        assert (_PKG_ROOT / relpath).is_file(), f"stale exclusion: {relpath}"


# ── 1c. the scanner itself, on planted trees ──────────────────────────────────


def _plant(tmp_path: Path, files: dict[str, str]) -> Path:
    for root in _SCAN_ROOTS:
        (tmp_path / root).mkdir(parents=True, exist_ok=True)
    for relpath, source in files.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


def _found(tmp_path: Path, files: dict[str, str], **kwargs: object) -> set[tuple[str, int, str]]:
    report = _scan(_plant(tmp_path, files), **kwargs)  # type: ignore[arg-type]
    # Every planted fixture has at most one literal per line per pattern, so
    # a repeated row is a literal (or its fragment) reported twice.
    rows = [(o.relpath, o.line, o.pattern) for o in report.offences]
    assert len(rows) == len(set(rows)), f"reported twice: {rows}"
    return set(rows)


_ROUTE = "web/routes/planted.py"
_ENGINE = "context/planted.py"
_TOOL = "server/tools/planted.py"


@pytest.mark.parametrize(
    ("relpath", "source", "expected"),
    [
        pytest.param(
            _ROUTE,
            'X = ("re-run the "\n     "import to retry")\n',
            {(1, r"re-run (the import|sync) to retry")},
            id="phrase-split-across-lines",
        ),
        pytest.param(
            "web/routes/brand_new.py",
            'X = "nothing to import"\n',
            {(1, r"\bto import\b")},
            id="file-no-list-names",
        ),
        pytest.param(
            _ENGINE,
            'X = "delete it first to re-import"\n',
            {
                (1, r"(?i)\b(re|reverse)[- ]?(sync|import)(ed|s|ing)?\b"),
                (1, r"(?i)\b\w+-import(ed|s|ing)?\b"),
            },
            id="re-import",
        ),
        pytest.param(
            _ENGINE,
            'X = "the reverse-sync step"\n',
            {(1, r"(?i)\b(re|reverse)[- ]?(sync|import)(ed|s|ing)?\b")},
            id="reverse-sync",
        ),
        pytest.param(
            _ENGINE,
            'X = "an artifact-import refresh"\n',
            {(1, r"(?i)\b\w+-import(ed|s|ing)?\b")},
            id="artifact-import",
        ),
        pytest.param(
            _ENGINE,
            'X = "pass --would-import"\n',
            {(1, r"would-import\b"), (1, r"(?i)\b\w+-import(ed|s|ing)?\b")},
            id="flag-lookalike",
        ),
        pytest.param(
            _TOOL,
            '@register("context")\ndef mem_x():\n    """Import skills from a runtime."""\n',
            {(3, r"Import(ed)?[:\s/]")},
            id="mcp-register-docstring",
        ),
        pytest.param(
            _TOOL,
            '@mcp.tool()\ndef mem_x():\n    """Import skills from a runtime."""\n',
            {(3, r"Import(ed)?[:\s/]")},
            id="mcp-tool-docstring",
        ),
        pytest.param(
            _ROUTE,
            '@router.head("/x")\nasync def x():\n    """Sync the skills."""\n',
            {(3, r"Sync(ed)?[:\s/]")},
            id="head-endpoint-docstring",
        ),
        pytest.param(
            _ROUTE,
            'def x(t=Query("u", description="the tier to import into")):\n    pass\n',
            {(1, r"tier to import into"), (1, r"\bto import\b")},
            id="query-description",
        ),
        pytest.param(
            _ROUTE,
            '@router.get("/x", summary=("Run " "Sync skills"))\nasync def x():\n    pass\n',
            {(1, r"Sync(ed)?[:\s/]")},
            id="decorator-summary-concatenated",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{kind}" f"Import skills"\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="fstring-fstring-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{kind}" "Import skills"\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="fstring-plain-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = "Auto" "Sync skills"\n',
            {(1, r"Sync(ed)?[:\s/]")},
            id="word-glued-by-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = f"Auto" f"Import skills"\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="word-glued-by-fstring-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Import"}\n',
            {(1, r"^(Sync|Import)(ed)?$")},
            id="bare-action-label",
        ),
        pytest.param(
            _ENGINE,
            """X = 'Example: {"action": "Import"}'\n""",
            {(1, r'"action": "(Sync|Import)"')},
            id="embedded-action-label",
        ),
        pytest.param(
            _ENGINE,
            'X = b"Import skills"\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="bytes",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{x!r:>{w}} to import"\n',
            {(1, r"\bto import\b")},
            id="fstring-format-spec-once",
        ),
        pytest.param(
            _ENGINE,
            "X = f\"{f'nested to import'}\"\n",
            {(1, r"\bto import\b")},
            id="nested-fstring-once",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{ts:Next chance to import: %Y-%m-%d}"\n',
            {(1, r"\bto import\b")},
            id="format-spec-text",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Import" "\\nskills"}\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="label-then-newline",
        ),
        pytest.param(
            _ENGINE,
            'X = "Retry with --force-unsafe-importing"\n',
            {(1, r"(?i)\b\w+-import(ed|s|ing)?\b")},
            id="longer-token-than-the-flag",
        ),
        pytest.param(
            _ENGINE,
            'X = "Try --retry--force-unsafe-import"\n',
            {(1, r"(?i)\b\w+-import(ed|s|ing)?\b")},
            id="longer-token-before-the-flag",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Import" ": Skills"}\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="label-then-colon",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Sync" "/Import"}\n',
            {(1, r"Sync(ed)?[:\s/]")},
            id="label-then-slash",
        ),
        pytest.param(
            _ENGINE,
            'X = f"see #123 \\"Import skills\\""\n',
            {(1, r"Import(ed)?[:\s/]")},
            id="quoted-label-beside-issue-number",
        ),
    ],
)
def test_scan_flags_planted_shape(
    tmp_path: Path, relpath: str, source: str, expected: set[tuple[int, str]]
) -> None:
    assert _found(tmp_path, {relpath: source}) == {(relpath, ln, p) for ln, p in expected}


def test_add_api_route_endpoint_docstrings_are_public(tmp_path: Path) -> None:
    """``add_api_route`` publishes the endpoint's docstring, even when the
    endpoint is defined in another module — positional or keyword, absolute or
    relative import, aliased or not."""
    handlers = (
        'def listed():\n    """Sync the list."""\n\n'
        'def patched():\n    """Sync the patch."""\n\n'
        'def helper():\n    """Sync nothing: a helper no route registers."""\n'
    )
    registry = (
        "from memtomem.web.routes.handlers import listed\n"
        "from .handlers import patched as endpoint_fn\n"
        'router.add_api_route("", listed, methods=["GET"])\n'
        'router.add_api_route(path="/p", endpoint=endpoint_fn, methods=["PATCH"])\n'
    )
    found = _found(
        tmp_path, {"web/routes/handlers.py": handlers, "web/routes/registry.py": registry}
    )
    assert found == {
        ("web/routes/handlers.py", 2, r"Sync(ed)?[:\s/]"),
        ("web/routes/handlers.py", 5, r"Sync(ed)?[:\s/]"),
    }


@pytest.mark.parametrize(
    ("internal", "public"),
    [
        pytest.param(
            "X = 1  # nothing to import\n",
            'X = "nothing to import"\n',
            id="comment",
        ),
        pytest.param(
            'X = ("Continue "  # "Import skills"\n     "now")\n',
            'X = ("Continue "  "Import skills"\n     "now")\n',
            id="inline-comment-between-tokens",
        ),
        pytest.param(
            'def f():\n    """Import skills from a runtime."""\n',
            '@router.post("/x")\ndef f():\n    """Import skills from a runtime."""\n',
            id="engine-vs-endpoint-docstring",
        ),
        pytest.param(
            '"""Sync skills: the module docstring."""\n',
            'X = "Sync skills: a literal."\n',
            id="module-docstring",
        ),
    ],
)
def test_developer_text_is_not_policed_but_public_text_is(
    tmp_path: Path, internal: str, public: str
) -> None:
    assert _found(tmp_path / "a", {_ROUTE: internal}) == set()
    assert _found(tmp_path / "b", {_ROUTE: public}) != set()


@pytest.mark.parametrize(
    ("exempt", "control"),
    [
        pytest.param(
            'X = "Pass --force-unsafe-import to bypass."\n',
            'X = "Pass --force-unsafe-import to bypass, or pick a tier to import into."\n',
            id="flag-beside-stale-copy",
        ),
        pytest.param(
            "X = 'ADR-0021 §\"Sync orchestration\" deferred this.'\n",
            'X = "ADR-0021 Sync orchestration deferred this."\n',
            id="quoted-citation-vs-unquoted",
        ),
        pytest.param(
            'X = "ADR-0021 §\\"Sync orchestration\\" deferred this."\n',
            'X = "ADR-0021 §\\"Sync orchestration\\" deferred this; nothing to import."\n',
            id="escaped-citation-beside-stale-copy",
        ),
        pytest.param(
            'X = "Synced§\\"Design\\""\n',
            'X = "Synced: §\\"Design\\""\n',
            id="glued-citation-creates-no-match",
        ),
    ],
)
def test_span_exemptions_cover_only_their_span(tmp_path: Path, exempt: str, control: str) -> None:
    assert _found(tmp_path / "a", {_ENGINE: exempt}) == set()
    assert _found(tmp_path / "b", {_ENGINE: control}) != set()


def test_flag_exemption_is_what_clears_the_flag(tmp_path: Path) -> None:
    source = {_ENGINE: 'X = "Pass --force-unsafe-import to bypass."\n'}
    assert _found(tmp_path / "a", source) == set()
    assert _found(tmp_path / "b", source, span_exemptions=()) == {
        (_ENGINE, 1, r"(?i)\b\w+-import(ed|s|ing)?\b")
    }


def test_site_exemption_is_scoped_to_its_function_and_span(tmp_path: Path) -> None:
    site = _SITE_EXEMPTIONS[0]
    assert site.func == "apply_settings_sync"
    source = (
        "def apply_settings_sync():\n"
        '    raise E("Settings sync timed out — another sync may be in progress.")\n\n'
        "def apply_settings_sync_suffixed():\n"
        '    raise E("Settings sync timed out")\n\n'
        "def _sync_settings_core():\n"
        '    raise E("Settings sync timed out")\n'
    )
    appended = (
        "def apply_settings_sync():\n"
        '    raise E("Settings sync timed out. Pick a tier to import into.")\n'
    )
    pattern = r"(?i)\b(sync|import) timed out"
    assert _found(tmp_path / "a", {site.relpath: source}) == {
        (site.relpath, 5, pattern),
        (site.relpath, 8, pattern),
    }
    assert _found(tmp_path / "b", {site.relpath: appended}) == {
        (site.relpath, 2, r"tier to import into"),
        (site.relpath, 2, r"\bto import\b"),
    }


def test_other_feature_files_are_skipped_and_only_they(tmp_path: Path) -> None:
    source = 'X = "Import chunks from a bundle."\n'
    files = {relpath: source for relpath in _OTHER_FEATURE_FILES}
    files["web/routes/export_more.py"] = source
    assert _found(tmp_path, files) == {("web/routes/export_more.py", 1, r"Import(ed)?[:\s/]")}


def test_add_api_route_endpoint_in_a_package_or_a_function_scope(tmp_path: Path) -> None:
    """The endpoint lives in a package ``__init__`` and is imported inside a
    ``mount()`` function — both still resolve to the def that FastAPI publishes."""
    files = {
        "web/routes/handlers/__init__.py": 'def listed():\n    """Sync the list."""\n',
        "web/routes/handlers/mounting.py": (
            "def mount(router):\n"
            "    from . import listed as endpoint\n"
            '    router.add_api_route("/", endpoint=endpoint)\n'
        ),
    }
    assert _found(tmp_path, files) == {
        ("web/routes/handlers/__init__.py", 2, r"Sync(ed)?[:\s/]"),
    }


def test_site_exemption_does_not_reach_the_def_header(tmp_path: Path) -> None:
    """A decorator runs in the enclosing scope: text there is not inside
    ``apply_settings_sync`` and its site exemption does not apply."""
    site = _SITE_EXEMPTIONS[0]
    source = (
        '@router.post("/s", summary="Settings sync timed out")\n'
        "async def apply_settings_sync():\n"
        "    pass\n"
    )
    assert _found(tmp_path, {site.relpath: source}) == {
        (site.relpath, 1, r"(?i)\b(sync|import) timed out")
    }


def test_site_exemption_counts_each_occurrence(tmp_path: Path) -> None:
    site = _SITE_EXEMPTIONS[0]
    source = (
        "def apply_settings_sync():\n"
        '    raise E("Settings sync timed out. Settings sync timed out.")\n'
    )
    report = _scan(_plant(tmp_path, {site.relpath: source}))
    assert report.offences == []
    assert report.site_hits[site] == 2  # so the exactly-once check above fails


def test_two_literals_on_one_line_are_two_offences(tmp_path: Path) -> None:
    report = _scan(_plant(tmp_path, {_ENGINE: 'X = ["Sync skills", "Sync agents"]\n'}))
    assert [(o.line, o.col) for o in report.offences] == [(1, 5), (1, 20)]


#: The line-scanner predecessor's patterns, verbatim, each with a source line it
#: matched and the pattern of :data:`_STALE_ACTION_COPY` that now covers it.
_LEGACY = (
    (r'"Sync(ed)?[: ]', 'X = "Synced: 3 skills"', r"Sync(ed)?[:\s/]"),
    (r'f"Sync ', 'X = f"Sync {kind}"', r"Sync(ed)?[:\s/]"),
    (r'"Import(ed)? ', 'X = "Imported 3 skills"', r"Import(ed)?[:\s/]"),
    (r'f"Import ', 'X = f"Import {kind}"', r"Import(ed)?[:\s/]"),
    (r"\bto import\b", 'X = "nothing to import"', r"\bto import\b"),
    (r"already imported from", 'X = "already imported from claude"', r"already imported from"),
    (
        r"re-run (the import|sync) to retry",
        'X = "re-run the import to retry"',
        r"re-run (the import|sync) to retry",
    ),
    (r"re-run sync\.", 'X = "then re-run sync."', r"re-run sync\."),
    (r"before re-running sync\.", 'X = "fix before re-running sync."', r"before re-running sync\."),
    (
        r"blocked this (sync|import):",
        'X = "the scan blocked this sync: x"',
        r"blocked this (sync|import):",
    ),
    (r"tier to import into", 'X = "the tier to import into"', r"tier to import into"),
    (
        r"(?i)\b(sync|import) timed out",
        'X = f"{k} import timed out"',
        r"(?i)\b(sync|import) timed out",
    ),
    (r"(?i)preview the import\b", 'X = "Preview the import"', r"(?i)preview the import\b"),
    (r"would-import\b", 'X = "the would-import target"', r"would-import\b"),
    (
        r"(?i)per-type sync phase|five-phase sync\b",
        'X = "the five-phase sync"',
        r"(?i)per-type sync phase|five-phase sync\b",
    ),
    (r"→ Sync\)", 'X = "(Context → Sync)"', r"→ Sync\)"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Sync"}', r"^(Sync|Import)(ed)?$"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Import" "\\nskills"}', r"Import(ed)?[:\s/]"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Import" ": Skills"}', r"Import(ed)?[:\s/]"),
    (
        r"they sync from the working file",
        'X = "they sync from the working file"',
        r"they sync from the working file",
    ),
    (
        r"snapshot \(import from",
        'X = f"pre-overwrite snapshot (import from {r})"',
        r"snapshot \(import from",
    ),
    (r"reverse-sync updates", 'X = "reverse-sync updates dir layout"', r"reverse-sync updates"),
    (r"section-level import\b", 'X = "a section-level import"', r"section-level import\b"),
    (r"import routes' dry_run", """X = "the import routes' dry_run" """, r"import routes' dry_run"),
    (r"\bweb import is rejected", 'X = "web import is rejected"', r"\bweb import is rejected"),
    (r"-tier import\b", 'X = "a user-tier import"', r"-tier import\b"),
)


def test_legacy_matrix_covers_every_legacy_pattern() -> None:
    assert len({old for old, _, _ in _LEGACY}) == 24
    assert {new for _, _, new in _LEGACY} <= set(_STALE_ACTION_COPY)


@pytest.mark.parametrize(("old", "line", "new"), _LEGACY, ids=[old for old, _, _ in _LEGACY])
def test_scan_still_finds_what_the_line_scanner_found(
    tmp_path: Path, old: str, line: str, new: str
) -> None:
    assert re.search(old, line), "sample no longer exercises the legacy pattern"
    found = _found(tmp_path, {_ENGINE: line + "\n"})
    assert (_ENGINE, 1, new) in found


# ── 2. frozen wire identifiers did NOT move ───────────────────────────────────


@pytest.mark.parametrize(
    ("mod", "wire_id"),
    [
        # surface ids — Gate A audit attribution.
        (context_skills, '"web_context_skills_sync"'),
        # route paths.
        (context_sync_all, '"/context/sync-all"'),
        # the follow-up command the transfer result prints stays runnable.
        (transfer, 'f"mm context sync --scope {to_scope}"'),
    ],
)
def test_wire_identifiers_are_frozen(mod: object, wire_id: str) -> None:
    """Human text moved around these; the identifiers themselves must not."""
    assert wire_id in _src(mod)


@pytest.mark.parametrize(
    ("module_path", "code"),
    [
        # Skip codes: the human reason moved to "already pulled from …", the
        # code it travels with did not.
        ("memtomem.context._skip_reasons", '"already_imported"'),
        ("memtomem.context._skip_reasons", '"in_sync"'),
        # Enrollment codes live with the projects engine + its routes.
        ("memtomem.context.projects", '"sync_paused"'),
        ("memtomem.context.projects", '"sync_not_enrolled"'),
    ],
)
def test_reason_code_constants_keep_import_sync_spelling(module_path: str, code: str) -> None:
    """The skip-code vocabulary is wire, not copy — renaming it would break
    every consumer that branches on ``reason_code``."""
    import importlib

    src = _src(importlib.import_module(module_path))
    assert code in src, f"frozen reason_code {code} disappeared from {module_path}"


# ── 3. shared + relational vocabulary stays "sync" ────────────────────────────


def test_enrollment_vocabulary_stays_sync() -> None:
    """Enrollment / pause / resume is shared with Hooks Sync (PR-E boundary).

    ``settings-hooks-watchdog.js`` renders the same family, so renaming these to
    "push" leaks into a feature this campaign never touched.
    """
    from memtomem.web.routes import context_projects

    src = _src(context_projects)
    assert "not enrolled for sync" in src
    assert "enrollment paused" in src or "Resume sync" in src
    assert "not enrolled for push" not in src


def test_relational_drift_state_stays_sync() -> None:
    """ "in sync" / "out of sync" is status vocabulary (ADR-0030 §4), not the
    action verb — it describes a relation, so Push/Pull does not apply."""
    src = _src(_atomic_reverse)
    assert "in sync" in src
    assert "in push" not in src and "out of push" not in src
