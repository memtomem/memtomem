# corpus_v2 — public synthetic multilingual retrieval benchmark

Public, synthetic technical-documentation chunks used to measure search
pipeline quality regressions across topics, genres, and languages. The
dataset contains no customer records, user memories, private conversations,
or internal company documents.

## Provenance

Chunks in this directory were drafted by **Google Gemini** using the
public generation specification in [`GENERATION.md`](GENERATION.md) with
closed-set subtopic vocabulary and genre style constraints, then
**curated and normalized by a human maintainer** (subtopic drift
correction, format conversion to markdown, deduplication) before
commit.

Raw Gemini output was not accepted verbatim: each chunk was reviewed
for:

- Subtopic vocabulary conformance to the closed list in
  `b2-v2-gemini-template.md`
- Primary subtopic diversity within each batch (≥ 2 distinct
  primaries)
- Genre style fidelity (imperative for runbook, past-narrative with
  timestamps for postmortem, decision-frame for ADR,
  symptom→diagnosis→cause→workaround for troubleshooting)
- Technical specificity (≥ 2 concrete artifacts per chunk: commands,
  config keys, metric names)
- Korean authenticity (no translation-style phrasing)

Chunks that failed review were rejected or regenerated with adjusted
prompts.

Every committed revision is checked by
`tools/retrieval-eval/audit_public_corpus.py`. The audit fails if the corpus
contains a value caught by memtomem's secret scanner, an email outside the
reserved `example.*` domains, or a globally routable literal IP address. It
also pins the 48-file / 192-chunk / 100-query shape and emits content hashes
for benchmark manifests.

## Synthetic content disclaimer

All names, organizations, incidents, and timelines are synthetic fiction for
search-ranking regression testing. Credential-shaped examples use explicit
`<SYNTHETIC_...>` placeholders; secret-detector canaries live in separate
privacy tests and are not part of this retrieval corpus. **Do not use this
dataset as operational runbooks, incident response
guides, or architecture guidance without independent verification.**
Specific commands, config syntax, version numbers, and default
behaviors described here are plausible but not validated against
current software releases.

## Directory layout

```
corpus_v2/
├── {language}/
│   └── {topic}/
│       ├── runbook.md
│       ├── postmortem.md
│       ├── adr.md
│       └── troubleshooting.md
```

Each genre file contains 3-4 H2 sections (chunks), each with
`<!-- primary: topic/subtopic -->` and optional
`<!-- secondary: ..., ... -->` tags used for relevance judgments.

## License

Same as the memtomem repository (Apache-2.0). Synthetic content is
author-curated and is distributed under the repo license.
