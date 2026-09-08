# Exact token budgets and contextual chunks

Enable exact chunk limits with a **local tokenizer.json matching the embedding
model**. Install the `onnx` extra for tokenizer support. The budget feature loads
only that tokenizer; it neither downloads a model nor creates an ONNX session.
Existing installations keep the previous behavior until they opt in.

Example indexing configuration for BGE-M3:

```json
{
  "indexing": {
    "hard_max_chunk_tokens": 4096,
    "chunk_tokenizer_path": "/absolute/path/to/bge-m3/tokenizer.json",
    "chunk_context_tokens": 512,
    "chunk_model_tokens": 8192,
    "enrich_chunk_context": false,
    "exclude_patterns": ["**/generated/**", "**/*.manifest.json"]
  }
}
```

The **body alone** has a 4096-token ceiling, excluding special tokens. The
heading, file/symbol description and fragment information share a separate
512-token budget. The final input, including special tokens, must also fit the
model budget. The existing `max_chunk_tokens`, `target_chunk_tokens` and
`min_chunk_tokens` remain approximate packing preferences. Overlap cannot bypass
the exact ceiling. An impossibly small budget fails rather than dropping text.
Restart MCP servers after changing these settings; the web server also observes
configuration changes. Use `mm index --force` on reviewed source paths to convert
existing rows. This setting does not reset the global embedding policy or
retroactively rewrite the database. An ONNX truncation cap smaller than
`chunk_model_tokens` is rejected on startup; use compatible budgets or explicitly
migrate the embedding policy. The existing BGE-M3 profile with
`embedding.max_sequence_tokens=0` can retain that policy unchanged.

Python is partitioned at classes/functions/methods, then statements and lines.
Imports, decorators, comments, module constants and trailing text are retained.
JavaScript/TypeScript use optional tree-sitter parsing, including TSX grammar.
Descriptions include enclosing symbols, signatures, Python docstrings and nearby
comments. Syntax errors or missing parsers fall back to original text splitting.
Splitting uses original Unicode character slices, never decoded token IDs.

Oversized JSON containers are divided by JSON Pointer. Long string values are
decoded and split at their internal lines where possible. Their line references
continue to identify the original serialized value; decoded newlines are not
reported as physical file lines. The rule applies to any JSON source, including
fixtures, execution records and user documents. Markdown long lines, code fences
and tables receive the same final ceiling even when their usual chunker treats
them as indivisible.

`retrieval_context` is stored separately from `content`, included in both BM25
and dense retrieval, and exposed separately in API/MCP output and export bundles.
Changing a description preserves body identity but triggers re-embedding.
Existing databases receive an additive column with an empty default. FTS rebuilds
retain descriptions. Imports reject an over-budget bundle before writing records;
reindex the original sources and export again. Configured storage also rejects
oversized direct upserts atomically.

Optional LLM enrichment uses the existing configured provider and timeout. It is
off by default. Descriptions are cached by source fragment, structural context,
provider/model/endpoint, prompt and context budget. Errors keep structural context.
Enabling it makes one provider call per uncached fragment; it is separate from the
existing per-source summary feature.

## Preview and exclusions

```sh
python -m memtomem.indexing.budget_audit \
  --db /path/to/memtomem.db \
  --tokenizer /path/to/tokenizer.json \
  --exclude-pattern '**/*.manifest.json' \
  --report /tmp/chunk-budget-preview.json
```

This reads SQLite without writing and previews all indexed code sources plus
sources containing oversized chunks. The report includes source hashes, namespace
and scope inventory, token maxima and exact code coverage, without source bodies.
`--omit-source /absolute/path` excludes a source from this one preview only.

Exclusions use the existing generic `indexing.exclude_patterns` rules, shared by
discovery and direct indexing. There is no hardcoded project directory and no
automatic classification of generated logs. Choose patterns for your corpus.
Excluding a source does not delete its existing rows: review
`mm purge --matching-excluded` before `--apply`. Back up SQLite consistently before
cleanup/reindexing, preserve source metadata, and process large migrations one
file at a time with a small embedding batch. Embedding failure leaves the old
file's chunks intact.

## Deferred: Base64 content handling

Automatic Base64 detection, removal, decoding, image extraction and image
summarization are **deferred from this PR**. No Base64-specific filtering is added
here. A separately reviewed existing chunk can be deleted while preserving its
source file. That is a one-time cleanup: reindexing the unchanged source can bring
the data back. Omit that source from the migration until a content-handling policy
is agreed, or choose an explicit source exclusion rule.

## Index-only masking and source fidelity

Every file-indexing entry point (watcher, CLI, MCP and web), plus the doctor's
preview, uses the same conservative projection before chunking. Original files
are never rewritten. Generic key/value matches with a complete, identifiable
value become `[REDACTED]`; explicit empty assignments and bare inline-code labels
remain unchanged. Unterminated/ambiguous values, specific provider credentials,
private-key patterns and `project_shared` refusals still reach the existing guard.
The shared scanner, explicit exemption accounting and non-file ingress guards
retain their contracts. No `force_unsafe` bypass is used by the migration.

For example, a Python comment containing `"password": "secret"` is indexed as
`"password": "[REDACTED]"`. A command containing `OPENAI_API_KEY=` retains its empty
assignment. Only projected text feeds structural descriptions, optional LLM
prompts, embeddings, FTS and stored chunk bodies. `redaction_count` is the number
of values masked in the source, repeated on its chunks (not a per-chunk count).
It defaults to zero for older rows/exports. Masked projections cannot be edited
back into their source through the chunk-edit endpoints: edit the source, then
reindex. This is detected-secret masking, not a claim to anonymize arbitrary PII.

Code splitting preserves CRLF and Unicode. Descriptions and fragment numbering
are generated after whitespace packing. JSON with duplicate keys or excessive
nesting falls back to raw, lossless splitting; no duplicate value is discarded.
The token limiter inspects bounded character windows (at most 65,536 characters)
rather than repeatedly encoding the entire unconsumed file.

## Reviewed migration

`python -m memtomem.indexing.budget_migration plan --manifest PLAN.json`
reads the current configuration and previews indexed code/JSON and sources whose
stored bodies or composed inputs exceed the limits. `--omit-source PATH` is a
generic, repeatable migration-only omission. It does not modify watcher rules.
Reports contain hashes, counts and metadata, never source bodies or secret values.

`python -m memtomem.indexing.budget_migration apply --manifest PLAN.json --report RESULT.json`
creates an owner-only SQLite backup and replaces one source at a time. It does
not rewrite configuration. The process uses one embedding item per batch and
turns off optional LLM/rerank work for the migration only. Source content,
configuration, tokenizer and stored metadata must still match the preview.
Namespace, scope, tags, validity and provenance are preserved; ambiguous metadata
or changed sources are refused. Embedding finishes before deletion. A completion
receipt commits in the same transaction as the chunks, so repeating the command
resumes completed sources after checking their receipt and current state.

Chunk-policy changes require a Core restart. Hot reload refuses these changes
before publishing a partially updated engine/storage policy. Installing new
package files alone does not update already-running Core processes; deployment
must verify new process generations and their loaded code.

Optional description caches replace the prior generation per source, retain at
most 256 entries per source, and are cleared on source deletion. A source that
has never committed chunks cannot leave an orphan cache after embedding failure.
Base64 detection, extraction and summarization remain deferred. Omitting the
reviewed Base64 source from a migration does not implement a future-ingestion
exclusion: editing that source can make its content eligible again.
