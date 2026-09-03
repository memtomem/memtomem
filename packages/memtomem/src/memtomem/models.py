"""Core data models for memtomem."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

#: ``ChunkMetadata.origin`` value stamped on summaries written by the
#: consolidation policy (``memtomem.tools.consolidation_engine``). It is the
#: ownership proof ``_clear_policy_summaries`` deletes on: no ingress surface
#: (``mem_add``, indexing, the agent-path ``mem_consolidate_apply``) accepts an
#: origin, so a chunk carrying this value can only have come from
#: ``_make_summary_chunk`` or the one-shot migration backfill that adopts
#: summaries written before the column existed.
ORIGIN_CONSOLIDATION_POLICY = "consolidation_policy"

#: The origins a chunk may legitimately carry. Stored as TEXT, so a value from
#: an older or newer writer decodes verbatim and simply never equals a known
#: constant — an unrecognised origin is treated as "not ours", never as owned.
ChunkOrigin = Literal["consolidation_policy"]


class ChunkType(StrEnum):
    MARKDOWN_SECTION = "markdown_section"
    PYTHON_FUNCTION = "python_function"
    PYTHON_CLASS = "python_class"
    JS_FUNCTION = "js_function"
    RST_SECTION = "rst_section"
    RAW_TEXT = "raw_text"
    PROCEDURE = "procedure"


@dataclass(frozen=True, slots=True)
class ChunkMetadata:
    source_file: Path
    heading_hierarchy: tuple[str, ...] = ()
    chunk_type: ChunkType = ChunkType.RAW_TEXT
    start_line: int = 0
    end_line: int = 0
    language: str = "en"
    tags: tuple[str, ...] = ()
    namespace: str = "default"
    overlap_before: int = 0  # chars of overlap with previous chunk
    overlap_after: int = 0  # chars of overlap with next chunk
    parent_context: str = ""  # parent heading or document title
    file_context: str = ""  # filename + heading outline
    # Validity window from frontmatter (RFC: temporal-validity).
    # NULL means unbounded on that side; both NULL means always-valid.
    valid_from_unix: int | None = None
    valid_to_unix: int | None = None
    # Scope axis (ADR-0011). ``user`` is the only scope today; project_shared
    # / project_local are reserved for the read/write surface PRs.
    # ``project_root`` is None for user scope, an absolute path for project
    # scopes — required so a single user-local DB can hold chunks from
    # multiple worktrees of the same project without path-prefix collisions.
    scope: str = "user"
    project_root: Path | None = None
    # Provenance of the writer that produced this chunk (#2161). ``None`` for
    # everything a user or agent writes — the only value in use is
    # ``ORIGIN_CONSOLIDATION_POLICY``, and only the consolidation policy sets
    # it. Ownership over the virtual summary path is decided on this field
    # rather than on a namespace/tag combination a user chunk can reproduce.
    origin: str | None = None


def _like_glob_matches(pattern: str, value: str) -> bool:
    """Evaluate a user glob the way the SQL layer's ``LIKE`` would.

    The namespace/scope SQL emitters translate a user pattern by escaping
    ``_`` and turning ``*`` into ``%``, then compare with
    ``LIKE ? ESCAPE '\\'`` (:func:`memtomem.storage.sqlite_helpers.
    namespace_sql`, :func:`memtomem.storage.sqlite_scope._scopes_glob_clause`).
    Three consequences of that contract are easy to get wrong in Python and
    are pinned by the parity tests:

    - A literal ``%`` the user typed is **not** escaped by the emitter, so it
      is a wildcard here too.
    - ``_`` is escaped, so it matches only itself — unlike ``fnmatch``, which
      would also read ``?`` and ``[…]`` as wildcards. Never use ``fnmatch``.
    - SQLite's default ``LIKE`` folds case for **ASCII only**, so ``Ü`` does
      not match ``ü``.

    A pattern ending in a lone escape character has nothing to escape and
    matches nothing in SQLite, so it returns ``False`` here rather than
    falling back to a literal backslash.
    """
    sql_pattern = _ascii_fold(pattern.replace("_", r"\_").replace("*", "%"))

    # Tokenized and matched with the two-pointer LIKE algorithm rather than
    # compiled to a regex. A regex renders every ``%`` as a greedy ``.*``, and
    # a chain of those against a value that cannot match backtracks through
    # every way of splitting the value between them: 20 wildcards took ~16s on
    # a 14-character value, which a caller reaches by typing a query
    # parameter. This walks the value once per wildcard instead — worst case
    # O(len(pattern) x len(value)), with no input that blows up.
    tokens: list[tuple[str, str]] = []  # ("any", "") | ("one", "") | ("lit", ch)
    i = 0
    while i < len(sql_pattern):
        ch = sql_pattern[i]
        if ch == "\\":
            if i + 1 >= len(sql_pattern):
                return False
            tokens.append(("lit", sql_pattern[i + 1]))
            i += 2
            continue
        tokens.append(("any", "") if ch == "%" else ("one", "") if ch == "_" else ("lit", ch))
        i += 1

    folded = _ascii_fold(value)
    # ``star`` remembers the last ``%`` and ``mark`` the value position it was
    # first credited with, so a dead end backtracks by giving that one wildcard
    # a single extra character — never by re-splitting the earlier ones.
    t = v = 0
    star = mark = -1
    while v < len(folded):
        if t < len(tokens) and (
            tokens[t][0] == "one" or (tokens[t][0] == "lit" and tokens[t][1] == folded[v])
        ):
            t += 1
            v += 1
        elif t < len(tokens) and tokens[t][0] == "any":
            star = t
            mark = v
            t += 1
        elif star != -1:
            t = star + 1
            mark += 1
            v = mark
        else:
            return False
    return all(token[0] == "any" for token in tokens[t:])


def _ascii_fold(value: str) -> str:
    """Lowercase ASCII letters only, matching SQLite ``LIKE`` case folding."""
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in value)


def has_namespace_prefix(namespace: str, prefixes: Sequence[str]) -> bool:
    """Whether ``namespace`` starts with any of ``prefixes``.

    The Python twin of the ``namespace NOT LIKE ? ESCAPE '\\'`` conjuncts
    :func:`memtomem.storage.sqlite_helpers.namespace_sql` emits for
    ``exclude_prefixes``: the prefix is escaped there, so ``%`` and ``_``
    inside one are literal, and the comparison folds ASCII case. Callers that
    need "is this a system namespace" — context-window expansion, which has
    to answer that for a chunk it already holds — share this instead of
    re-deriving the rule.
    """
    folded = _ascii_fold(namespace)
    return any(folded.startswith(_ascii_fold(p)) for p in prefixes)


class InvalidFilterSyntaxError(ValueError):
    """A namespace/scope filter argument cannot be expressed as one filter.

    A ``ValueError`` subclass so the surfaces that already translate one
    carry it without new plumbing: ``server/error_handler.tool_handler``
    renders it as ``"Error: …"``, and the web app maps it to HTTP 400.
    """


class InvalidNamespaceFilterError(InvalidFilterSyntaxError):
    """The ``namespace`` argument mixes a comma list with a glob."""


class InvalidScopeFilterError(InvalidFilterSyntaxError):
    """The ``scope`` argument cannot be honored as written.

    Two failures, raised from two places: :meth:`ScopeFilter.parse` raises
    it for a comma/glob mix, which no single filter can express; and
    :func:`memtomem.services.search_service.validate_scope_vocabulary`
    raises it for a value outside the ADR-0011 tier vocabulary. Surfaces
    that render this must use the exception's own message rather than
    assuming the mix.
    """


@dataclass(frozen=True, slots=True)
class NamespaceFilter:
    """Filter for namespace-scoped queries.

    Supports exact match (single or union), glob patterns, comma-separated
    lists, and default-search exclusion of system namespace prefixes (e.g.
    ``archive:``). Exclusion is applied *only* when no explicit namespace
    is given — the idea is that callers who ask for ``archive:summary``
    directly have already opted in.

    A comma list and a glob are **mutually exclusive** spellings, not
    composable ones: the SQL layer emits either an ``IN (…)`` list or a
    single ``LIKE`` pattern (:func:`memtomem.storage.sqlite_helpers.
    namespace_sql`), and there is no representation for a union of
    patterns. Combining them is rejected rather than silently read as one
    pattern that matches nothing.
    """

    namespaces: tuple[str, ...] = ()
    pattern: str | None = None
    exclude_prefixes: tuple[str, ...] = ()

    @staticmethod
    def parse(
        value: str | list[str] | None,
        system_prefixes: tuple[str, ...] | list[str] | None = None,
    ) -> NamespaceFilter | None:
        """Parse a user-supplied namespace argument into a filter.

        When ``value`` is ``None`` or an empty list, and ``system_prefixes``
        is non-empty, the returned filter carries ``exclude_prefixes`` so
        default searches hide system-generated namespaces (``archive:*`` by
        default) without affecting explicit queries. When ``value`` names any
        namespace (exact string, comma list, glob, non-empty list),
        ``system_prefixes`` is ignored — the caller explicitly opted into
        whatever namespace they named.

        An empty list is normalised to the ``None`` path because it names no
        namespace, so it is not an opt-in to anything. Read as a plain
        filter it would emit no SQL predicate at all and ``matches`` would
        admit everything, which turns "no namespace given" into "hide
        nothing" — the opposite of the default.

        Raises:
            InvalidNamespaceFilterError: the value mixes a comma list with a
                glob. ``*`` is checked before ``,``, so such a value would
                otherwise become one pattern containing a literal comma and
                match nothing — a silent empty result set rather than an
                answer the caller can act on.
        """
        prefixes = tuple(system_prefixes) if system_prefixes else ()

        if value is None or (isinstance(value, list) and not value):
            if prefixes:
                return NamespaceFilter(exclude_prefixes=prefixes)
            return None
        if isinstance(value, list):
            return NamespaceFilter(namespaces=tuple(value))
        if "*" in value and "," in value:
            raise InvalidNamespaceFilterError(
                f"namespace {value!r} mixes a comma list with a glob, which cannot be "
                "expressed as one filter. Use either a comma list of exact names "
                '("work,personal") or a single glob ("proj:*"), and issue one query '
                "per pattern when you need several."
            )
        if "*" in value:
            return NamespaceFilter(pattern=value)
        if "," in value:
            return NamespaceFilter(namespaces=tuple(v.strip() for v in value.split(",")))
        return NamespaceFilter(namespaces=(value,))

    def matches(self, namespace: str) -> bool:
        """Evaluate this filter in Python, mirroring :func:`namespace_sql`.

        Used where a chunk has already been fetched and the filter has to be
        applied without a second query — context-window neighbours, which are
        read in bulk per source file. The branch order and comparison
        semantics must stay identical to the SQL emitter (exact ``IN`` is
        case-sensitive, ``LIKE`` folds ASCII case); the parity tests execute
        both against the same value matrix.
        """
        if self.namespaces:
            return namespace in self.namespaces
        if self.pattern:
            return _like_glob_matches(self.pattern, namespace)
        if self.exclude_prefixes:
            return not has_namespace_prefix(namespace, self.exclude_prefixes)
        return True


@dataclass(frozen=True, slots=True)
class ScopeFilter:
    """Filter for scope-axis (ADR-0011) queries.

    Sibling of :class:`NamespaceFilter`. Supports exact match (single or
    union) and glob patterns over the three scope values
    (``user`` / ``project_shared`` / ``project_local``). Comma-separated
    lists are normalised to the union form.

    The "context boundary" — the always-on rule that out-of-project
    searches see only ``user`` and in-project searches see ``user`` plus
    the current project's project-tier rows — lives in the SQL helper
    (:func:`memtomem.storage.sqlite_scope.scope_context_sql`), not in
    the filter itself. This keeps the filter a pure user-intent value;
    the helper is the place where caller intent meets project context.
    """

    scopes: tuple[str, ...] = ()
    pattern: str | None = None

    @staticmethod
    def parse(value: str | list[str] | None) -> ScopeFilter | None:
        """Parse a user-supplied scope argument into a filter.

        ``None`` returns ``None`` (caller falls back to the always-on
        context-boundary rule). An empty list is deliberately *not*
        normalised to ``None`` the way :meth:`NamespaceFilter.parse` does
        (#2232): both consumers of the resulting empty filter already read
        it as "no intent" rather than "everything" —
        :func:`memtomem.storage.sqlite_scope.scope_context_sql` falls back
        to the boundary rule, and
        :func:`memtomem.search.visibility.neighbor_visible` refuses to treat
        it as an opt-in — so collapsing it here would change nothing and
        would only make those two guards look like dead code.
        ``"project_*"`` parses as a glob. Comma
        list and bare exact match work the same as for namespaces. Parsing
        and vocabulary are deliberately separate concerns: this parser
        builds a predicate out of whatever it is handed and does NOT check
        the value against ``user`` / ``project_shared`` / ``project_local``,
        so callers who need an unrecognized tier to reach no rows rather
        than raise still get that (portable eval cases, the CLI's
        empty-state diagnostics). The *public* vocabulary is enforced one
        level up, in
        :func:`memtomem.services.search_service.validate_scope_vocabulary`,
        which the three *search* surfaces (``GET /api/search``,
        ``mem_search``, ``mm search``) call before they open anything — so a
        misspelled tier is the same answer on HTTP, MCP and the CLI. Recall
        does not: it parses ``scope`` here directly, and an unrecognized tier
        stays an empty result set there.

        Mixing a comma list with a glob is rejected for the same reason as
        in :meth:`NamespaceFilter.parse` — the two spellings map to
        different SQL shapes and cannot be combined.

        Raises:
            InvalidScopeFilterError: the value mixes a comma list with a glob.
        """
        if value is None:
            return None
        if isinstance(value, list):
            return ScopeFilter(scopes=tuple(value))
        if "*" in value and "," in value:
            raise InvalidScopeFilterError(
                f"scope {value!r} mixes a comma list with a glob, which cannot be "
                'expressed as one filter. Use either a comma list ("user,project_local") '
                'or a single glob ("project_*").'
            )
        if "*" in value:
            return ScopeFilter(pattern=value)
        if "," in value:
            return ScopeFilter(scopes=tuple(v.strip() for v in value.split(",")))
        return ScopeFilter(scopes=(value,))

    def matches(self, scope: str) -> bool:
        """Evaluate the *explicit* part of this filter in Python.

        Mirrors the scope clause :func:`memtomem.storage.sqlite_scope.
        scope_context_sql` builds from the filter itself — **not** the
        always-on context boundary it wraps that clause in. Callers that need
        the boundary too (context-window neighbours) must apply the
        project-root rule alongside this predicate, exactly as the SQL does.
        An empty filter matches everything, mirroring the emitter's fallback
        to the no-filter context rule.
        """
        if self.scopes:
            return scope in self.scopes
        if self.pattern:
            return _like_glob_matches(self.pattern, scope)
        return True


@dataclass(slots=True)
class Chunk:
    content: str
    metadata: ChunkMetadata
    id: UUID = field(default_factory=uuid4)
    content_hash: str = ""
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.content_hash:
            import unicodedata

            self.content_hash = hashlib.sha256(
                unicodedata.normalize("NFC", self.content).encode()
            ).hexdigest()

    @property
    def retrieval_content(self) -> str:
        """Content with heading hierarchy prefix for embedding and BM25.

        chunk.content stores the pure text (no hierarchy prefix).
        This property prepends the hierarchy for retrieval quality.
        """
        h = self.metadata.heading_hierarchy
        if not h:
            return self.content
        prefix = " > ".join(h)
        return f"{prefix}\n\n{self.content}"


@dataclass(frozen=True, slots=True)
class ContextInfo:
    """Contextual information for a search result chunk."""

    window_before: tuple[Chunk, ...] = ()
    window_after: tuple[Chunk, ...] = ()
    parent_content: str | None = None
    parent_heading: str | None = None
    sibling_count: int = 0
    chunk_position: int = 0  # 1-indexed
    total_chunks_in_file: int = 0
    context_tier_used: str | None = None  # "full" | "standard" | "minimal" | None
    ranked_siblings: tuple[object, ...] = ()  # RankedSibling instances (Feature C)
    related_chunks: tuple[object, ...] = ()  # cross-source related chunks (Feature I)


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: Chunk
    score: float
    rank: int
    source: str  # "bm25", "dense", "fused", "reranked", "session_rescue"
    context: ContextInfo | None = None
    # True when this chunk surfaced (at least in part) via the
    # Stage-1 session-summary rescue leg — i.e. an
    # ``archive:session:*`` summary above threshold pointed at this
    # chunk's source file. Preserved by fusion / dedup / decay / MMR /
    # access / importance via OR semantics: if any leg of fusion (or
    # any pre-stage candidate) carried the flag, the merged result
    # carries it too. Surfaced in ``output_format="structured"``
    # payloads only — irrelevant to compact/verbose text formats.
    via_session_summary: bool = False


@dataclass(frozen=True, slots=True)
class ChunkLink:
    """Structured provenance link between two chunks.

    Mirrors a row of the ``chunk_links`` SQL table (see
    ``planning/mem-agent-share-chunk-links-rfc.md``). ``source_id`` is
    ``None`` when the source chunk has been deleted — the FK is
    ``ON DELETE SET NULL``, so the destination chunk and the link row
    survive, but the structured pointer back to the source is gone.
    """

    target_id: UUID
    link_type: str
    namespace_target: str
    created_at: datetime
    source_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class IndexingStats:
    total_files: int
    total_chunks: int
    indexed_chunks: int
    skipped_chunks: int
    deleted_chunks: int
    duration_ms: float
    errors: tuple[str, ...] = ()
    # IDs of chunks actually upserted during this run. Empty when nothing new
    # was written (all candidates were unchanged) or on the zero-result paths
    # (missing file, too large, binary, etc). Consumers that need to act on
    # freshly created chunks — e.g. ``mem_consolidate_apply`` linking a new
    # summary — should read this instead of polling ``recall_chunks``.
    new_chunk_ids: tuple[UUID, ...] = ()
    # Hybrid per-file namespace echo, distinct and in stable (sort) order.
    # Successful upserts contribute the authoritative in-lock resolution.
    # In bulk runs, files with no namespace-bearing write — including
    # unchanged, skipped, privacy-blocked, and failed files — contribute the
    # pre-write preview instead. Such a preview says where the file would land;
    # it is not evidence that any chunk was written. Single-file no-write and
    # zero-result paths are empty. ``None`` represents the
    # ``default_namespace == "default"`` carve-out (untagged chunks). Surfaced
    # in the web ``IndexResponse`` so the UI can accurately echo concurrent
    # namespace moves while preserving historical bulk preview behavior.
    resolved_namespaces: tuple[str | None, ...] = ()
    # ADR-0006 PR-A: files skipped by the secret-redaction gate during
    # un-adjudicated bulk indexing (each such file raised ``PrivacyRejection``,
    # was recorded here, and the run continued). ``blocked_files`` is the count;
    # ``blocked_paths`` are the absolute paths so surfaces can list them.
    blocked_files: int = 0
    blocked_paths: tuple[str, ...] = ()
    # Subset of ``blocked_files`` whose scope is ``project_shared`` — those are
    # hard-refused even with ``force_unsafe`` (ADR-0011 §5), so surfaces must
    # not tell the user to retry with ``--force-unsafe`` for them.
    blocked_project_shared_files: int = 0
    # Subset of ``errors`` (same strings) whose cause was typed
    # ``RetryableError`` — e.g. a mid-run namespace lookup the store could
    # not answer (issue #2018). ``gather(return_exceptions=True)`` erases
    # exception types when a bulk run flattens per-file failures; this field
    # keeps "retry this" tellable apart from "this file is broken". Kept
    # defaulted so positional construction stays stable.
    retryable_errors: tuple[str, ...] = ()
    # Distinct, stable-sorted subset of ``resolved_namespaces`` that is known
    # to have been applied by a successful namespace-bearing upsert in this
    # run. Values present only in ``resolved_namespaces`` are preview-only
    # fallbacks from unchanged, skipped, blocked, or failed bulk files. This is
    # value-level provenance: when one file applies a namespace and another
    # only previews the same value, the namespace appears here once. ``None``
    # remains the valid untagged carve-out. Appended last for positional
    # construction compatibility.
    applied_namespaces: tuple[str | None, ...] = ()
    # #2061 namespace advisory. ``namespaces_preserved_against_rules`` counts
    # files that kept their stored namespace while the current path rules
    # would have assigned a different one — the signal that a rule edit has
    # not taken effect, and the prompt for ``mm index --reassign-namespaces``.
    # ``namespaces_reassigned`` counts files a reassignment run actually moved
    # (committed writes only), and ``namespace_moves`` records those moves as
    # ``{"from": str, "to": str, "files": int}`` entries, sorted — structured
    # rather than rendered so a consumer can tell a move *between* system
    # namespaces from one that makes chunks newly visible, and can sum files
    # instead of counting summary lines. Appended last for positional
    # construction compatibility.
    namespaces_preserved_against_rules: int = 0
    namespaces_reassigned: int = 0
    namespace_moves: tuple[dict[str, object], ...] = ()
    # Chunks this run skipped as unchanged that have no dense vector — the
    # state ``mm embedding-reset --mode apply-current`` leaves behind, where a
    # plain re-index matches every surviving content hash, reports success, and
    # restores nothing (#2115). Zero when the configured embedder produces no
    # vectors at all (``provider="none"``), which is an opt-in, not a gap.
    # Appended last for positional construction compatibility.
    chunks_missing_vectors: int = 0
    # #2076 / ADR-0006 Axis E.5: files admitted by their own frontmatter
    # ``redaction: documents-patterns`` declaration. Counted after the chunk
    # transaction commits, so a file whose embedding or storage write failed
    # is excluded — but a file whose chunks were all *unchanged* is counted:
    # it was adjudicated and admitted under the declaration this run, and a
    # standing bypass that went unreported on steady-state re-indexes would be
    # invisible exactly when someone should notice it is still in force.
    # ``exempted_paths`` are the absolute paths so surfaces can name them the
    # way ``blocked_paths`` does. Refused declarations are NOT counted here —
    # they land in ``blocked_files`` like any other refusal.
    # Appended last for positional construction compatibility.
    exempted_files: int = 0
    exempted_paths: tuple[str, ...] = ()

    # #2141: True iff this run committed at least one durable, search-visible
    # chunk write (delete / upsert / line-range refresh / metadata-only
    # refresh). The counters cannot answer this — metadata-only rows are
    # reported as ``skipped_chunks`` (#2124/#2140) and the line-range refresh
    # is reported nowhere — so long-lived callers gate their
    # ``SearchPipeline.invalidate_cache()`` on this flag instead.
    # Appended last for positional construction compatibility.
    mutated: bool = False
