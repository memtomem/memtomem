# ADR-0002: Per-Entry Tag Promotion via Blockquote Header

**Status:** Accepted
**Date:** 2026-04-25
**Audience:** memtomem maintainers touching the markdown chunker
(`packages/memtomem/src/memtomem/chunking/markdown.py`) or the `mem_add` /
`mem_edit` writer (`packages/memtomem/src/memtomem/tools/memory_writer.py`).

## Context

`mem_add(tags=[...])` advertises tags as a first-class API parameter, but
prior to v0.1.28 the value never reached `ChunkMetadata.tags`, so
`mem_search(tag_filter=...)` silently missed anything added through this
path. The two halves of the contract disagreed:

- **Writer** (`memory_writer.append_entry`) emitted the tags as a bare
  `tags: ['x']` line glued onto the `> created:` blockquote via
  CommonMark's lazy continuation, in Python's `repr()` format.
- **Reader** (`MarkdownChunker.chunk_file`) only inspected file-level YAML
  frontmatter; the per-entry blockquote header was passed through into
  chunk content unchanged. Tags survived BM25 search by accident
  (substring of the chunk body) but never populated metadata.

`tag_filter` matching is set membership at
`packages/memtomem/src/memtomem/search/pipeline.py:364–366`:

```python
if tag_filter:
    required = {t.strip() for t in tag_filter.split(",") if t.strip()}
    fused = [r for r in fused if required & set(r.chunk.metadata.tags)]
```

A chunk whose `metadata.tags` is `()` cannot match `tag_filter` no matter
what its content contains — there is no substring fallback on this path.
The bug surfaced during PR #462 multi-agent e2e testing, where
`mem_agent_share` emits a `shared-from=<src-uuid>` audit tag and the
assertion on `copy.metadata.tags` failed (the tag sat in `copy.content`
as a string instead).

## Decision

Promote the per-entry blockquote header to a first-class metadata
channel. Three coordinated changes, shipped together in v0.1.28
(PRs #463 reader, #464 writer + `mem_edit`, #465 batch broadcast cleanup,
plus #466/#468 line-counter follow-ups).

### 1. Writer emits a canonical blockquote group

`memory_writer.append_entry` (`memory_writer.py:14–44`) now emits every
metadata line with an explicit `> ` prefix and JSON-encodes the tag list:

```python
# memory_writer.py:33
tag_line = f"\n> tags: {json.dumps(list(tags))}" if tags else ""
```

Output:

```
## Heading

> created: 2026-04-25T...+00:00
> tags: ["cache", "shared-from=abc"]

body...
```

JSON is valid YAML; Python `repr()` of a list (single-quoted) is not.
The explicit `> ` on every line removes the dependency on CommonMark
lazy continuation. Empty / `None` tags omit the line entirely.

### 2. Reader extracts tags from the section-leading blockquote

`MarkdownChunker.chunk_file` (`chunking/markdown.py:165–257`) calls a new
helper `_extract_section_blockquote_tags`
(`chunking/markdown.py:299–384`) on each section's text. The helper:

1. Scans the first non-blank block after the heading.
2. If it is a blockquote, collects the contiguous group — including
   lazy-continuation lines so files written by older `mem_add` reindex
   correctly without rewriting them on disk.
3. Looks for a single `tags:` key (case-sensitive).
4. Parses the value via the shared `_parse_tags_value` helper
   (`chunking/markdown.py:259–284`), which the file-level
   `_extract_frontmatter_tags` (`chunking/markdown.py:286–297`) also
   uses. Four input shapes are accepted: inline list (`["a", "b"]` /
   `['a', 'b']` / `[a, b]`), single bare value, and block list (`- item`
   continuation lines).
5. Strips the blockquote group from the returned section text — and
   reports the strip line count so `_split_section`
   (`chunking/markdown.py:386–`) can keep oversized-section sub-chunk
   `start_line` / `end_line` aligned with the source file.

The parser is constrained to **section-leading** blockquotes only.
A mid-section quoted paragraph that happens to contain a `tags:` line
is left alone. This matches the writer (which only ever emits the
header at section start) and avoids false positives from user prose.

### 3. Frontmatter and section tags compose by union

For each emitted chunk (`chunking/markdown.py:206`):

```python
combined_tags = tuple(sorted(set(fm_tags) | set(section_tags)))
```

File-level frontmatter tags apply to every chunk in the file (unchanged
behavior). Section-leading blockquote tags apply only to chunks of that
section — `_split_section` carries them onto every sub-chunk. Empty
sets compose to empty.

### 4. `mem_edit` preserves the header

Because the reader strips the blockquote header from chunk content,
naïve `replace_lines(meta.start_line, meta.end_line, new_content)`
would overwrite the entire `> created:` / `> tags:` block with
user-supplied body text. `memory_writer.replace_chunk_body`
(`memory_writer.py:89–126`) detects the header span via
`_find_body_start_index` (`memory_writer.py:47–86`) and prepends it
back to `new_content`. A user who supplies a string starting with
`## ` is treated as a full-replacement override, preserving the
pre-RFC `mem_edit` semantic.

### 5. `mem_batch_add` no longer broadcasts

`memory_crud.add_entries_batch` (`memory_crud.py:380–387`) previously
collected `all_tags ∪ every-chunk-in-file` for batch adds — necessary
when per-entry tags were lost in content, but redundant once the
reader correctly tags only the relevant chunk and over-applies (a tag
from entry A would leak onto entry B's chunk). The post-RFC code
passes per-entry tags straight through to `append_entry`.

## Consequences

- **`mem_add(tags=[...])` round-trips.** `mem_search(tag_filter="x")`
  matches chunks added through any path that hits `append_entry`,
  including `mem_agent_share`'s `shared-from=` audit tag.
- **One-time UUID churn on reindex.** Stripping the blockquote header
  from chunk content changes `content_hash = sha256(content)`
  (`models.py:97`), which the differ treats as a new chunk → fresh
  `uuid4()`. External pins of `chunk_id` (notebooks, cross-LTM,
  scripts) miss after reindex; `shared-from=<old-uuid>` audit chains
  break because the source UUID is gone. This was called out in the
  v0.1.28 CHANGELOG; the chunk_links foreign-key follow-up
  (PRs #469/#470) closes the recovery gap structurally.
- **Backward-compatible reindex.** Files written by older `mem_add`
  (lazy-continuation, Python-repr list) reindex correctly via the
  lazy-continuation branch + the legacy-quote branch in
  `_parse_tags_value`. No on-disk migration required. Manual
  hand-edited markdown without a leading blockquote is untouched.
- **Strip-from-content invariant.** Chunk content diverges from the
  raw file on disk for any file that contains a section-leading
  metadata blockquote. Acceptable: chunks are an indexed view, the
  file is the source of truth, and the Web UI renders raw markdown
  rather than chunks.
- **`tag_filter` semantics unchanged.** Set membership in
  `pipeline.py:366`. Populating `metadata.tags` was sufficient — no
  search-pipeline change.

## Alternatives considered

1. **Top-of-file frontmatter merge on every `mem_add`** — write the
   union of every entry's tags into the file's YAML frontmatter.
   Rejected: loses per-entry granularity (`tag_filter` would match
   every chunk in the file, not just the tagged entry), and rewriting
   the head of an append-only memory file is a write-amplification
   footgun for large files.
2. **HTML comment metadata** (`<!-- tags: [...] -->`) — invisible to
   readers, doesn't show up in markdown previews. Rejected: hides an
   API surface from the human reading the file; the Web UI memory
   view already renders blockquote headers naturally.
3. **Heading hashtags** (`## Title #tag1 #tag2`) — pollutes the
   heading used for hierarchy + `file_context`, and the hashtag idiom
   collides with markdown anchor generation in some renderers. BM25
   already covers keyword search inside the body, so the value-add
   is only `tag_filter`, which doesn't justify the cost.
4. **Drop `mem_add(tags=)` from the API** — honest about what was
   indexed pre-RFC. Rejected: the parameter is documented and used by
   `mem_agent_share` (audit tag), tool callers, and the `mem_do`
   surface. Removing it is a behavior break with no replacement story
   for `tag_filter`.

## Tests

- `packages/memtomem/tests/test_chunking_blockquote_tags.py` — section
  parser cases (canonical, lazy, legacy repr, frontmatter union,
  mid-section non-promotion, leading blockquote without `tags:`).
- `packages/memtomem/tests/test_memory_writer_tag_format.py` — pins
  the writer's canonical output so a future regression to lazy /
  Python-repr fails fast.
- `packages/memtomem/tests/test_multi_agent_integration.py`
  `test_case_b_share_trail` — asserts both `metadata.tags`
  membership and the `mem_search(tag_filter=f"shared-from=...")`
  round-trip.
