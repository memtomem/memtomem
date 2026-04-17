# corpus_v2 — multilingual regression test fixtures

Synthetic technical documentation chunks used by
`test_multilingual_regression.py` to measure search pipeline quality
regressions across topics, genres, and languages.

## Provenance

Chunks in this directory were drafted by **Google Gemini** using the
prompt template in `docs/planning/b2-v2-gemini-template.md` with
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

## Synthetic content disclaimer

All chunks are synthetic fiction for search-ranking regression
testing. **Do not use as operational runbooks, incident response
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
