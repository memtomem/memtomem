"""CLI: ``mm memory doctor`` — hygiene report (+ narrow ``--fix``) for memory stores.

Tier 1 of the ``mm memory doctor`` plan: surface 3-way drift between what's
on disk, what the agent-managed index file (e.g. Claude Code's ``MEMORY.md``)
points at, and what's actually indexed in the searchable DB. The default
command is report-only — no writes to disk, the DB, or config.

Tier 2 adds an opt-in ``--fix`` (ADR-0020): a *subtractive-only* curation that
deletes index-file pointer lines the doctor classifies as ``broken_link`` with
link-class ``missing_target`` (a ``- [title](target)`` whose target resolves
inside the memory root but points at a file that no longer exists). It never
adds, reorders, reformats, or budget-trims, and never touches the DB —
removing a provably-dead pointer is the one curation move that cannot conflict
with the agent's own intent. ``--fix`` is dry-run by default; ``--apply``
writes. See :func:`_run_fix` for the write contract.

Why this exists: a ``memory_dir`` can be *registered* yet barely indexed (the
fs watcher only reacts to live events, so files that landed while the server
was down stay invisible until a forced re-walk), and the index/TOC file can
drift from the files on disk. ``mem_search`` silently can't find the
un-indexed files; this command makes that visible.

Checks (per configured ``memory_dir``):

* **db_coverage** — files on disk the engine would index but that have zero
  chunks in the DB ("indexed N/M"). The headline signal.
* **stale_source** — DB chunks whose ``source_file`` is gone from disk (the
  file was deleted but its chunks linger; there is no single-file delete CLI).
* **convention_violation** — an index/meta file (``MEMORY.md`` / ``README.md``
  for a ``claude-memory`` dir) indexed as searchable content despite the
  provider convention.
* **broken_link** — links in the index file that don't resolve:
  ``missing_target`` (file gone) or ``outside_root`` (escapes the memory
  root). ``url`` and ``anchor`` links are classified and *not* reported.
* **dangling_wikilink** — a ``[[name]]`` on an index line naming no
  ``name.md`` inside the memory root, or one outside it (#1762). Each item
  names its class (``missing_target`` / ``outside_root``). Informational,
  never an error: the doctor cannot tell a forward reference (allowed by the
  agent memory convention) from a stale link to a deleted memo. Wikilinks are
  never pointer entries, so they don't feed ``broken_link``, ``index_orphan``
  or ``--fix``.
* **index_orphan** — files on disk that the index file (``MEMORY.md``) does
  not list. Distinct from ``db_coverage``: "not in the TOC" ≠ "not indexed".
* **ambiguous_index_line** — a pointer line naming something we won't resolve
  on a guess: a link target that isn't a plain relative path (it carries a
  query, a percent-escape, a scheme or a space), link syntax that resolved to
  no link at all, or a wholly-bracketed link label the raw source cannot
  attribute (a same-named genuine wikilink and escaped pointer sharing a
  line, #1774). Left unclassified rather than guessed at, so the line is
  reported for a human instead of being link-checked, counted as listed, or
  offered to ``--fix``.
* **budget** — the index file exceeds its byte / line / per-line-char budget
  (the hot cache loaded into the agent's context each session).
* **cold_candidate** — indexed files never accessed since indexing
  (``access_count`` sum 0 and ``last_accessed_at`` NULL). Informational.

Output: human glyphs by default, ``--json`` for a structured payload. Exit
``1`` when any *error*-severity finding exists (``stale_source``,
``convention_violation``, ``broken_link``), else ``0``. Coverage gaps,
orphans, dangling wikilinks, budget and cold candidates are advisory (a
partially-indexed dir is
a legitimate steady state) so they warn without failing the exit code —
mirrors ``mm sync-doctor`` (warns don't fail) while exposing a JSON + exit
code for CI like ``mm context settings-doctor``.

Read-only contract: config is read via ``Mem2MemConfig`` +
``load_config_d(quiet=True)`` + ``load_config_overrides(migrate=False)`` so
the diagnostic never triggers the legacy ``auto_discover`` config rewrite
(see PR #838 / #873). The DB is opened through a bare ``sqlite3`` connection
in URI ``mode=ro`` — never the full ``SqliteBackend``, which on
``initialize()`` would create the file/parent dir, run schema migration, and
checkpoint the WAL on close. ``mode=ro`` still surfaces committed rows in a
live writer's WAL (unlike ``immutable=1``); a missing or too-old DB degrades
to disk/index-only checks instead of being created. Discovery reuses the
engine's own ``discover_indexable_files`` (via a storage-less, model-less
engine) so the "should be indexed" set can't drift from what the real indexer
does.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click
from markdown_it import MarkdownIt
from markdown_it.token import Token

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig

# ── Index-file budget ───────────────────────────────────────────────
# Hot-cache ceiling for an agent-managed index file (``MEMORY.md``). The
# TOC is loaded into the agent's context every session, so it has a budget
# the curation process targets. These live here, not in
# ``ProviderIndexConvention``, because Tier 1 is the only consumer; the
# write-contract ADR promotes them to config when ``--fix`` needs to enforce
# them. Per-line cap is measured in characters (not bytes) so CJK prose isn't
# double-counted.
_INDEX_MAX_BYTES = 24_400
_INDEX_MAX_LINES = 200
_INDEX_MAX_LINE_CHARS = 200

Severity = Literal["error", "warn", "info"]

_GLYPH: dict[Severity, str] = {"error": "✗", "warn": "!", "info": "·"}
_COLOR: dict[Severity, str | None] = {"error": "red", "warn": "yellow", "info": None}

# The exit code flips to 1 when any finding carries ``severity="error"`` (see
# ``memory_doctor``). Today that's ``stale_source`` / ``convention_violation``
# / ``broken_link``; severity is assigned per-finding so the exit logic needs
# no separate check allowlist to stay in sync.

# Max sample items echoed per finding in human output (JSON carries all).
_SAMPLE_LIMIT = 8


# ── Pure: index-file parsing ────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """One ``[title](target)`` link occurrence on a pointer line.

    The harness contract is one entry per line, but an index that packs several
    onto a line is parsed faithfully rather than silently truncated: every link
    on a bullet line becomes an entry, so entries may share a ``line_no`` and
    ``raw`` (which is always the whole line, never the link's slice of it).

    ``unreadable_reason`` marks a link this command won't resolve on a guess,
    and names which doubt it is: an ``unreadable target`` (URI machinery that
    may name a file other than its literal text does, see
    :func:`_target_is_unreadable`) or a ``contested wikilink label`` (a
    wholly-bracketed label the raw source cannot attribute, see
    :func:`_settle_wikilink_labels`). The reason is the exact bracket tag the
    ``ambiguous_index_line`` item prints, so the report says what it knows
    instead of calling a perfectly readable path "unreadable". It is per
    *entry*, not per line: one odd link says nothing about the pointer next to
    it, and suppressing a sibling's broken-link verdict on its neighbour's
    account would put back the blind spot this all started with.
    """

    line_no: int  # 1-based
    title: str
    target: str
    raw: str
    unreadable_reason: str | None = None

    @property
    def unreadable(self) -> bool:
        """Whether this entry is left for a human — see ``unreadable_reason``."""
        return self.unreadable_reason is not None


@dataclass(frozen=True)
class ParsedIndex:
    """Parsed index file: pointer entries plus every other (preserved) line.

    ``other_lines`` keeps prose / comments / blanks verbatim with their line
    numbers so a future write phase can round-trip the file; Tier 1 only reads
    them for the budget measurement.

    ``unresolved_syntax_lines`` holds the 1-based numbers of lines that meant a
    pointer the parser could not resolve to a link (an unclosed ``[B](b.md``).
    It is kept apart from the entries because that is the whole problem with
    such a line: it has none. Two different doubts — "this target may not be the
    path it reads as" (an entry, flagged ``unreadable``) and "this text meant a
    link and isn't one" (no entry at all) — must not share a set, or reporting
    one has to guess at the other.

    ``multiline_lines`` holds the first line of any list item that runs past it
    (a lazy continuation). Their links are read and checked like any other —
    only ``--fix`` cares, because deleting the item's first line would strand
    the rest of it as loose prose.

    ``wikilinks`` holds ``(line_no, target)`` for every ``[[target]]`` /
    ``[[target|alias]]`` on a list-item line. Deliberately *not* entries: a
    wikilink is a cross-reference, not a TOC pointer — it never feeds
    ``broken_link``, the listed set, or ``--fix`` eligibility (#1761 pinned
    that). It only feeds the info-severity ``dangling_wikilink`` check (#1762).
    """

    entries: tuple[IndexEntry, ...]
    other_lines: tuple[tuple[int, str], ...]
    unresolved_syntax_lines: frozenset[int] = frozenset()
    multiline_lines: frozenset[int] = frozenset()
    wikilinks: tuple[tuple[int, str], ...] = ()

    @property
    def ambiguous_lines(self) -> frozenset[int]:
        """Lines ``--fix`` must not splice: something on them could not be read.

        Line-level because deletion is — the doubt is about the line as a whole.
        Reporting stays per entry (:attr:`IndexEntry.unreadable`) and per
        unresolved line, so a readable pointer is still checked where it shares
        a line with an unreadable one.
        """
        return self.unresolved_syntax_lines | {e.line_no for e in self.entries if e.unreadable}


# Link syntax surviving as literal text: the line meant a pointer the parser
# could not resolve (an unclosed ``- [B](b.md``). Only ever applied to text the
# parser handed back as text — a code span holding ``[x](y)`` is quoting, not
# pointing, and the parser tells the two apart for us.
#
# The lookbehind spares wikilinks. ``[[memo]](note)`` closes with ``]](``, and
# when the note holds a space CommonMark won't read it as a link at all — so the
# whole thing stays text, and without the lookbehind its ``](`` reads as a
# pointer someone failed to close. Same wikilink-vs-CommonMark collision as
# :data:`_WIKILINK_LABEL_RE`, reached down the text path instead of the link
# path. A genuinely unclosed link has something other than ``]`` before its
# ``](``.
_UNRESOLVED_LINK_SYNTAX_RE = re.compile(r"(?<!\])\]\(")

# What a path-resolved destination must look like for the doctor to act on it:
# a plain relative path. CommonMark hands back the destination the file
# declares, so this is not about *reading* the link — it is about what we are
# willing to treat as a filename. A destination carrying URI machinery (``?``
# query, ``%`` escape, ``:`` scheme) may name a different file than its literal
# text does, and the index convention is plain relative filenames anyway, so
# anything else is left for a human rather than resolved on a guess. ``url`` and
# ``anchor`` targets never reach this test — they are not path-resolved.
_PLAIN_RELATIVE_TARGET_RE = re.compile(r"^[^\s?%:]+$")

# A link label that is itself a whole ``[[wikilink]]``. CommonMark has no
# wikilinks, so it reads ``[[note]](미커밋)`` as an ordinary link — label
# ``[note]``, destination ``미커밋`` — and the destination is prose, not a path.
# memtomem *does* have wikilinks (``chunking/markdown.py:_WIKILINK_RE``, and the
# agent memory convention writes ``[[other-memo]]``), so reading that shape as a
# pointer isn't a judgement call about ambiguous markdown; it is this command
# failing to know its own system's link syntax. The label is the tell: a title
# CommonMark reports as ``[note]`` can only have come from ``[[note]]`` in the
# source. Bracketed *parts* of a title (``[draft] Title``, ``Title [note]``)
# don't match, and are pointers as before. The label is read from the *rendered*
# title, so it is only ever a hint: ``[\[memo\]](gone.md)`` and
# ``[&#91;memo&#93;](gone.md)`` render the same ``[memo]`` from source that never
# wrote a wikilink, and demoting those would lose an ordinary pointer. Every
# match is therefore settled against the raw source — per label name, not per
# inline (:func:`_settle_wikilink_labels`) — before it is treated as a wikilink.
_WIKILINK_LABEL_RE = re.compile(r"^\[[^\[\]]*\]$")

# A ``[[target]]`` / ``[[target|alias]]`` wikilink in literal text. Mirrors
# ``chunking/markdown.py:_WIKILINK_RE`` (kept as this command's own copy, like
# the other index-reading patterns here, rather than importing chunking
# internals). Wikilinks are never pointer *entries* — ``--fix`` must not act on
# them — but they do name memory files, so the doctor collects them for the
# info-severity ``dangling_wikilink`` check (#1762). Group 1 is the target; an
# alias never names the file.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _wikilink_in(raw: str, inner: str) -> bool:
    """Whether *raw* (an inline's **source**) literally writes ``[[inner]]``.

    The text route to a wikilink reads text markdown-it has already decoded,
    and decoding is exactly where a wikilink can be conjured from source that
    wrote none: ``\\[\\[future]]`` and ``&#91;[future]]`` both *render*
    ``[[future]]``. Escaping brackets is how an author says "not a wikilink";
    honouring that keeps escaped prose out of ``dangling_wikilink``.

    The check only ever *subtracts*: the token walk has already established the
    match came from literal text rather than a code span, so this can never
    re-admit quoted syntax.

    Membership is inline-wide, so two residual shapes stay imprecise, both
    accepted: escaped prose beside a code span quoting *the same name* raises
    an advisory ``dangling_wikilink``, and a target carrying an entity
    (``[[memo&amp;x]]``) is decoded in the text but not in *raw*, so it is not
    collected. Both are contrived, neither is an error, and closing them means
    hand-rolling CommonMark's code-span and escape rules over the raw source —
    the parsing-by-pattern this module exists to avoid. The label route, where
    a wrong call costs a pointer its link-check and ``--fix`` its safety
    proof, does not use this membership test at all — it gets the stricter
    per-name census in :func:`_settle_wikilink_labels`.
    """
    return f"[[{inner}]]" in raw


# The inline child token types the wikilink census knows how to account for.
# A *whitelist*, deliberately: the enumerate-the-bad-shapes direction was tried
# over successive review rounds and kept missing the next shape (an ``image``
# carries the needle in its own syntax, ``html_inline`` in an attribute, a
# reference link splits it across its neighbours). Anything not listed here —
# including whatever a future markdown-it adds — makes the inline opaque to the
# census, which fails *closed* (contested → warn), never open (demote).
_CENSUS_SAFE_TYPES = frozenset(
    {
        "text",
        "code_inline",
        "html_inline",
        "link_open",
        "link_close",
        "softbreak",
        "hardbreak",
        "em_open",
        "em_close",
        "strong_open",
        "strong_close",
        "s_open",
        "s_close",
    }
)


def _settle_wikilink_labels(
    raw: str,
    events: list[tuple[str, str, str]],
    claim_sources: list[str],
    opaque: bool,
) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """Decide, per label name, which wikilink-shaped links are wikilinks.

    A completed link whose title is wholly bracketed (``[memo]``) *may* have
    been a ``[[memo]](note)`` wikilink — or an escaped ``[\\[memo\\]](file.md)``
    pointer that decodes to the same title. Demoting the wrong one costs a live
    pointer its link-check and, worse, its voice in ``--fix``'s all-links-dead
    test: the line it sits on can then be deleted with the live pointer still
    on it (#1774). So a demotion is never granted on the label alone; it must
    be paid for by the raw source, occurrence for occurrence.

    Per label name, three verdicts (fail closed on ambiguity):

    * **No raw ``[[name]](`` at all** → every same-named label was escaped or
      encoded; all stay pointers.
    * **Every raw occurrence is attributable** — the inline is not opaque, and
      the raw occurrences minus those a *claim source* accounts for still
      cover every same-named label → all are wikilinks; demote all.
    * **Anything else** → the parse cannot say which occurrence is which.
      Every same-named label-shaped link stays an entry, flagged
      ``contested`` — surfaced as ``ambiguous_index_line`` (warn), neither
      link-checked nor counted as listed, and its line ineligible for
      ``--fix``. A contested *label occurrence* collects no wikilink either —
      what the census declined to attribute must not raise
      ``dangling_wikilink`` on a guess. (A text-route ``[[name]]`` the walk
      confirmed independently is not a guess, and passes through even beside
      a contested label.)

    *Claim sources* are the places a raw needle can sit *whole* without being
    a wikilink: a code span's content, an ``html_inline``'s verbatim text, a
    link destination or title attribute (each held between raw delimiters —
    backticks, ``<…>``, the link's own ``](…)`` — that a needle cannot span),
    and out-of-link text itself (a spaced ``[[memo]](PR#42 merged)`` that
    stayed prose carries its needle inside one token: no other token's raw
    starts with ``(``, so a text-resident ``[[name]]`` can only complete its
    needle in that same text). Decoding can make a claim with no raw
    counterpart; that only over-subtracts, which lands in contested — the
    error every path here is allowed to make.

    *opaque* is the census's account of everything else (built during the
    token walk): a child token type outside :data:`_CENSUS_SAFE_TYPES`, or
    bracket characters nothing accounts for — left in out-of-link text after
    every whole ``[[…]]`` is scrubbed, or anywhere in the label of a link
    that is *not* wholly bracketed. A needle split across token boundaries (a
    reference link consuming its middle) necessarily strands bracket
    *fragments* — ``[other][``, ``](x)`` — that no scrub matches, so
    unaccounted brackets mean unattributable needles. Labels get the stricter
    unscrubbed test because a label's own ``](`` can complete a needle a
    scrub would hide (``[[[a]]](x)``); the one bracket use the census itself
    accounts for — a label-shaped link's own decoded ``[inner]`` — is exempt.
    The cost of this rigor is warn-only noise on contrived lines (a genuine
    wikilink beside an image, or beside bracketed prose, goes contested); the
    alternative was hand-rolling CommonMark's escape rules over the raw
    source, which this module exists to avoid.
    """
    label_counts = Counter(
        title[1:-1]
        for kind, title, _ in events
        if kind == "link" and _WIKILINK_LABEL_RE.match(title)
    )
    verdicts: dict[str, str] = {}
    for inner, label_count in label_counts.items():
        needle = f"[[{inner}]]("
        raw_count = raw.count(needle)
        claimed = sum(source.count(needle) for source in claim_sources)
        if raw_count == 0:
            verdicts[inner] = "pointer"
        elif not opaque and raw_count - claimed >= label_count:
            verdicts[inner] = "wikilink"
        else:
            verdicts[inner] = "contested"

    links: list[tuple[str, str, bool]] = []
    wikilinks: list[str] = []
    for kind, title, dest in events:
        if kind == "wiki":
            wikilinks.append(title)  # the name rides in the title slot
            continue
        inner = title[1:-1] if _WIKILINK_LABEL_RE.match(title) else None
        if inner is not None and verdicts[inner] == "wikilink":
            # CommonMark reports the label as ``[memo]``; settled against the
            # source, it can only have come from ``[[memo]]`` — recover the
            # target (dropping an ``|alias`` part, which never names the file).
            wikilinks.append(inner.split("|", 1)[0])
        else:
            links.append((title, dest, inner is not None and verdicts[inner] == "contested"))
    return links, wikilinks


def _markdown_parser() -> MarkdownIt:
    """A CommonMark parser that reports destinations as the file declares them.

    ``normalizeLink`` percent-encodes for the web (``한글노트.md`` becomes
    ``%ED%95%9C…``), which is the wrong shape for a filesystem lookup; the
    escapes and character references we *do* need resolved are handled during
    inline parsing, before normalization. ``validateLink`` is opened up so an
    unusual scheme surfaces as a destination we can judge, rather than being
    silently dropped into a non-link.
    """
    md = MarkdownIt("commonmark")
    md.normalizeLink = lambda url: url
    md.validateLink = lambda url: True
    return md


def _read_inline(token: Token) -> tuple[list[tuple[str, str, bool]], list[str], bool]:
    """An inline token's ``(title, destination, contested)`` links, its
    wikilink targets, plus whether it also holds link syntax that resolved to
    no link at all.

    Reading an index means reading Markdown as Markdown. A destination can be
    escaped (``notes_\\(v2.md``), entity-encoded (``notes_&amp;v2.md``) or
    angle-bracketed (``<x y.md>``); a label can nest brackets; a link's
    destination can be defined on a different line entirely. In every case the
    literal text differs from the link the file declares, and ``--fix`` deletes
    lines on the strength of that read. A pattern would have to enumerate those
    differences — tried over three review rounds, it kept missing the next one —
    so the parser resolves them instead.

    The third return value catches what the first cannot say: an unclosed
    ``[B](b.md`` yields no link, and without it the line would read as ordinary
    prose. Only literal **text** counts toward it — a code span quoting link
    syntax is quoting, not pointing, and the parser hands those back as
    ``code_inline``.

    Nested links can't occur in CommonMark (a link inside a link label is
    demoted to text), so each ``link_open`` starts a new entry.

    A ``[[wikilink]](note)`` is not read as a pointer — see
    :data:`_WIKILINK_LABEL_RE`. CommonMark has no wikilinks; memtomem does.
    Wikilinks are instead returned as their own list, in source order: both
    the bare/text forms (``[[memo]]``, ``[[memo|alias]]``) and the label
    recovered from that dropped-link shape. Only literal text is scanned — a
    code span quoting ``[[x]]`` is quoting, not linking — and every match is
    checked against the inline's raw source, because the text the parser hands
    back is decoded and a decoded ``[[x]]`` may have been escaped in the file:
    text-route matches by membership (:func:`_wikilink_in`), label-shaped
    links by the per-name census (:func:`_settle_wikilink_labels`), whose
    contested verdict this function passes through per link. The walk here
    only gathers what the census needs — completed links and text-route hits
    in source order, the claim sources, and the two opacity signals (a child
    type outside :data:`_CENSUS_SAFE_TYPES`; brackets left in decoded text
    outside a label-shaped label).
    """
    raw = token.content
    events: list[tuple[str, str, str]] = []
    claim_sources: list[str] = []
    opaque = False
    unresolved = False
    title_parts: list[str] = []
    href = ""
    in_link = False
    for child in token.children or []:
        if child.type not in _CENSUS_SAFE_TYPES:
            opaque = True  # a shape the census can't account for (e.g. ``image``)
        if child.type == "link_open":
            in_link = True
            title_parts = []
            href = str(child.attrGet("href") or "")
            # Attributes never surface as child content, but their raw spans
            # sit between the link's own delimiters — claimable, not spannable.
            claim_sources.extend((href, str(child.attrGet("title") or "")))
        elif child.type == "link_close" and in_link:
            title = "".join(title_parts).strip()
            # markdown-it always pairs link_open/link_close, so recording the
            # completed link at close (rather than a placeholder at open) is
            # unobservable — and leaves nothing half-built to misread.
            events.append(("link", title, href))
            if not _WIKILINK_LABEL_RE.match(title) and ("[" in title or "]" in title):
                opaque = True  # brackets in a label the census doesn't account for
            in_link = False
        elif child.type in ("code_inline", "html_inline"):
            claim_sources.append(child.content)
            if in_link:
                title_parts.append(child.content)
        elif in_link:
            title_parts.append(child.content)
        elif child.type == "text":
            # A needle *resident* in the text is claimable as-is; only the
            # brackets no whole ``[[…]]`` accounts for — the strands a needle
            # split across token boundaries always leaves — force opacity.
            claim_sources.append(child.content)
            scrubbed = _WIKILINK_RE.sub("", child.content)
            if "[" in scrubbed or "]" in scrubbed:
                opaque = True  # stranded brackets: split or escaped syntax
            if _UNRESOLVED_LINK_SYNTAX_RE.search(child.content):
                unresolved = True
            events.extend(
                ("wiki", m.group(1), "")
                for m in _WIKILINK_RE.finditer(child.content)
                if _wikilink_in(raw, m.group(0)[2:-2])
            )
    links, wikilinks = _settle_wikilink_labels(raw, events, claim_sources, opaque)
    return links, wikilinks, unresolved


def _list_item_body(tokens: list[Token], open_index: int) -> list[Token]:
    """The tokens between ``tokens[open_index]`` (a ``list_item_open``) and its
    matching close, nested items included."""
    depth = 0
    for j in range(open_index, len(tokens)):
        if tokens[j].type == "list_item_open":
            depth += 1
        elif tokens[j].type == "list_item_close":
            depth -= 1
            if depth == 0:
                return tokens[open_index + 1 : j]
    return tokens[open_index + 1 :]  # unclosed item: treat the rest as its body


def _own_inlines(body: list[Token]) -> list[Token]:
    """The item's *own* inlines — never one belonging to a nested item.

    ``_list_item_body`` slices the whole item, children included, because the
    structural check needs to see them. Reading pointers out of that slice
    naively lets a parent with no text of its own (``-`` on a line by itself, or
    one opening with a fence) adopt its child's inline: the child's pointer gets
    recorded twice, once against each item, which double-reports the link and
    hangs the parent's unfixable-item verdict on the child's line — skipping a
    fix that is safe.

    Every own inline is read, not just the first: an item can put its pointer in
    a second paragraph, and skipping those would drop a real pointer from the
    report. Each entry keeps its own inline's line number; the item is not
    one-line, so ``--fix`` stands down for all of them.
    """
    inlines: list[Token] = []
    depth = 0
    for token in body:
        if token.type == "list_item_open":
            depth += 1
        elif token.type == "list_item_close":
            depth -= 1
        elif depth == 0 and token.type == "inline" and token.map is not None:
            inlines.append(token)
    return inlines


def _item_is_one_line(body: list[Token], inline: Token) -> bool:
    """Whether a list item is exactly the one line its pointer sits on.

    ``--fix`` splices whole lines, so an item that is more than its first line
    can't be deleted by deleting that line — the remainder would be reparented
    as top-level markdown. Two ways an item outgrows its line, and the item's
    source map catches neither cleanly (a *loose* list's item map swallows the
    blank line after it, so measuring the map would flag every entry before a
    paragraph break):

    * **Its paragraph wraps** — a lazy continuation, which shows up as the
      inline's own map spanning more than one line.
    * **It holds more than that paragraph** — a second paragraph, a child fence,
      a nested list. Structure says so: a one-line item's body is exactly
      ``paragraph_open, inline, paragraph_close``.
    """
    if inline.map is not None and inline.map[1] - inline.map[0] > 1:
        return False
    return len(body) == 3 and body[0].type == "paragraph_open"


def _target_is_unreadable(target: str) -> bool:
    """Whether *target* will be path-resolved but may not be the path it reads as."""
    t = target.strip()
    if not t or t.startswith("#") or _is_url(t):
        return False  # anchor / url — never resolved against the filesystem
    return not _PLAIN_RELATIVE_TARGET_RE.match(t)


def parse_memory_index(text: str) -> ParsedIndex:
    """Parse an index file into pointer entries + preserved other lines.

    The whole document is parsed once, and only the inline content of genuine
    **list items** is read for pointers. Block context is the reason: a bullet
    inside a fenced or indented code block is an *example* of an index line, not
    an index line, and a reader that works line by line cannot tell the
    difference — it would offer the example's dead target to ``--fix``, which
    would then edit the code block. The token stream answers structurally what
    no amount of squinting at a line can.

    Line numbers come from each item's source map, so an entry still carries the
    line ``--fix`` splices by (ADR-0020 §2), and reference definitions resolve
    because the document is read as one.
    """
    lines = text.splitlines()
    entries: list[IndexEntry] = []
    unresolved_syntax: set[int] = set()
    multiline: set[int] = set()
    pointer_lines: set[int] = set()
    wikilinks: list[tuple[int, str]] = []

    tokens = _markdown_parser().parse(text, {})
    for i, token in enumerate(tokens):
        if token.type != "list_item_open":
            continue
        body = _list_item_body(tokens, i)
        for inline in _own_inlines(body):
            line_no = inline.map[0] + 1
            links, inline_wikilinks, unresolved = _read_inline(inline)
            wikilinks.extend((line_no, target) for target in inline_wikilinks)
            # Link syntax that resolved to no link (``- [B](b.md``) would
            # otherwise pass as ordinary prose — an unread pointer reported as
            # nothing at all. It has no entry to carry it, so the line is
            # recorded, whether or not a *readable* pointer shares it.
            if unresolved:
                unresolved_syntax.add(line_no)
            if not links:
                continue
            pointer_lines.add(line_no)
            if not _item_is_one_line(body, inline):
                multiline.add(line_no)
            entries.extend(
                IndexEntry(
                    line_no=line_no,
                    title=title,
                    target=target.strip(),
                    raw=lines[line_no - 1],
                    # Target-shape doubt wins the wording when both hold: a
                    # target we can't resolve is the stronger claim, and
                    # either way the entry is left for a human.
                    unreadable_reason=(
                        "unreadable target"
                        if _target_is_unreadable(target)
                        else "contested wikilink label"
                        if contested
                        else None
                    ),
                )
                for title, target, contested in links
            )

    other = [(i, line) for i, line in enumerate(lines, start=1) if i not in pointer_lines]
    return ParsedIndex(
        entries=tuple(entries),
        other_lines=tuple(other),
        unresolved_syntax_lines=frozenset(unresolved_syntax),
        multiline_lines=frozenset(multiline),
        wikilinks=tuple(wikilinks),
    )


# ── Pure: link classification ───────────────────────────────────────

LinkClass = Literal["ok", "missing_target", "outside_root", "url", "anchor"]

# A URL needs an explicit ``scheme://`` (so a Windows drive ``C:\…`` isn't
# misread as a scheme) plus the ``mailto:`` special case.
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _is_url(target: str) -> bool:
    return bool(_URL_RE.match(target)) or target.startswith("mailto:")


def classify_link(target: str, *, root: Path, source_dir: Path) -> LinkClass:
    """Classify an index/markdown link target.

    ``root`` is the memory dir (links may not escape it); ``source_dir`` is
    the directory of the file the link lives in (for resolving relatives).
    Resolution is path-only — ``Path.resolve()`` is strict=False so a missing
    target still resolves and is then existence-checked, separating
    ``missing_target`` from ``outside_root``.
    """
    t = target.strip()
    if not t or t.startswith("#"):
        return "anchor"  # in-page anchor or empty — not a file reference
    if _is_url(t):
        return "url"
    path_part = t.split("#", 1)[0]  # strip ``file.md#section`` anchor suffix
    if not path_part:
        return "anchor"
    raw = Path(path_part).expanduser()
    base = raw if raw.is_absolute() else (source_dir / raw)
    try:
        resolved = base.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return "missing_target"
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return "outside_root"
    return "ok" if resolved.exists() else "missing_target"


# ── Pure: budget measurement ────────────────────────────────────────


@dataclass(frozen=True)
class BudgetMeasure:
    byte_len: int
    line_count: int
    overlong_lines: tuple[int, ...]  # 1-based line numbers exceeding the char cap

    @property
    def over_budget(self) -> bool:
        return (
            self.byte_len > _INDEX_MAX_BYTES
            or self.line_count > _INDEX_MAX_LINES
            or bool(self.overlong_lines)
        )


def measure_budget(text: str) -> BudgetMeasure:
    """Measure an index file against the hot-cache budget.

    Bytes are UTF-8 encoded length; lines are counted from ``splitlines``;
    per-line length is character count (``len``) so a CJK line isn't penalised
    for its multi-byte encoding.
    """
    lines = text.splitlines()
    overlong = tuple(
        i for i, line in enumerate(lines, start=1) if len(line) > _INDEX_MAX_LINE_CHARS
    )
    return BudgetMeasure(
        byte_len=len(text.encode("utf-8")),
        line_count=len(lines),
        overlong_lines=overlong,
    )


# ── Finding + report model ──────────────────────────────────────────


@dataclass
class Finding:
    check: str
    severity: Severity
    summary: str
    items: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def to_json(self) -> dict[str, object]:
        return {
            "check": self.check,
            "severity": self.severity,
            "count": self.count,
            "summary": self.summary,
            "items": self.items,
        }


@dataclass
class DirReport:
    path: str
    category: str
    index_file: str | None
    exists: bool
    disk_indexable: int
    db_covered: int
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "category": self.category,
            "index_file": self.index_file,
            "exists": self.exists,
            "disk_indexable": self.disk_indexable,
            "db_covered": self.db_covered,
            "findings": [f.to_json() for f in self.findings],
        }


def _read_error_message(exc: Exception) -> str:
    """Render a read failure for a report — never empty.

    ``str(OSError())`` is ``""``, so interpolating the exception directly can
    leave a message that trails off (``"could not read MEMORY.md: "``) or, in
    Tier 2, one that vanishes under a truthiness check and turns a real failure
    back into "clean" (#1769). Both read paths route through here: the message
    falls back to the exception class name, and Tier 2's presence check stays
    ``error is not None`` regardless.
    """
    return str(exc) or type(exc).__name__


# ── Read-only config + engine plumbing ──────────────────────────────


def _load_config_read_only() -> Mem2MemConfig:
    """Load config without triggering the legacy auto-discover migration.

    Mirrors ``mm sync-doctor``: a read-only diagnostic must not rewrite
    ``config.json`` as a side effect (``migrate=False``).
    """
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    config = Mem2MemConfig()
    load_config_d(config, quiet=True)
    load_config_overrides(config, migrate=False)
    return config


def _build_discovery_engine(config: Mem2MemConfig) -> object:
    """Build an ``IndexEngine`` purely for its file-discovery method.

    ``discover_indexable_files`` reads only ``config`` (supported extensions,
    index roots, exclude rules) — it never touches storage or the embedder —
    so both are passed as inert stand-ins (``None`` storage, a model-less
    ``NoopEmbedder``). Reusing the engine's own discovery is deliberate: the
    doctor's "should be indexed" set is then guaranteed identical to what the
    real indexer produces, so coverage / orphan counts can't drift from
    reality.
    """
    from memtomem.embedding.noop import NoopEmbedder
    from memtomem.indexing.engine import IndexEngine

    return IndexEngine(
        storage=None,  # type: ignore[arg-type]
        embedder=NoopEmbedder(),  # type: ignore[arg-type]
        config=config.indexing,
        namespace_config=config.namespace,
    )


# Per-source aggregate the cold-candidate / coverage checks consume.
# ``MAX(last_accessed_at)`` over ISO-8601 text sorts chronologically and
# SQLite's ``MAX`` ignores NULL, so a never-accessed multi-chunk file reports
# ``(…, None, 0, …)``. ``COALESCE`` guards an all-NULL importance column.
_SOURCE_SIGNALS_SQL = (
    "SELECT source_file, COUNT(*), MAX(last_accessed_at),"
    " COALESCE(SUM(access_count), 0),"
    " COALESCE(MAX(importance_score), 0.0),"
    " COALESCE(AVG(importance_score), 0.0)"
    " FROM chunks GROUP BY source_file ORDER BY source_file"
)


def _read_source_signals(
    db_path: Path,
) -> list[tuple[Path, int, str | None, int, float, float]] | None:
    """Read per-source signal rows from an *existing* DB, strictly read-only.

    Opens the SQLite file with ``mode=ro`` (URI) so the doctor can never
    create the file, run a schema migration, or checkpoint the WAL — the
    report-only contract. Unlike ``immutable=1``, ``mode=ro`` still surfaces
    committed rows sitting in an active writer's WAL (e.g. while ``mm web`` is
    up), so the report reflects live state. Returns ``None`` — not an empty
    list — when the DB is absent, its schema predates the aggregate's columns
    (a fresh or very old install), or the file is corrupt / not a database;
    the caller then degrades to disk/index-only checks rather than crashing or
    creating the DB. ``sqlite3.DatabaseError`` is the parent of
    ``OperationalError`` (missing table/column) and also covers the
    "file is not a database" / malformed-image cases, so one except clause
    handles every "can't read this DB" path.
    """
    db = db_path.expanduser()
    if not db.is_absolute():
        db = db.absolute()
    if not db.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(_SOURCE_SIGNALS_SQL).fetchall()
    except sqlite3.DatabaseError:
        return None  # missing/old-schema/corrupt — degrade to disk-only checks
    finally:
        if conn is not None:
            conn.close()
    return [(Path(r[0]), int(r[1]), r[2], int(r[3]), float(r[4]), float(r[5])) for r in rows]


# ── Analysis ────────────────────────────────────────────────────────


def _analyze_dir(
    *,
    dir_path: Path,
    config: Mem2MemConfig,
    engine: object,
    db_rows: list[tuple[Path, int, str | None, int, float, float]],
    memory_dirs: list[Path],
) -> DirReport:
    """Run every check for one configured ``memory_dir``. Pure given inputs.

    ``memory_dirs`` is the full set of configured roots — needed to attribute
    each discovered disk file to its *most-specific* owning root, the same
    longest-prefix rule the DB rows are bucketed by. Without that, a recursive
    walk of a parent root would pull in files that belong to a nested child
    root (whose DB rows are bucketed to the child), making the parent falsely
    report the child's already-indexed files as uncovered.
    """
    from memtomem.config import (
        categorize_memory_dir,
        index_excluded_filenames,
        provider_index_file,
    )
    from memtomem.indexing.engine import resolve_owning_memory_dir
    from memtomem.storage.sqlite_helpers import norm_path

    resolved_dir = dir_path.expanduser().resolve()
    exists = resolved_dir.is_dir()
    category = categorize_memory_dir(dir_path)
    index_file_name = provider_index_file(category)
    excluded = index_excluded_filenames(category)
    dir_key = norm_path(dir_path.expanduser())

    # Disk side: files the engine would index (post-exclusion, recursive —
    # faithful to the real indexer), keeping only the files this dir is the
    # most-specific owner of (symmetry with the DB bucketing in
    # ``_gather_reports`` — a nested child root owns its own subtree).
    disk_files = engine.discover_indexable_files(resolved_dir)  # type: ignore[attr-defined]
    disk_norm: dict[str, Path] = {}
    for p in disk_files:
        owning = resolve_owning_memory_dir(p, memory_dirs)
        if owning is not None and norm_path(owning) == dir_key:
            disk_norm[norm_path(p)] = p

    # DB side: source-file signal rows already bucketed to this dir.
    db_norm: dict[str, tuple[Path, int, str | None, int, float, float]] = {
        norm_path(row[0]): row for row in db_rows
    }

    report = DirReport(
        path=str(resolved_dir),
        category=category,
        index_file=index_file_name,
        exists=exists,
        disk_indexable=len(disk_norm),
        db_covered=len(disk_norm.keys() & db_norm.keys()),
    )

    # 1. db_coverage — on disk, no chunks.
    uncovered = sorted(p for k, p in disk_norm.items() if k not in db_norm)
    if uncovered:
        report.findings.append(
            Finding(
                check="db_coverage",
                severity="warn",
                summary=(
                    f"{len(uncovered)}/{len(disk_norm)} indexable file(s) have no DB "
                    "chunks — `mem_search` can't find them (run `mm index <dir> --force`)"
                ),
                items=[p.name for p in uncovered],
            )
        )

    # 2/3. DB-only sources — split into stale (deleted), convention violation
    # (meta file indexed), and an unexpected residue.
    stale: list[str] = []
    violations: list[str] = []
    unexpected: list[str] = []
    for k, row in db_norm.items():
        if k in disk_norm:
            continue
        src = row[0]
        if not Path(src).exists():
            stale.append(str(src))
        elif Path(src).name in excluded:
            violations.append(str(src))
        else:
            unexpected.append(str(src))
    if stale:
        report.findings.append(
            Finding(
                check="stale_source",
                severity="error",
                summary=(
                    f"{len(stale)} DB source file(s) no longer exist on disk — "
                    "chunks linger after the file was deleted"
                ),
                items=sorted(stale),
            )
        )
    if violations:
        report.findings.append(
            Finding(
                check="convention_violation",
                severity="error",
                summary=(
                    f"{len(violations)} index/meta file(s) indexed as content despite the "
                    f"{category} convention (run `mm purge --matching-excluded --apply`)"
                ),
                items=sorted(violations),
            )
        )
    if unexpected:
        report.findings.append(
            Finding(
                check="db_extra",
                severity="warn",
                summary=(
                    f"{len(unexpected)} DB source file(s) exist on disk but fall outside the "
                    "current indexable set (unsupported extension or excluded path)"
                ),
                items=sorted(unexpected),
            )
        )

    # 4. cold_candidate — covered files never accessed since indexing.
    cold = [
        (row[0], row[1])  # (path, chunk_count)
        for k, row in db_norm.items()
        if k in disk_norm and row[3] == 0 and row[2] is None
    ]
    if cold:
        cold.sort(key=lambda pc: (-pc[1], str(pc[0])))
        report.findings.append(
            Finding(
                check="cold_candidate",
                severity="info",
                summary=(
                    f"{len(cold)} indexed file(s) never accessed since indexing "
                    "(access_count 0, last_accessed_at unset)"
                ),
                items=[f"{p.name} ({c} chunk{'s' if c != 1 else ''})" for p, c in cold],
            )
        )

    # 5/6/7. Index-file checks (only for providers with a TOC convention).
    if index_file_name:
        _analyze_index_file(
            report=report,
            root=resolved_dir,
            index_file_name=index_file_name,
            disk_norm=disk_norm,
            norm_path=norm_path,
        )

    return report


def _analyze_index_file(
    *,
    report: DirReport,
    root: Path,
    index_file_name: str,
    disk_norm: dict[str, Path],
    norm_path: object,
) -> None:
    """Broken-link, dangling-wikilink, index-orphan and budget checks against
    the TOC file."""
    index_path = root / index_file_name
    try:
        text = index_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report.findings.append(
            Finding(
                check="index_missing",
                severity="warn",
                summary=f"index file {index_file_name} not found in {root}",
            )
        )
        return
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError — without this arm
        # an undecodable index takes the whole command down (#1769). Bare
        # ValueError stays uncaught: parse bugs must not masquerade as an
        # unreadable file.
        report.findings.append(
            Finding(
                check="index_missing",
                severity="warn",
                summary=f"could not read {index_file_name}: {_read_error_message(exc)}",
            )
        )
        return

    parsed = parse_memory_index(text)

    # broken_link — classify every pointer target; report only the broken
    # classes. ``listed_norm`` collects the resolvable targets for the orphan
    # check below.
    #
    # An unreadable target is evidence of nothing: the text read as a path may
    # not be the path Markdown resolves. Such an entry is reported on its own and
    # feeds *neither* conclusion — not a ``broken_link`` error (which would fail
    # a run over a healthy index) and not a ``listed`` entry (which would mark a
    # file indexed on a guess). The file it may have meant can therefore also
    # surface as an ``index_orphan``; that is the honest read.
    #
    # This is per entry, not per line. Doubt about one destination says nothing
    # about the pointer beside it, and letting it suppress that sibling's verdict
    # would put back the very blind spot this parser exists to close.
    broken: list[str] = []
    ambiguous_items: list[str] = []
    ambiguous_line_nos: set[int] = set()
    listed_norm: set[str] = set()
    for entry in parsed.entries:
        if entry.unreadable:
            ambiguous_items.append(f"L{entry.line_no} [{entry.unreadable_reason}] {entry.target}")
            ambiguous_line_nos.add(entry.line_no)
            continue
        cls = classify_link(entry.target, root=root, source_dir=root)
        if cls in ("missing_target", "outside_root"):
            broken.append(f"L{entry.line_no} [{cls}] {entry.target}")
        elif cls == "ok":
            resolved = (root / entry.target.split("#", 1)[0]).resolve()
            listed_norm.add(norm_path(resolved))  # type: ignore[operator]
    # A line that meant a pointer the parser could not resolve has no entry to
    # speak for it, so it is reported from the source text. It is reported
    # whether or not a readable pointer shares the line: an unread ``[B](b.md``
    # beside a live ``[A](a.md)`` is the same pointer-hiding-behind-its-company
    # shape as above, and the line yields no --fix candidate to surface it
    # either.
    raw_lines = text.splitlines()
    for line_no in sorted(parsed.unresolved_syntax_lines):
        ambiguous_items.append(f"L{line_no} [unreadable link syntax] {raw_lines[line_no - 1]}")
        ambiguous_line_nos.add(line_no)
    ambiguous_items.sort(key=lambda item: int(item.split(None, 1)[0][1:]))
    if broken:
        report.findings.append(
            Finding(
                check="broken_link",
                severity="error",
                summary=f"{len(broken)} broken link(s) in {index_file_name}",
                items=broken,
            )
        )
    if ambiguous_items:
        report.findings.append(
            Finding(
                check="ambiguous_index_line",
                severity="warn",
                summary=(
                    f"{len(ambiguous_line_nos)} line(s) in {index_file_name} name a target this "
                    "command will not resolve on a guess (it carries a query, percent-escape, "
                    "scheme or space), hold link syntax that resolved to no link, or carry a "
                    "wikilink-shaped label the raw source cannot attribute — they are "
                    "neither link-checked nor counted as listed, and --fix will not touch them; "
                    "rewrite the target as a plain relative filename, or rename the wholly "
                    "bracketed label if the link is a pointer (un-escaping it makes it a "
                    "wikilink instead)"
                ),
                items=ambiguous_items,
            )
        )

    # dangling_wikilink — a ``[[name]]`` on an index line whose memory file
    # doesn't exist (#1762). Info-severity by decision: the doctor cannot tell
    # a forward reference (blessed by the agent memory convention — a name
    # worth writing later) from a stale link left by a deleted memo, so this
    # never gates the exit code, and wikilinks stay out of entries / the
    # listed set / ``--fix`` eligibility.
    #
    # Resolution is this command's own rule, close to the importers'
    # (``indexing/importers.py``: ``[[name]]`` → ``name.md``) but deliberately
    # more lenient where they part: an author who writes the suffix means the
    # file, so ``[[name.md]]`` resolves to ``name.md`` rather than the
    # importers' ``name.md.md``. A target outside the root is reported too —
    # it may well exist, so the item names its class rather than claiming the
    # file is missing.
    dangling: list[str] = []
    for line_no, target in parsed.wikilinks:
        name = target.strip().split("#", 1)[0]  # drop an Obsidian ``#section``
        if not name:
            continue
        candidate = name if name.endswith(".md") else name + ".md"
        cls = classify_link(candidate, root=root, source_dir=root)
        if cls in ("missing_target", "outside_root"):
            dangling.append(f"L{line_no} [{cls}] [[{target}]] → {candidate}")
    if dangling:
        report.findings.append(
            Finding(
                check="dangling_wikilink",
                severity="info",
                summary=(
                    f"{len(dangling)} wikilink(s) in {index_file_name} naming no memory file "
                    "inside the memory root (missing_target), or one outside it "
                    "(outside_root) — a forward reference (fine: it marks a memory worth "
                    "writing later), a stale link to a deleted memo, or a name to correct"
                ),
                items=dangling,
            )
        )

    # index_orphan — indexable files on disk the TOC doesn't point at. The
    # index file itself / excluded meta files are not in ``disk_norm`` (the
    # engine excludes them), so they're never flagged here.
    orphans = sorted(p for k, p in disk_norm.items() if k not in listed_norm)
    if orphans:
        report.findings.append(
            Finding(
                check="index_orphan",
                severity="warn",
                summary=(
                    f"{len(orphans)} file(s) on disk not listed in {index_file_name} "
                    "(index orphans — present and indexable but absent from the TOC)"
                ),
                items=[p.name for p in orphans],
            )
        )

    # budget — hot-cache size.
    budget = measure_budget(text)
    if budget.over_budget:
        parts = [f"{budget.byte_len} bytes (cap {_INDEX_MAX_BYTES})"]
        parts.append(f"{budget.line_count} lines (cap {_INDEX_MAX_LINES})")
        if budget.overlong_lines:
            shown = ", ".join(f"L{n}" for n in budget.overlong_lines[:_SAMPLE_LIMIT])
            parts.append(
                f"{len(budget.overlong_lines)} line(s) over {_INDEX_MAX_LINE_CHARS} chars ({shown})"
            )
        report.findings.append(
            Finding(
                check="budget",
                severity="warn",
                summary=f"{index_file_name} over budget: " + "; ".join(parts),
                items=[f"L{n}" for n in budget.overlong_lines],
            )
        )


def _gather_reports(
    *,
    config: Mem2MemConfig,
    inspect_dirs: list[Path],
) -> list[DirReport]:
    """Read DB signals read-only, bucket by owning dir, analyze each inspected dir.

    Fully synchronous: the only DB access is the read-only aggregate in
    :func:`_read_source_signals`, and discovery is a sync disk walk — no
    embedder, no async storage backend, no writes.
    """
    from collections import defaultdict

    from memtomem.indexing.engine import resolve_owning_memory_dir
    from memtomem.storage.sqlite_helpers import norm_path

    raw_signals = _read_source_signals(Path(config.storage.sqlite_path))
    db_unreadable = raw_signals is None
    signals = raw_signals or []
    memory_dirs = config.indexing.all_index_roots()

    # Bucket every DB source row to the configured dir that owns it
    # (longest-prefix), keyed by the resolved dir string so it matches the
    # per-dir loop below. Unowned rows are surfaced separately.
    by_dir: dict[str, list[tuple[Path, int, str | None, int, float, float]]] = defaultdict(list)
    unowned = 0
    for row in signals:
        owning = resolve_owning_memory_dir(row[0], memory_dirs)
        if owning is None:
            unowned += 1
        else:
            by_dir[norm_path(owning)].append(row)

    engine = _build_discovery_engine(config)

    reports: list[DirReport] = []
    for d in inspect_dirs:
        key = norm_path(d.expanduser())
        reports.append(
            _analyze_dir(
                dir_path=d,
                config=config,
                engine=engine,
                db_rows=by_dir.get(key, []),
                memory_dirs=memory_dirs,
            )
        )

    if db_unreadable:
        # Top-level note: no readable DB, so coverage/stale/cold are unknown
        # and every disk file shows as uncovered. Info — never fails the exit.
        db_report = _note_report("(database)")
        db_report.findings.append(
            Finding(
                check="db_unavailable",
                severity="info",
                summary=(
                    f"no readable memtomem DB at {Path(config.storage.sqlite_path).expanduser()} "
                    "— reporting disk/index-only checks (run `mm index` to build it)"
                ),
            )
        )
        reports.append(db_report)

    if unowned:
        # Top-level info: chunks attributed to no configured memory_dir
        # (e.g. a dir was removed from config, or content added elsewhere).
        # Never affects the exit code.
        unowned_report = _note_report("(unowned)")
        unowned_report.findings.append(
            Finding(
                check="unowned_chunks",
                severity="info",
                summary=(
                    f"{unowned} DB source file(s) under no configured memory_dir "
                    "(removed dir, or content added outside the registry)"
                ),
            )
        )
        reports.append(unowned_report)

    return reports


# ── Rendering ───────────────────────────────────────────────────────


# A "note" report is a synthetic top-level entry (not a real ``memory_dir``):
# its parenthesized path is the sentinel. Carries only top-level findings.
def _note_report(label: str) -> DirReport:
    return DirReport(
        path=label, category="", index_file=None, exists=False, disk_indexable=0, db_covered=0
    )


def _is_note(report: DirReport) -> bool:
    return report.path.startswith("(")


def _severity_totals(reports: list[DirReport]) -> dict[str, int]:
    totals = {"error": 0, "warn": 0, "info": 0}
    for r in reports:
        for f in r.findings:
            totals[f.severity] += 1
    return totals


def _emit_human(reports: list[DirReport]) -> None:
    for r in reports:
        if _is_note(r):
            for f in r.findings:
                click.secho(f"{_GLYPH[f.severity]} {f.summary}", fg=_COLOR[f.severity])
            continue
        header = f"{r.path}"
        suffix = []
        if not r.exists:
            suffix.append("missing")
        if r.index_file:
            suffix.append(f"index={r.index_file}")
        suffix.append(f"indexed {r.db_covered}/{r.disk_indexable}")
        click.secho(f"\n■ {header}", bold=True)
        click.echo(f"  {r.category} · " + " · ".join(suffix))
        if not r.findings:
            click.secho("  ✓ no issues", fg="green")
            continue
        for f in r.findings:
            click.secho(f"  {_GLYPH[f.severity]} {f.summary}", fg=_COLOR[f.severity])
            for item in f.items[:_SAMPLE_LIMIT]:
                click.echo(f"      - {item}")
            if f.count > _SAMPLE_LIMIT:
                click.echo(f"      … and {f.count - _SAMPLE_LIMIT} more")

    totals = _severity_totals(reports)
    click.echo(f"\nSummary: {totals['error']} error, {totals['warn']} warn, {totals['info']} info.")


def _emit_json(reports: list[DirReport]) -> None:
    totals = _severity_totals(reports)
    payload = {
        "status": "issues" if totals["error"] or totals["warn"] else "ok",
        "dirs": [r.to_json() for r in reports],
        "summary": totals,
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


# ── Tier 2: subtractive --fix (ADR-0020) ────────────────────────────
#
# ``--fix`` deletes index-file pointer lines whose link-class is
# ``missing_target`` and nothing else. The contract (ADR-0020):
#   §1 subtractive-only, missing_target-only (outside_root et al. excluded);
#   §2 byte-exact preservation of every surviving line via a line *splice*
#      (keepends) of the ORIGINAL text — never a reconstruction from parsed
#      fields, which would lose CRLF / trailing-newline state;
#   §4 dry-run by default, ``--apply`` to write;
#   §5 atomic write (mode-preserving) under the sidecar lock, re-validating
#      each candidate against fresh content so concurrent agent edits survive.


class _IndexUnreadable(Exception):
    """The locked fresh read inside :func:`_apply_fix` could not decode the index.

    Raised so ``_run_fix`` can report the file as a per-file error instead of
    letting the traceback corrupt the ``--json`` payload. Deliberately narrow:
    only the read raises this — lock, ``stat`` and write failures keep
    propagating, so an exception after the atomic replace can never be
    misreported as ``removed=[]`` (ADR-0020 §5, every removed line is
    auditable).
    """


@dataclass
class FixFileResult:
    """Per-index-file outcome of a ``--fix`` run (preview or applied)."""

    path: str  # the resolved memory_dir
    index_file: str  # the index filename (e.g. MEMORY.md)
    applied: bool
    removed: list[tuple[int, str]]  # (1-based line_no, raw line text)
    # Candidates --fix found but will not remove: (line_no, raw, why). ADR-0020
    # §1 — a skipped candidate is a dead pointer left behind, so it is part of
    # the report, never a silent omission.
    skipped: list[tuple[int, str, str]] = field(default_factory=list)
    # The index could not be read: nothing about this file is verified, so the
    # run must not read as clean (#1769). Presence checks compare against None,
    # never truthiness — the message itself is never empty (see
    # _read_error_message), but "" would still be a real failure.
    error: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "index_file": self.index_file,
            "removed": [{"line": n, "text": t} for n, t in self.removed],
            "skipped": [{"line": n, "text": t, "reason": w} for n, t, w in self.skipped],
            "error": self.error,
        }


def _missing_target_entries(
    text: str, *, root: Path, parsed: ParsedIndex | None = None
) -> list[IndexEntry]:
    """Pointer entries in *text* whose target classifies as ``missing_target``.

    The single link-class ``--fix`` removes (ADR-0020 §1): a pointer that
    resolves *inside* the memory root but points at a file that does not exist.
    ``outside_root`` / ``url`` / ``anchor`` / ``ok`` are all left untouched —
    only a provably-dead in-root reference is safe to delete subtractively.

    An **unreadable** target is excluded here, at the candidate stage, the same
    way Tier 1 excludes it from ``broken_link``: a destination carrying URI
    machinery may name a file other than its literal text does, so its literal
    miss is not proof of death — it is an ``ambiguous_index_line`` for a human
    to read, not a candidate. The distinction matters because a line whose only
    links are unreadable must not surface as a *skipped candidate* either: it
    is not a `--fix` concern at all (ADR-0020 §1), and reporting it as a
    pointer `--fix` declined to remove would misfile Tier 1's warning as Tier
    2 business. A line mixing a dead link with an unreadable one *is* a
    candidate, and the ambiguity is then caught per line by
    :func:`_line_skip_reason`.

    Pass *parsed* to reuse a read of the same *text* the caller already has.
    """
    entries = (parsed if parsed is not None else parse_memory_index(text)).entries
    return [
        e
        for e in entries
        if not e.unreadable
        and classify_link(e.target, root=root, source_dir=root) == "missing_target"
    ]


# ADR-0020 §1's strict grammar starts at the marker: a single-line ``-``/``*``
# bullet. An ordered (``1.``) or ``+`` item is read by the parser and reported,
# but stays out of the write path — the shape the ``MEMORY.md`` contract
# specifies is the only one whose whole-line deletion is a curation no-op.
_BULLET_MARKER_RE = re.compile(r"^\s*[-*]\s")


def _line_skip_reason(line_no: int, *, parsed: ParsedIndex, root: Path) -> str | None:
    """Why the line at *line_no* is not eligible for deletion, or ``None``.

    ADR-0020 §1's two-part test, asked of a *candidate* line (one carrying at
    least one ``missing_target`` link). Both parts fail closed:

    * **Strict grammar — the parse must account for the whole line.** A
      single-line ``-``/``*`` bullet of links plus inert prose. A line that is
      not that bullet, an item that runs past its first line (deleting it would
      strand the continuation as loose prose), link syntax that resolved to no
      link (the line meant a pointer the grammar could not read), or a link
      this command will not resolve on a guess (URI machinery may name a file
      other than its literal text does, or a wikilink-shaped label the raw
      source cannot attribute) — none is eligible.
    * **All links dead.** Every link on the line must classify
      ``missing_target``. One live, out-of-root, or otherwise not-provably-dead
      sibling spares the whole line: splicing it would delete a pointer
      memtomem cannot prove dead, and carving the dead entry out of the line is
      the span surgery ADR-0020 rejects.

    Judged per *line*, not per entry, because the line is the unit of deletion.
    Judged against *parsed*, so it can be re-asked of a fresh read under the
    lock — the index is a live file, and a line eligible at analysis time need
    not still be (§5).

    *line_no* must name a pointer line of *parsed*; every reason below is a
    statement about the links on it, so a line with none has no answer here —
    only a caller bug can ask, and it fails loudly rather than dressing a
    non-candidate up as a skipped one.
    """
    line_entries = [e for e in parsed.entries if e.line_no == line_no]
    if _BULLET_MARKER_RE.match(line_entries[0].raw) is None:
        return "is not a `-`/`*` bullet entry"
    if line_no in parsed.multiline_lines:
        return "is a list item that continues past this line"
    if line_no in parsed.unresolved_syntax_lines:
        return "also holds link syntax that resolved to no link"
    if any(e.unreadable for e in line_entries):
        return "holds a link this command will not resolve on a guess"
    if any(
        classify_link(e.target, root=root, source_dir=root) != "missing_target"
        for e in line_entries
    ):
        return "carries a link that is not provably dead"
    return None


def _partition_candidates(
    candidates: list[IndexEntry], *, parsed: ParsedIndex, root: Path
) -> tuple[list[tuple[int, str]], list[tuple[int, str, str]]]:
    """Split candidate lines into the eligible and the skipped (ADR-0020 §1).

    *candidates* are entries; the unit of the answer is the **line**, so they
    are collapsed by line number first — a line carrying two dead links is one
    candidate, judged once. Returns ``(eligible, skipped)``, each in file
    order: ``eligible`` as ``(line_no, raw)``, ``skipped`` as
    ``(line_no, raw, reason)``. Rendering belongs to the caller.
    """
    lines: dict[int, str] = {}
    for e in candidates:
        lines.setdefault(e.line_no, e.raw)
    eligible: list[tuple[int, str]] = []
    skipped: list[tuple[int, str, str]] = []
    for line_no in sorted(lines):
        why = _line_skip_reason(line_no, parsed=parsed, root=root)
        if why is None:
            eligible.append((line_no, lines[line_no]))
        else:
            skipped.append((line_no, lines[line_no], why))
    return eligible, skipped


@dataclass(frozen=True)
class _AnalysisSnapshot:
    """What the analysis read (T1) tells the locked apply (T2) about the file.

    The apply re-derives everything else from its own fresh read (ADR-0020 §5) —
    this carries only what a *later* read cannot recover: what the file looked
    like when the report was made.
    """

    eligible: tuple[tuple[int, str], ...]  # (line_no, raw) of the lines §1 cleared
    candidate_raws: frozenset[str]  # raws of every candidate line, cleared or skipped
    occurrences: Counter[str]  # pointer lines carrying each raw

    @classmethod
    def of(
        cls, parsed: ParsedIndex, *, candidates: list[IndexEntry], eligible: list[tuple[int, str]]
    ) -> _AnalysisSnapshot:
        return cls(
            eligible=tuple(eligible),
            candidate_raws=frozenset(e.raw for e in candidates),
            occurrences=Counter(_pointer_lines(parsed).values()),
        )


def _pointer_lines(parsed: ParsedIndex) -> dict[int, str]:
    """Every physical line carrying a pointer, as ``{line_no: raw}``.

    The population §5's multiplicity guard counts, on both the analysis and the
    fresh side: a line's entries collapsed by line number, so a line with two
    links is one occurrence. Counting the two sides over *different* populations
    is how a guard starts reporting a change that never happened.
    """
    lines: dict[int, str] = {}
    for e in parsed.entries:
        lines.setdefault(e.line_no, e.raw)
    return lines


def _splice_lines(original_text: str, remove_line_nos: set[int]) -> str:
    """Return *original_text* with the given 1-based lines removed, byte-exact.

    ADR-0020 §2: every surviving line keeps its exact end-of-line terminator
    (LF vs CRLF) and the file's trailing-newline state is untouched. The
    mechanism is a *splice* of the original text — ``splitlines(keepends=True)``
    has the same 1-based line indexing as the parser's terminator-stripped
    ``splitlines()``, so a parser ``line_no`` maps directly to an index here —
    NOT a reconstruction from ``IndexEntry.raw`` / ``other_lines`` (which are
    terminator-stripped and could not round-trip CRLF or the EOF newline).
    """
    return "".join(
        line
        for i, line in enumerate(original_text.splitlines(keepends=True), start=1)
        if i not in remove_line_nos
    )


def _apply_fix(
    index_path: Path,
    root: Path,
    analysis: _AnalysisSnapshot,
) -> tuple[list[tuple[int, str]], list[tuple[int, str, str]]]:
    """Under the sidecar lock, splice still-eligible candidate lines out of *index_path*.

    Implements ADR-0020 §5's concurrency-aware write. All work happens while
    holding the ``_file_lock`` sidecar lock so a concurrent *memtomem* writer is
    serialized; the agent (the memory hook) does not honor the lock, so a
    residual sub-``os.replace`` race remains and is accepted as bounded — see
    the ADR.

    1. **Fresh, newline-preserving re-read** (``read_bytes().decode`` — NOT
       ``read_text``, which would normalize CRLF→LF before the splice could
       preserve it).
    2. **Re-validate** each candidate against the fresh content + current disk,
       on two axes.

       *Multiplicity.* Matching is count-bounded on the raw line text, counted
       in **physical line occurrences** on both sides — an all-dead line
       carrying two links is one occurrence, not two. A raw whose fresh count
       differs from its analysis-time count is skipped **entirely**: the count
       says how many copies should survive but nothing about *which*, and
       byte-identical lines can sit in different sections, so picking one to
       remove could delete the copy the agent meant to keep. The mismatch fails
       closed and is reported. A count *match* means the agent neither added nor
       removed a copy, so removing them is the net the analysis promised; a
       distinct line the agent added is never a match at all, and an agent
       *edit* to a candidate spares it (its raw stops matching, so the count
       drops and the mismatch skips it).

       *Eligibility.* §1 is re-asked of the **fresh parse**, not carried over —
       it is not a one-time admission check. A resurrected target, or a
       reference definition the agent added that turns an all-dead line into one
       with a live sibling, drops the line here. Drops are reported, not
       silently absent (§1).
    3. **Splice** the still-eligible lines out of the fresh text, carrying
       through everything the agent added before the lock.
    4. **Atomically replace**, preserving the file's existing mode (``mkstemp``
       defaults to ``0o600``, which would silently downgrade a ``0o644`` TOC).

    **The report describes the file this call leaves on disk.** It is built from
    the fresh partition alone; the analysis snapshot contributes only the count
    bound. Every dead pointer still in the file — skipped by §1, dropped by the
    guards above, or written by the agent since analysis — is named, and nothing
    else is. Two properties follow, and both are the point:

    * *No stale coordinates.* A candidate the agent deleted or rewrote between
      the two reads is not reported: it is not in the file, so there is nothing
      to repair and no line to name. The snapshot's line numbers describe a file
      the agent may have rewritten, so a report merged from both can name a line
      that moved — or, when eligibility swaps between byte-identical copies,
      call the same line removed *and* skipped.
    * *"Clean" means clean.* A dead pointer the agent wrote since analysis is
      not removable here (no analysis-time count bounds it) but it is *there*,
      so the run reports it rather than calling the file clean. Re-running
      ``--fix`` clears it, now that an analysis has seen it.

    Returns ``(removed, skipped)`` — the removed lines as ``(line_no, raw)`` for
    the audit report, and the apply-time drops as ``(line_no, raw, why)``.
    """
    import stat

    from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_text

    removed: list[tuple[int, str]] = []
    with _file_lock(_lock_path_for(index_path)):
        # §5.1 — fresh, newline-preserving read (NOT read_text()).
        try:
            fresh_text = index_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Only the read is wrapped: nothing has been written yet, so the
            # caller can report error + removed=[] without hiding a write.
            # Failures past this point (stat, atomic replace, lock teardown)
            # keep propagating — converting those would let a post-commit
            # exception erase removed lines from the audit report (#1769).
            raise _IndexUnreadable(_read_error_message(exc)) from exc
        fresh = parse_memory_index(fresh_text)
        # §5.2 — re-ask §1 of the FRESH read, and report from it. The analysis
        # snapshot contributes only the count bound: its line numbers and
        # verdicts describe a file that may no longer exist, so reporting from it
        # can name a line the agent moved — or contradict this run's own removal.
        fresh_candidates = _missing_target_entries(fresh_text, root=root, parsed=fresh)
        fresh_eligible, skipped = _partition_candidates(fresh_candidates, parsed=fresh, root=root)
        fresh_eligible_by_raw: dict[str, list[int]] = {}
        for line_no, raw in fresh_eligible:
            fresh_eligible_by_raw.setdefault(raw, []).append(line_no)
        fresh_count = Counter(_pointer_lines(fresh).values())
        eligible_count: Counter[str] = Counter(raw for _, raw in analysis.eligible)

        for raw, line_nos in fresh_eligible_by_raw.items():
            if raw not in analysis.candidate_raws:
                # A dead line analysis never saw — the agent added or rewrote it
                # since. Removing it is not this run's to do (no analysis-time
                # count bounds it, and it may precede a file the agent is about
                # to create), but it is a dead pointer sitting in the file, so
                # the report names it rather than passing the file off as clean.
                why = "added since analysis — re-run --fix to remove it"
            elif raw not in eligible_count:
                # Analysis saw this line and skipped it; it is eligible now, so
                # there is no analysis-time count to bound a removal by.
                why = "changed in eligibility since analysis — will not guess which copy to remove"
            elif fresh_count[raw] != analysis.occurrences[raw]:
                why = "changed in number since analysis — will not guess which copy to remove"
            elif len(line_nos) != eligible_count[raw]:
                why = "changed in eligibility since analysis — will not guess which copy to remove"
            else:
                removed.extend((n, raw) for n in line_nos)
                continue
            # Fail closed on every copy: the counts say how many copies should go
            # but nothing about which, and byte-identical lines can sit in
            # different sections. One skip per physical line, so the report names
            # them all (§1) — a raw-level record would undercount duplicates.
            skipped.extend((n, raw, why) for n in line_nos)
        if not removed:
            return [], sorted(skipped)
        # §5.3 — splice out of the FRESH text (agent additions carried through).
        new_text = _splice_lines(fresh_text, {n for n, _ in removed})
        # §5.4 — atomic replace, preserving the original file mode.
        original_mode = stat.S_IMODE(index_path.stat().st_mode)
        atomic_write_text(index_path, new_text, mode=original_mode)
    return sorted(removed), sorted(skipped)


def _collect_fixable(inspect_dirs: list[Path]) -> list[tuple[Path, Path, str]]:
    """Find inspected dirs with a readable index file. Returns ``(dir, index_path, name)``.

    Only providers with a TOC convention (``provider_index_file``) and an index
    file actually on disk are fixable; everything else is skipped silently
    (there is nothing for a subtractive fix to act on).
    """
    from memtomem.config import categorize_memory_dir, provider_index_file

    out: list[tuple[Path, Path, str]] = []
    for d in inspect_dirs:
        resolved = d.expanduser().resolve()
        index_file_name = provider_index_file(categorize_memory_dir(d))
        if not index_file_name:
            continue
        index_path = resolved / index_file_name
        if not index_path.is_file():
            continue
        out.append((resolved, index_path, index_file_name))
    return out


def _run_fix(*, inspect_dirs: list[Path], apply: bool, json_out: bool) -> None:
    """Drive ``--fix`` over every inspected dir's index file (preview or apply).

    Per file the analysis-time read (T1) collects the ``missing_target``
    candidate lines and partitions them (ADR-0020 §1): the eligible ones are
    previewed or handed to :func:`_apply_fix`, which re-reads fresh under the
    lock (T2) and re-validates before writing; the rest are skipped and
    reported for manual repair, while the eligible lines in the same file are
    still fixed. Every removed line is reported too (§5 — even on ``--apply``,
    the removal must be auditable).

    Skipping is a per-*line* partition, not a whole-run refusal: one
    non-conforming line no longer blocks fixing the rest of the file.
    """
    results: list[FixFileResult] = []
    for resolved, index_path, index_file_name in _collect_fixable(inspect_dirs):
        try:
            # T1 (analysis snapshot). Terminator-stripped ``raw`` is CRLF-agnostic,
            # so this read need not be newline-preserving — only the apply splice is.
            text = index_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # An unread index supports no claim — dropping it here made the run
            # report "clean" about a file it never opened (#1769). Report it as
            # a per-file error instead; the other dirs still get their report.
            results.append(
                FixFileResult(
                    str(resolved),
                    index_file_name,
                    applied=apply,
                    removed=[],
                    error=_read_error_message(exc),
                )
            )
            continue
        parsed = parse_memory_index(text)
        candidates = _missing_target_entries(text, root=resolved, parsed=parsed)
        eligible, skipped = _partition_candidates(candidates, parsed=parsed, root=resolved)
        if apply:
            # Run even with nothing to do: ``--apply``'s report describes the
            # file it leaves on disk, and only the locked fresh read knows what
            # that is — an index clean at T1 may have gained a dead pointer by
            # the time the lock is taken, and reporting it clean would be a claim
            # this run never checked.
            #
            # ``_apply_fix`` re-partitions that fresh read and reports from *it*,
            # so its skips replace these rather than joining them: the analysis
            # snapshot's verdicts describe a file the agent may have edited
            # since, and merging the two can report a line as both removed and
            # skipped.
            try:
                removed, skipped = _apply_fix(
                    index_path,
                    resolved,
                    _AnalysisSnapshot.of(parsed, candidates=candidates, eligible=eligible),
                )
            except _IndexUnreadable as exc:
                # The file decoded at T1 but not under the lock — an agent
                # rewrote it to non-UTF-8 in between. Same accounting as the
                # T1 read failure: nothing was written (only the fresh read
                # raises this), so error + removed=[] is the truth.
                results.append(
                    FixFileResult(
                        str(resolved),
                        index_file_name,
                        applied=apply,
                        removed=[],
                        error=str(exc),
                    )
                )
                continue
        elif not candidates:
            results.append(FixFileResult(str(resolved), index_file_name, applied=apply, removed=[]))
            continue
        else:
            removed = eligible
        results.append(
            FixFileResult(
                str(resolved), index_file_name, applied=apply, removed=removed, skipped=skipped
            )
        )

    if json_out:
        _emit_fix_json(results, applied=apply)
    else:
        _emit_fix_human(results, applied=apply)

    # A dead pointer left behind is not success, and neither is an index this
    # run could not read. Exit non-zero so a script cannot read a
    # partially-fixed — or unread — index as a clean one (ADR-0020 §1, #1769).
    if any(r.skipped for r in results) or any(r.error is not None for r in results):
        raise click.exceptions.Exit(1)


def _emit_fix_human(results: list[FixFileResult], *, applied: bool) -> None:
    total = sum(len(r.removed) for r in results)
    skipped_total = sum(len(r.skipped) for r in results)
    error_total = sum(1 for r in results if r.error is not None)
    if total == 0 and skipped_total == 0 and error_total == 0:
        click.secho("No missing_target links to remove.", fg="green")
        return
    verb = "Removed" if applied else "Would remove"
    for r in results:
        if not r.removed and not r.skipped and r.error is None:
            continue
        click.secho(f"\n■ {r.path} · {r.index_file}", bold=True)
        if r.error is not None:
            # "Couldn't even look" is louder than "looked and skipped" (red vs
            # yellow): nothing in this file is verified.
            click.secho(f"  Could not read {r.index_file}: {r.error}", fg="red")
            click.echo("  Nothing removed or verified here — repair the file and re-run --fix.")
            continue
        if r.removed:
            # Lines, not links: an all-dead line may carry several.
            click.echo(f"  {verb} {len(r.removed)} dead pointer line(s):")
            for line_no, raw in r.removed:
                click.echo(f"      - L{line_no}: {raw}")
        if r.skipped:
            click.secho(
                f"  Skipped {len(r.skipped)} line(s) --fix will not remove (repair by hand):",
                fg="yellow",
            )
            for line_no, raw, why in r.skipped:
                click.echo(f"      - L{line_no}: {raw}\n        ({why})")
    n_files = sum(1 for r in results if r.removed)
    if total == 0 and skipped_total == 0:
        # Error-only run (the all-clean case returned above): a "--apply to
        # write" hint would imply there is something to write.
        click.secho(f"\n{error_total} index file(s) could not be read.", fg="red")
        return
    if applied:
        summary = f"\n{total} line(s) removed across {n_files} file(s)."
    else:
        summary = f"\n{total} line(s) across {n_files} file(s). Run with --apply to write."
    if skipped_total:
        # Not a clean run: dead pointers remain, and only a human can clear them.
        summary += f" {skipped_total} line(s) skipped."
    if error_total:
        summary += f" {error_total} index file(s) could not be read."
        click.secho(summary, fg="red")
    elif skipped_total:
        click.secho(summary, fg="yellow")
    elif applied:
        click.secho(summary, fg="green")
    else:
        click.echo(summary)


def _emit_fix_json(results: list[FixFileResult], *, applied: bool) -> None:
    total = sum(len(r.removed) for r in results)
    skipped_total = sum(len(r.skipped) for r in results)
    error_total = sum(1 for r in results if r.error is not None)
    # The outcomes stay distinguishable (ADR-0020 §1): nothing to do, every
    # candidate handled, some candidate left behind, some index never read. A
    # run that skipped a line is never "clean" and never plain "fixed"; a run
    # that could not read an index is "error" no matter what the readable files
    # yielded — a skip is a complete account of what remains, an unread file is
    # no account at all (#1769). One word for dry-run and apply alike: not
    # reading the file is a condition, not an action, and `applied` already
    # carries the run mode.
    if error_total:
        status = "error"
    elif skipped_total:
        status = "partial" if applied else "would-partial"
    elif total == 0:
        status = "clean"
    else:
        status = "fixed" if applied else "would-fix"

    def _reportable(r: FixFileResult) -> bool:
        return bool(r.removed or r.skipped) or r.error is not None

    payload = {
        "status": status,
        "applied": applied,
        "files": [r.to_json() for r in results if _reportable(r)],
        "summary": {
            "files": sum(1 for r in results if _reportable(r)),
            "lines": total,
            "skipped": skipped_total,
            "errors": error_total,
        },
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


# ── Click entry point ───────────────────────────────────────────────


@click.group("memory")
def memory() -> None:
    """Memory-store hygiene: inspect index/DB/disk consistency."""


@memory.command("doctor")
@click.argument("path", required=False, type=click.Path())
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
@click.option(
    "--fix",
    "fix",
    is_flag=True,
    default=False,
    help=(
        "Subtractively remove broken `missing_target` links from the index file "
        "(ADR-0020). Dry-run unless --apply. Other findings are left untouched. "
        "A dead link on a line --fix cannot safely splice is skipped and reported "
        "(exit 1), while the rest of the file is still fixed. An index file it "
        "cannot read is reported as an error (exit 1) instead of being skipped."
    ),
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="With --fix, actually rewrite the index file. Without it, --fix is a dry-run.",
)
def memory_doctor(path: str | None, json_out: bool, fix: bool, apply_: bool) -> None:
    """Report drift between disk, the index file, and the searchable DB (read-only).

    With no PATH, inspects every configured ``memory_dir``. Pass a PATH to
    scope the report to one configured dir.

    Exit codes: ``0`` clean (or advisory-only findings), ``1`` when any
    error-severity finding exists (stale DB sources, convention violations,
    or broken index links).

    ``--fix`` switches to a subtractive curation mode: it removes index-file
    pointer lines whose target is missing on disk (``broken_link`` /
    ``missing_target``) and nothing else (ADR-0020). It is a dry-run preview
    unless ``--apply`` is also passed. A dead link on a line ``--fix`` cannot
    safely delete whole (it carries a link that is not provably dead, or is not
    a single-line bullet the parse accounts for) is skipped, reported for manual
    repair, and exits ``1``; the eligible lines in the same file are still
    fixed. The default report is read-only and unchanged.
    """
    if apply_ and not fix:
        raise click.UsageError("--apply only applies with --fix. See: mm memory doctor --help")

    config = _load_config_read_only()
    memory_dirs = config.indexing.all_index_roots()

    if path is not None:
        target = Path(path).expanduser().resolve()
        inspect_dirs = [d for d in memory_dirs if d.expanduser().resolve() == target]
        if not inspect_dirs:
            raise click.ClickException(
                f"{path} is not a configured memory_dir. Run without PATH to inspect all, "
                "or `mm config` to see the configured dirs."
            )
    else:
        inspect_dirs = list(memory_dirs)

    if not inspect_dirs:
        click.secho("No memory_dirs configured. Run `mm init`.", fg="yellow")
        return

    if fix:
        _run_fix(inspect_dirs=inspect_dirs, apply=apply_, json_out=json_out)
        return

    reports = _gather_reports(config=config, inspect_dirs=inspect_dirs)

    if json_out:
        _emit_json(reports)
    else:
        _emit_human(reports)

    if _severity_totals(reports)["error"] > 0:
        raise click.exceptions.Exit(1)
