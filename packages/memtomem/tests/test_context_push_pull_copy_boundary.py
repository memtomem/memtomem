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

Joining removes the token boundaries, so a literal of two or more source
tokens is also read where each token starts: :data:`_TOKEN_START_LABEL` matches
there a label glued to a following capitalised word (``"Sync" "All"`` reads as
``SyncAll``, ``f"{x}" "ImportSkills"`` as ``{}ImportSkills``). The tokens come
from one ``tokenize`` pass over the module, so a comment between them is its
own token and is not read. A one-token literal is never read this way, which
keeps an identifier such as ``"SyncPhaseError"`` unflagged.
Text is classified by where it is published:

* **public** — every ``str`` / ``bytes`` literal and f-string that is not a
  docstring; the docstring of every function the running app or MCP server
  publishes, read at test time from the mounted FastAPI routes (``prod`` and
  ``dev`` modes) and the MCP tool registries (:func:`_published_callables`);
  and the docstring of any class in ``web/routes/`` (a pydantic model
  publishes its docstring);
* **developer** — module docstrings and every other function or class docstring;
* **internal** — ``#`` comments, which are not in the AST at all.

Exemptions are spans of a value, never a whole line or literal, because each is
a place a regression could hide: a QUOTED §-citation title (the words are the
identifier), the ``--force-unsafe-import`` flag, the ``Hooks Sync`` feature
name, and the per-site spans in
:data:`_SITE_EXEMPTIONS`. A match lying wholly inside an exempt span is
dropped; one that reaches past it, and the rest of the value, are still matched.
:data:`_OTHER_FEATURE_FILES` skips the
memory import/export modules whole — "Import" is their own feature's verb.

**Limits, stated honestly.** A green run means no pattern in
:data:`_STALE_ACTION_COPY` matches public text under the scan roots, and
:data:`_TOKEN_START_LABEL` matches at no token start of such text, outside an
exempt span (the real trees carry one match per site exemption). It does not
prove the trees are free of Sync/Import copy: a spelling no pattern names still
passes (the hyphen patterns name ``re-``/``reverse-`` and ``<word>-import``, not
every compound), ``web/schemas/`` is not walked, a file in
:data:`_OTHER_FEATURE_FILES` is not read at all, and the timeout suffix
``another sync may be in progress`` is not policed — whether "sync" is wrong
there depends on which lock each site waits on.

A label glued to a following word is caught only at a token start of a
concatenated literal, and only before an ASCII capital ``A``–``Z``: written as
one token (``"SyncAll"``), or starting mid-token (``"Run Sync" "All"``), it
reads like an identifier and is not matched. Bytes literals are read whole only
(``b"Sync" b"All"`` is not caught): a UTF-8 sequence can straddle two bytes
tokens, so a token has no offset of its own in the decoded value. The trees
hold 5 public bytes literals today, none naming Sync or Import. That check
errs toward flagging a concatenation whose token starts with an identifier
(``"SyncPhaseError" " raised"``).

Two more limits, neither of whose shapes exists in the trees today. The checks
err toward flagging in two places: route path strings are matched as text, and a mounted route's
whole docstring and every class docstring under ``web/routes/`` count as
public, though FastAPI publishes less (``include_in_schema=False``, an explicit
``description=``, text after ``\\f``, classes that are not schema models).

A function's docstring counts as published only when that function is
mounted or registered: a route module the app never includes, or a tool the
server never registers, is read as developer text. A published callable whose
definition the walk cannot find fails
:func:`test_every_published_callable_is_found_by_the_walk`, and a tool
decorator that hides the function (no ``functools.wraps``) fails
:func:`test_published_callables_reads_the_live_registries`, instead of quietly
exempting its docstring.
"""

from __future__ import annotations

import ast
import bisect
import inspect
import io
import re
import sys
import textwrap
import tokenize
import types
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
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
    # A capitalised action label anywhere in a value, ended by anything but
    # an identifier character: "Sync skills", f"{kind}Import …", "Synced: 3",
    # "Sync/Import", "Sync-skills", "Sync!". "SyncPhaseError" and "Imports"
    # are not labels. Case-sensitive, so
    # "in sync" is not hit.
    r"Sync(ed)?(?!\w)",
    r"Import(ed)?(?!\w)",
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

#: A label glued by implicit concatenation to a following capitalised word:
#: ``"Sync" "All"`` reads as ``SyncAll``, which ``Sync(ed)?(?!\w)`` cannot see.
#: Matched only where a source token of a concatenated literal starts (see
#: :func:`_piece_starts`), so a one-token identifier such as ``"SyncPhaseError"``
#: is never read by it.
_TOKEN_START_LABEL = r"(Sync|Import)(ed)?(?=[A-Z])"

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
    # The name of the sibling feature, which keeps "sync" (PR-E boundary).
    re.compile(r"\bHooks Sync\b"),
)


@dataclass(frozen=True)
class _SiteExemption:
    """One exact substring, exempt only inside one function of one file.

    ``func`` is the function's ``__qualname__``: a same-named method, or a def
    nested inside the function, is a different scope and is not exempt.
    """

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
    #: ``(module, __qualname__)`` of every def the walk saw.
    defs: set[tuple[str, str]] = field(default_factory=set)


def _published_callables() -> frozenset[tuple[str, str]]:
    """``(module, __qualname__)`` of every function the product publishes.

    Read from the running registries rather than from decorators in source:
    every route ``create_app`` mounts in ``prod`` and ``dev`` mode (its
    docstring becomes the OpenAPI description) and every MCP tool — the core
    tools registered straight on ``mcp`` and the ``@register``-ed ``ACTIONS``
    (their docstrings become tool descriptions and ``mem_do`` help). A
    wrapper made with ``functools.wraps`` carries the defining function's
    ``__module__`` and ``__qualname__``; without it the registries do not name
    the defining function, and the registry tests below fail.

    The route walk follows ``original_router`` the way
    ``tests/test_web_invariants_registry.py`` does; ``_tool_manager`` is
    FastMCP-private, the coupling ``tests/test_server_instructions.py`` accepts.
    """
    from starlette.routing import Route

    from memtomem.server import mcp
    from memtomem.server.tool_registry import ACTIONS
    from memtomem.web.app import create_app

    def walk(routes: list[object]) -> Iterator[Route]:
        for route in routes:
            if isinstance(route, Route):
                yield route
            elif hasattr(route, "original_router"):
                yield from walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                yield from walk(route.routes)

    fns: list[Any] = []
    for mode in ("prod", "dev"):
        fns.extend(r.endpoint for r in walk(create_app(lifespan=None, mode=mode).routes))
    fns.extend(tool.fn for tool in mcp._tool_manager.list_tools())
    fns.extend(info.fn for info in ACTIONS.values())
    return frozenset((fn.__module__, fn.__qualname__) for fn in fns)


def _dotted(relpath: str) -> str:
    """``web/routes/x.py`` → ``memtomem.web.routes.x``; a package ``__init__`` is the package."""
    return "memtomem." + relpath.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _walk_with_scope(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    """Every node of *tree*, with the ``__qualname__`` of the scope whose body
    holds it (``""`` at module level) — except a def or class itself, which is
    paired with its own ``__qualname__``, the one its body's nodes carry.

    A def's decorators, defaults, bases and annotations are evaluated in the
    enclosing scope, so they belong to it, not to the def being defined.
    """

    def visit(node: ast.AST, qual: str, in_func: bool) -> Iterator[tuple[ast.AST, str]]:
        if isinstance(node, _FuncDef | ast.ClassDef):
            if in_func:
                inner = f"{qual}.<locals>.{node.name}"
            else:
                inner = f"{qual}.{node.name}" if qual else node.name
            yield node, inner
            body = {id(stmt) for stmt in node.body}
            for child in ast.iter_child_nodes(node):
                if id(child) in body:
                    yield from visit(child, inner, isinstance(node, _FuncDef))
                else:
                    yield from visit(child, qual, in_func)
        else:
            yield node, qual
            for child in ast.iter_child_nodes(node):
                yield from visit(child, qual, in_func)

    for child in ast.iter_child_nodes(tree):
        yield from visit(child, "", False)


def _docstring_node(node: ast.AST) -> ast.Constant | None:
    body = getattr(node, "body", None)
    if not body or not isinstance(body[0], ast.Expr):
        return None
    value = body[0].value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value
    return None


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
    relpath: str,
    tree: ast.Module,
    endpoints: frozenset[tuple[str, str]],
    defs: set[tuple[str, str]],
) -> Iterator[tuple[ast.Constant | ast.JoinedStr, str]]:
    """Each public literal of a module once, with the qualname of its scope.

    Adds ``(module, qualname)`` of every def in the module to *defs*.
    """
    module = _dotted(relpath)
    docstrings: dict[int, ast.AST] = {}
    doc = _docstring_node(tree)
    if doc is not None:
        docstrings[id(doc)] = tree
    not_top_level: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _FuncDef | ast.ClassDef):
            doc = _docstring_node(node)
            if doc is not None:
                docstrings[id(doc)] = node
        elif isinstance(node, ast.JoinedStr):
            # Fragments and format specs are part of the f-string they sit in.
            for part in node.values:
                not_top_level.add(id(part))
                if isinstance(part, ast.FormattedValue) and part.format_spec is not None:
                    not_top_level.add(id(part.format_spec))
    for node, qual in _walk_with_scope(tree):
        if isinstance(node, _FuncDef):
            defs.add((module, qual))  # the def's own qualname
            continue
        owner = docstrings.get(id(node))
        # A docstring sits in its owner's body, so *qual* is the owner's qualname.
        if isinstance(owner, _FuncDef) and (module, qual) not in endpoints:
            continue
        if isinstance(owner, ast.ClassDef) and not relpath.startswith("web/routes/"):
            continue
        if isinstance(owner, ast.Module) or id(node) in not_top_level:
            continue
        if isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)
        ):
            yield node, qual


@dataclass(frozen=True)
class _Piece:
    """One source token of a string literal: a ``STRING`` token, or a whole
    ``FSTRING_START … FSTRING_END`` run. ``start`` is (line, character column),
    and ``depth`` counts the f-/t-strings whose replacement field holds it."""

    start: tuple[int, int]
    source: str
    depth: int


def _source_lines(source: str) -> list[str]:
    """The module's lines as tokenize and the parser count them: split at
    ``\\n`` only. ``str.splitlines`` also splits at a form feed or U+2028,
    which would shift every line and column after one."""
    return io.StringIO(source).readlines()


def _string_pieces(source: str) -> list[_Piece]:
    """Every string piece of a module, in source order, from one tokenize pass.

    A t-string (3.14) run is not a piece — :func:`_template_parts` keeps its
    synthesized nodes from being split — but literals inside its replacement
    fields are, one level deeper.
    """
    lines = _source_lines(source)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    def offset(pos: tuple[int, int]) -> int:
        return line_starts[pos[0] - 1] + pos[1]

    opening = {tokenize.FSTRING_START}
    closing = {tokenize.FSTRING_END}
    # 3.14 t-strings; ``getattr`` because 3.12 / 3.13 have no such token.
    t_start, t_end = (
        getattr(tokenize, "TSTRING_START", None),
        getattr(tokenize, "TSTRING_END", None),
    )
    pieces: list[_Piece] = []
    open_runs: list[tuple[int, tuple[int, int]]] = []  # (token type, start) per open run
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in opening or tok.type == t_start:
            open_runs.append((tok.type, tok.start))
        elif tok.type in closing or tok.type == t_end:
            kind, start = open_runs.pop()
            if kind in opening:
                text = source[offset(start) : offset(tok.end)]
                pieces.append(_Piece(start, text, len(open_runs)))
        elif tok.type == tokenize.STRING:
            pieces.append(_Piece(tok.start, tok.string, len(open_runs)))
    pieces.sort(key=lambda p: p.start)
    return pieces


def _template_parts(tree: ast.Module) -> set[int]:
    """``id`` of every node a 3.14 t-string synthesizes: its text fragments and
    its interpolations' format specs. Their source spans are not literals of
    their own, so they are never split into pieces (they are still read whole)."""
    template = getattr(ast, "TemplateStr", None)
    parts: set[int] = set()
    if template is None:
        return parts
    for node in ast.walk(tree):
        if isinstance(node, template):
            for part in node.values:
                parts.add(id(part))
                spec = getattr(part, "format_spec", None)
                if spec is not None:
                    # The spec only: a literal in a field nested inside it is real.
                    parts.add(id(spec))
    return parts


class _CharColumns:
    """AST columns (UTF-8 bytes) as the character columns tokenize reports.

    An ASCII line needs no conversion; any other line gets one byte-to-
    character table, built the first time one of its columns is asked for.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._tables: dict[int, dict[int, int]] = {}

    def __call__(self, lineno: int, byte_col: int) -> int:
        line = self._lines[lineno - 1]
        if line.isascii():
            return byte_col
        table = self._tables.get(lineno)
        if table is None:
            table, at = {}, 0
            for i, ch in enumerate(line):
                table[at] = i
                at += len(ch.encode("utf-8"))
            table[at] = len(line)
            self._tables[lineno] = table
        return table[byte_col]


def _piece_starts(
    node: ast.Constant | ast.JoinedStr, pieces: list[_Piece], char_col: _CharColumns
) -> list[int]:
    """Where each piece of a ``str`` literal or f-string starts in its rendered
    value; empty for a literal of fewer than two pieces.

    The pieces are the shallowest ones inside the node's span: a literal inside
    a replacement field is one level deeper and is its own node.
    """
    start = (node.lineno, char_col(node.lineno, node.col_offset))
    end = (node.end_lineno, char_col(node.end_lineno, node.end_col_offset))  # type: ignore[arg-type]
    lo = bisect.bisect_left(pieces, start, key=lambda p: p.start)
    hi = bisect.bisect_left(pieces, end, lo=lo, key=lambda p: p.start)
    inside = pieces[lo:hi]
    if not inside:
        return []
    depth = min(p.depth for p in inside)
    top = [p for p in inside if p.depth == depth]
    if len(top) < 2:
        return []
    rendered: list[str] = []
    for piece in top:
        parsed = ast.parse(piece.source, mode="eval").body
        assert isinstance(parsed, ast.Constant | ast.JoinedStr), piece
        rendered.append(_render(parsed))
    # A renderer that disagrees with the tokens would misplace every offset.
    assert "".join(rendered) == _render(node), (node.lineno, rendered)
    starts, at = [], 0
    for text in rendered:
        starts.append(at)
        at += len(text)
    return starts


def _exempt_spans(
    value: str,
    relpath: str,
    func: str,
    span_exemptions: tuple[re.Pattern[str], ...],
    site_exemptions: tuple[_SiteExemption, ...],
) -> list[tuple[int, int, _SiteExemption | None]]:
    spans: list[tuple[int, int, _SiteExemption | None]] = []
    for rx in span_exemptions:
        spans.extend((*m.span(), None) for m in rx.finditer(value))
    for site in site_exemptions:
        if site.relpath == relpath and site.func == func:
            start = value.find(site.text)
            while start != -1:
                spans.append((start, start + len(site.text), site))
                start = value.find(site.text, start + 1)
    return spans


def _scan(
    pkg_root: Path = _PKG_ROOT,
    *,
    endpoints: frozenset[tuple[str, str]],
    roots: tuple[str, ...] = _SCAN_ROOTS,
    patterns: tuple[str, ...] = _STALE_ACTION_COPY,
    token_start: str | None = _TOKEN_START_LABEL,
    span_exemptions: tuple[re.Pattern[str], ...] = _SPAN_EXEMPTIONS,
    site_exemptions: tuple[_SiteExemption, ...] = _SITE_EXEMPTIONS,
    other_feature_files: frozenset[str] | set[str] = frozenset(_OTHER_FEATURE_FILES),
) -> ScanReport:
    report = ScanReport()
    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for root in roots:
        for path in sorted((pkg_root / root).rglob("*.py")):
            relpath = path.relative_to(pkg_root).as_posix()
            if relpath in other_feature_files:
                report.excluded.append(relpath)
            else:
                report.files.append(relpath)
                sources[relpath] = path.read_text(encoding="utf-8")
                trees[relpath] = ast.parse(sources[relpath], filename=relpath)
    compiled = [(p, re.compile(p)) for p in patterns]
    token_rx = re.compile(token_start) if token_start is not None else None
    for relpath, tree in trees.items():
        root = next(r for r in roots if relpath.startswith(r + "/"))
        pieces = _string_pieces(sources[relpath]) if token_rx is not None else []
        char_col = _CharColumns(_source_lines(sources[relpath]))
        synthesized = _template_parts(tree)
        for node, func in _public_literals(relpath, tree, endpoints, report.defs):
            report.public_literals[root] += 1
            value = _render(node)
            exempt = _exempt_spans(value, relpath, func, span_exemptions, site_exemptions)
            for _start, _end, site in exempt:
                if site is not None:
                    report.site_hits[site] += 1
            for pattern, rx in compiled:
                for m in rx.finditer(value):
                    if not any(s <= m.start() and m.end() <= e for s, e, _ in exempt):
                        report.offences.append(
                            Offence(relpath, node.lineno, node.col_offset, pattern)
                        )
                        break
            if token_rx is None or id(node) in synthesized:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
                # Not split: a UTF-8 sequence can straddle two bytes tokens, so a
                # piece has no offset of its own in the decoded value.
                continue
            # A label glued to the next word by concatenation has no boundary in
            # the value, only in the source: match where each token starts.
            for at in _piece_starts(node, pieces, char_col):
                m = token_rx.match(value, at)
                if m and not any(s <= m.start() and m.end() <= e for s, e, _ in exempt):
                    report.offences.append(
                        Offence(relpath, node.lineno, node.col_offset, token_start)  # type: ignore[arg-type]
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
def published() -> frozenset[tuple[str, str]]:
    return _published_callables()


@pytest.fixture(scope="module")
def real_scan(published: frozenset[tuple[str, str]]) -> ScanReport:
    return _scan(endpoints=published)


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


def test_every_published_callable_is_found_by_the_walk(
    published: frozenset[tuple[str, str]], real_scan: ScanReport
) -> None:
    """Each mounted route and registered tool under the scan roots matches a
    def the walk saw, so its docstring was classified, not skipped.

    Every published callable must come from a scan root, ``web/app.py`` or
    FastAPI itself (its ``openapi.json`` handler). A published function
    defined anywhere else is outside the walk, so its docstring would go
    unread; a new one fails here until the roots or this list say why.
    """
    prefixes = tuple(_dotted(root + "/x.py").removesuffix("x") for root in _SCAN_ROOTS)
    excluded = {_dotted(relpath) for relpath in _OTHER_FEATURE_FILES}
    outside = sorted(
        m
        for m, _ in published
        if not m.startswith(prefixes) and m != "memtomem.web.app" and m != "fastapi.applications"
    )
    assert not outside, f"published from outside the scan roots: {outside}"
    in_roots = {(m, q) for m, q in published if m.startswith(prefixes) and m not in excluded}
    missing = sorted(in_roots - real_scan.defs)
    assert not missing, f"published but not found by the walk: {missing}"
    per_root = Counter(
        next(r for r in _SCAN_ROOTS if m.startswith(_dotted(r))) for m, _ in in_roots
    )
    # Floors, well under today's counts, so an empty registry cannot pass.
    assert per_root["web/routes"] > 100, per_root
    assert per_root["server/tools"] > 50, per_root


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
    """Planted modules are not mounted or registered, so no docstring of theirs
    is published unless the test names the def in ``endpoints``."""
    kwargs.setdefault("endpoints", frozenset())
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
            _ROUTE,
            'def x(t=Query("u", description="the tier to import into")):\n    pass\n',
            {(1, r"tier to import into"), (1, r"\bto import\b")},
            id="query-description",
        ),
        pytest.param(
            _ROUTE,
            '@router.get("/x", summary=("Run " "Sync skills"))\nasync def x():\n    pass\n',
            {(1, r"Sync(ed)?(?!\w)")},
            id="decorator-summary-concatenated",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{kind}" f"Import skills"\n',
            {(1, r"Import(ed)?(?!\w)")},
            id="fstring-fstring-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{kind}" "Import skills"\n',
            {(1, r"Import(ed)?(?!\w)")},
            id="fstring-plain-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = "Auto" "Sync skills"\n',
            {(1, r"Sync(ed)?(?!\w)")},
            id="word-glued-by-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = f"Auto" f"Import skills"\n',
            {(1, r"Import(ed)?(?!\w)")},
            id="word-glued-by-fstring-concat",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Import"}\n',
            {(1, r"^(Sync|Import)(ed)?$"), (1, r"Import(ed)?(?!\w)")},
            id="bare-action-label",
        ),
        pytest.param(
            _ENGINE,
            """X = 'Example: {"action": "Import"}'\n""",
            {(1, r'"action": "(Sync|Import)"'), (1, r"Import(ed)?(?!\w)")},
            id="embedded-action-label",
        ),
        pytest.param(
            _ENGINE,
            'X = b"Import skills"\n',
            {(1, r"Import(ed)?(?!\w)")},
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
            {(1, r"Import(ed)?(?!\w)")},
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
            {(1, r"Import(ed)?(?!\w)")},
            id="label-then-colon",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Sync" "/Import"}\n',
            {(1, r"Sync(ed)?(?!\w)"), (1, r"Import(ed)?(?!\w)")},
            id="label-then-slash",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Sync" "-skills"}\n',
            {(1, r"Sync(ed)?(?!\w)")},
            id="label-then-hyphen",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Sync" "!"}\n',
            {(1, r"Sync(ed)?(?!\w)")},
            id="label-then-punctuation",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Import" "…"}\n',
            {(1, r"Import(ed)?(?!\w)")},
            id="label-then-ellipsis",
        ),
        pytest.param(
            _ENGINE,
            'X = "SyncPhaseError raised; Imports are pinned"\n',
            set(),
            id="identifier-is-not-a-label",
        ),
        pytest.param(
            _ENGINE,
            'X = {"action": "Sync" "All"}\n',
            {(1, _TOKEN_START_LABEL)},
            id="label-glued-to-a-capitalised-word",
        ),
        pytest.param(
            _ENGINE,
            'X = f"{x}" "ImportSkills"\n',
            {(1, _TOKEN_START_LABEL)},
            id="label-glued-after-an-fstring",
        ),
        pytest.param(
            _ENGINE,
            'X = "Run " f"SyncAll {x}"\n',
            {(1, _TOKEN_START_LABEL)},
            id="glued-label-starts-an-fstring-piece",
        ),
        pytest.param(
            _ENGINE,
            'X = "Auto" "SyncedAll"\n',
            {(1, _TOKEN_START_LABEL)},
            id="glued-past-tense-label",
        ),
        pytest.param(
            _ENGINE,
            'X = b"Sync" b"All"\n',
            set(),
            id="bytes-are-read-whole-only-a-stated-limit",
        ),
        pytest.param(
            _ENGINE,
            'X = b"\\xc3" b"\\xa7\\"" b"Sync" b"All\\""\n',
            set(),
            id="bytes-with-a-utf8-sequence-split-across-tokens",
        ),
        pytest.param(
            _ENGINE,
            'X = "Sync" "All" "ImportSkills"\n',
            {(1, _TOKEN_START_LABEL)},
            id="two-glued-labels-in-one-literal-are-one-offence",
        ),
        pytest.param(
            _ENGINE,
            'é = "Sync" "All"\n',
            {(1, _TOKEN_START_LABEL)},
            id="non-ascii-before-the-literal-on-its-line",
        ),
        pytest.param(
            _ENGINE,
            'X = ["é", "Sync" "All"]\n',
            {(1, _TOKEN_START_LABEL)},
            id="non-ascii-literal-before-it-on-the-line",
        ),
        pytest.param(
            _ENGINE,
            'X = ("Run "\n     "Sync"\n     "All")\n',
            {(1, _TOKEN_START_LABEL)},
            id="glued-label-across-lines",
        ),
        pytest.param(
            _ENGINE,
            'X = "a" \\\n    "SyncAll"\n',
            {(1, _TOKEN_START_LABEL)},
            id="glued-label-after-a-line-continuation",
        ),
        pytest.param(
            _ENGINE,
            "X = f\"{'Sync' 'All'}\"\n",
            {(1, _TOKEN_START_LABEL)},
            id="glued-label-inside-a-replacement-field-once",
        ),
        pytest.param(
            _ENGINE,
            'X = "SyncPhaseError" " raised"\n',
            {(1, _TOKEN_START_LABEL)},
            id="identifier-starting-a-concatenation-errs-toward-flagging",
        ),
        pytest.param(
            _ENGINE,
            'X = "x" "Syncs"\nY = "x" "Synchronise"\nZ = "AgentSyncResult"\n',
            set(),
            id="token-start-needs-a-capital-and-two-pieces",
        ),
        pytest.param(
            _ENGINE,
            '__all__ = ["SkillSyncResult", "SyncPhaseError"]\n',
            set(),
            id="one-token-identifiers",
        ),
        pytest.param(
            _ENGINE,
            'X = ("a"  # "SyncAll"\n     "b")\n',
            set(),
            id="comment-between-pieces-is-not-a-piece",
        ),
        pytest.param(
            _ENGINE,
            '\x0cX = "Sync" "All"\n',
            {(1, _TOKEN_START_LABEL)},
            id="form-feed-is-not-a-line-break",
        ),
        pytest.param(
            _ENGINE,
            '# \u2028abcdefghiABCDEFGHI1234567890\nY = f"{x}" "ImportSkills"\n',
            {(2, _TOKEN_START_LABEL)},
            id="line-separator-in-a-comment-is-not-a-line-break",
        ),
        pytest.param(
            _ENGINE,
            'X = "Run Sync" "All"\n',
            set(),
            id="label-glued-mid-piece-is-a-stated-limit",
        ),
        pytest.param(
            _ENGINE,
            'X = f"see #123 \\"Import skills\\""\n',
            {(1, r"Import(ed)?(?!\w)")},
            id="quoted-label-beside-issue-number",
        ),
    ],
)
def test_scan_flags_planted_shape(
    tmp_path: Path, relpath: str, source: str, expected: set[tuple[int, str]]
) -> None:
    assert _found(tmp_path, {relpath: source}) == {(relpath, ln, p) for ln, p in expected}


@pytest.mark.parametrize(
    ("relpath", "source", "qualname", "line"),
    [
        pytest.param(
            _TOOL,
            'def mem_x():\n    """Import skills from a runtime."""\n',
            "mem_x",
            2,
            id="tool",
        ),
        pytest.param(
            _ROUTE,
            'class Api:\n    async def x(self):\n        """Sync the skills."""\n',
            "Api.x",
            3,
            id="method",
        ),
        pytest.param(
            _ROUTE,
            'def mount():\n    async def x():\n        """Sync the skills."""\n',
            "mount.<locals>.x",
            3,
            id="nested-def",
        ),
    ],
)
def test_a_def_docstring_is_public_exactly_when_the_def_is_published(
    tmp_path: Path, relpath: str, source: str, qualname: str, line: int
) -> None:
    """Whether a docstring is published is the registries' answer, keyed by
    ``(module, __qualname__)`` — not a decorator read from source."""
    module = _dotted(relpath)
    report = _scan(_plant(tmp_path / "d", {relpath: source}), endpoints=frozenset())
    assert (module, qualname) in report.defs
    assert _found(tmp_path / "a", {relpath: source}) == set()
    found = _found(tmp_path / "b", {relpath: source}, endpoints=frozenset({(module, qualname)}))
    assert {(f, ln) for f, ln, _ in found} == {(relpath, line)}
    other = frozenset({(module, qualname + "_other")})
    assert _found(tmp_path / "c", {relpath: source}, endpoints=other) == set()


def test_walk_qualnames_match_cpython() -> None:
    """The walk's qualnames are CPython's ``__qualname__`` for every def and
    class, read from the compiled code objects' ``co_qualname``."""
    source = textwrap.dedent(
        """
        def top(): pass
        async def atop(): pass
        class C:
            def m(self):
                def inner(): pass
                class Local:
                    def lm(self): pass
            class Nested:
                async def n(self): pass
        def factory():
            async def endpoint(): pass
            class K:
                def k(self): pass
            f = lambda: None
            return [x for x in ()]
        """
    )

    def qualnames(code: types.CodeType) -> Iterator[str]:
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                if not const.co_name.startswith("<"):  # lambdas, comprehensions
                    yield const.co_qualname
                yield from qualnames(const)

    expected = set(qualnames(compile(source, "<planted>", "exec")))
    walked = {
        qual
        for node, qual in _walk_with_scope(ast.parse(source))
        if isinstance(node, _FuncDef | ast.ClassDef)
    }
    assert walked == expected
    assert "factory.<locals>.endpoint" in walked and "C.m.<locals>.Local.lm" in walked


def test_published_callables_reads_the_live_registries(
    published: frozenset[tuple[str, str]],
) -> None:
    """Spot-checks with one witness per registry: a decorated route, a route
    registered with ``add_api_route`` in another module, a core MCP tool and a
    ``@register``-ed action."""
    from memtomem.server import mcp
    from memtomem.server.tool_registry import ACTIONS
    from memtomem.web.routes import context_skills

    # Defined in ``namespaces``, mounted by ``namespaces_read.add_api_route``.
    assert ("memtomem.web.routes.namespaces", "list_namespaces") in published
    # Mounted only in ``dev`` mode (``_DEV_ONLY_ROUTERS``).
    assert ("memtomem.web.routes.namespaces", "delete_namespace") in published
    assert ("memtomem.web.routes.context_skills", "import_skill_to_user") in published
    assert hasattr(context_skills, "import_skill_to_user")
    core = {t.name for t in mcp._tool_manager.list_tools()}
    assert "mem_search" in core
    assert ("memtomem.server.tools.search", "mem_search") in published
    assert "context_detect" in ACTIONS
    assert ("memtomem.server.tools.context", "mem_context_detect") in published


def test_class_docstrings_are_public_only_under_web_routes(tmp_path: Path) -> None:
    source = 'class Report:\n    """Sync report for one runtime."""\n'
    assert _found(tmp_path / "a", {_ROUTE: source}) == {(_ROUTE, 2, r"Sync(ed)?(?!\w)")}
    assert _found(tmp_path / "b", {_ENGINE: source}) == set()
    assert _found(tmp_path / "c", {_TOOL: source}) == set()


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
            '@router.post("/x")\ndef f():\n    """Import skills from a runtime."""\n',
            '@router.post("/x", summary="Import skills from a runtime.")\ndef f():\n    pass\n',
            id="decorated-but-unregistered-docstring",
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
            'X = "Syncs§\\"Design\\""\n',
            'X = "Synced: §\\"Design\\""\n',
            id="glued-citation-creates-no-match",
        ),
        pytest.param(
            'X = "Configure Hooks Sync for this project."\n',
            'X = "Configure Hooks Sync, or Sync skills."\n',
            id="sibling-feature-name",
        ),
    ],
)
def test_span_exemptions_cover_only_their_span(tmp_path: Path, exempt: str, control: str) -> None:
    assert _found(tmp_path / "a", {_ENGINE: exempt}) == set()
    assert _found(tmp_path / "b", {_ENGINE: control}) != set()


def test_a_span_exemption_crossing_pieces_clears_a_token_start_match(tmp_path: Path) -> None:
    """Exemptions are spans of the whole value, so one that spans two tokens
    still covers a glued label inside it."""
    exempt = {_ENGINE: "X = 'ADR-0021 §\"' \"Sync\" 'All\" deferred this.'\n"}
    control = {_ENGINE: "X = 'ADR-0021 ' \"Sync\" 'All deferred this.'\n"}
    assert _found(tmp_path / "a", exempt) == set()
    assert _found(tmp_path / "b", control) == {(_ENGINE, 1, _TOKEN_START_LABEL)}


def test_a_site_exemption_clears_a_token_start_match_and_counts_once(tmp_path: Path) -> None:
    site = _SiteExemption(_ENGINE, "f", "ImportSkills does not overwrite", "planted")
    source = {_ENGINE: 'def f():\n    return "x. " "Import" "Skills does not overwrite"\n'}
    report = _scan(_plant(tmp_path / "a", source), endpoints=frozenset(), site_exemptions=(site,))
    assert report.offences == []
    assert report.site_hits[site] == 1
    assert _found(tmp_path / "b", source, site_exemptions=()) == {(_ENGINE, 2, _TOKEN_START_LABEL)}


def test_token_start_check_reads_a_deeply_nested_replacement_field(tmp_path: Path) -> None:
    """Tokenizing the module once adds no nesting level: a field 199
    parentheses deep, which a re-tokenized and re-parenthesised span would
    refuse, is read, and the literal inside it is checked."""
    source = 'X = f"{' + "(" * 199 + "'Sync' 'All'" + ")" * 199 + '}"\n'
    assert _found(tmp_path, {_ENGINE: source}) == {(_ENGINE, 1, _TOKEN_START_LABEL)}


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings are 3.14 syntax")
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('X = t"("\n', set(), id="fragment-is-not-a-literal-span"),
        # Split so the synthesized debug text ``'Sy' 'nc' 'All'=`` names no
        # label: the one offence is the inner literal's token-start match.
        pytest.param(
            "X = t\"{'Sy' 'nc' 'All'=}\"\n", {(1, _TOKEN_START_LABEL)}, id="debug-field-text"
        ),
        pytest.param(
            "X = t\"{name:{'Sync' 'All'}}\"\n", {(1, _TOKEN_START_LABEL)}, id="format-spec-field"
        ),
    ],
)
def test_token_start_check_skips_template_string_synthesized_nodes(
    tmp_path: Path, source: str, expected: set[tuple[int, str]]
) -> None:
    """A t-string's fragments and format specs have spans that are not
    literals of their own; only a real literal inside a field is split."""
    assert _found(tmp_path, {_ENGINE: source}) == {(_ENGINE, ln, p) for ln, p in expected}


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
    assert _found(tmp_path, files) == {("web/routes/export_more.py", 1, r"Import(ed)?(?!\w)")}


def test_site_exemption_does_not_reach_a_nested_def_or_a_same_named_method(
    tmp_path: Path,
) -> None:
    site = _SITE_EXEMPTIONS[0]
    source = (
        "def apply_settings_sync():\n"
        "    def helper():\n"
        '        return "Settings sync timed out"\n\n'
        "class Gateway:\n"
        "    def apply_settings_sync(self):\n"
        '        return "Settings sync timed out"\n'
    )
    pattern = r"(?i)\b(sync|import) timed out"
    assert _found(tmp_path, {site.relpath: source}) == {
        (site.relpath, 3, pattern),
        (site.relpath, 7, pattern),
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


def test_a_match_reaching_past_an_exempt_span_is_kept(tmp_path: Path) -> None:
    """Only a match wholly inside an exempt span is dropped: ``artifact-Import``
    overlaps the site span ``Import does not overwrite`` but starts before it."""
    site = next(s for s in _SITE_EXEMPTIONS if s.text == "Import does not overwrite")
    inside = f'def {site.func}():\n    raise E("x. Import does not overwrite.")\n'
    across = f'def {site.func}():\n    raise E("x. artifact-Import does not overwrite.")\n'
    assert _found(tmp_path / "a", {site.relpath: inside}) == set()
    assert _found(tmp_path / "b", {site.relpath: across}) == {
        (site.relpath, 2, r"(?i)\b\w+-import(ed|s|ing)?\b")
    }


def test_site_exemption_counts_each_occurrence(tmp_path: Path) -> None:
    site = _SITE_EXEMPTIONS[0]
    source = (
        "def apply_settings_sync():\n"
        '    raise E("Settings sync timed out. Settings sync timed out.")\n'
    )
    report = _scan(_plant(tmp_path, {site.relpath: source}), endpoints=frozenset())
    assert report.offences == []
    assert report.site_hits[site] == 2  # so the exactly-once check above fails


def test_two_literals_on_one_line_are_two_offences(tmp_path: Path) -> None:
    report = _scan(
        _plant(tmp_path, {_ENGINE: 'X = ["Sync skills", "Sync agents"]\n'}), endpoints=frozenset()
    )
    assert [(o.line, o.col) for o in report.offences] == [(1, 5), (1, 20)]


#: The line-scanner predecessor's patterns, verbatim, each with a source line it
#: matched and the pattern of :data:`_STALE_ACTION_COPY` that now covers it.
_LEGACY = (
    (r'"Sync(ed)?[: ]', 'X = "Synced: 3 skills"', r"Sync(ed)?(?!\w)"),
    (r'f"Sync ', 'X = f"Sync {kind}"', r"Sync(ed)?(?!\w)"),
    (r'"Import(ed)? ', 'X = "Imported 3 skills"', r"Import(ed)?(?!\w)"),
    (r'f"Import ', 'X = f"Import {kind}"', r"Import(ed)?(?!\w)"),
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
    (r'"action": "(Sync|Import)"', 'X = {"action": "Import" "\\nskills"}', r"Import(ed)?(?!\w)"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Import" ": Skills"}', r"Import(ed)?(?!\w)"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Sync" "-skills"}', r"Sync(ed)?(?!\w)"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Sync" "!"}', r"Sync(ed)?(?!\w)"),
    (r'"action": "(Sync|Import)"', 'X = {"action": "Import" "…"}', r"Import(ed)?(?!\w)"),
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
