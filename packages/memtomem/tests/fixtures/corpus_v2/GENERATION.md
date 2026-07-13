# Public synthetic corpus generation specification

This specification is the reproducible content contract for `corpus_v2`.
It deliberately contains no source excerpts from user memories, customer
records, private conversations, or internal company documentation.

## Fixed shape

- Languages: `en`, `ko`
- Topics: caching, PostgreSQL, cost optimization, security, observability,
  Kubernetes
- Genres per topic: runbook, postmortem, ADR, troubleshooting
- Files: 2 languages x 6 topics x 4 genres = 48
- H2 chunks: 4 per file = 192
- Queries: 50 per language = 100

## Draft prompt contract

For each language, topic, and genre, draft four independent fictional
technical passages. Each passage must:

1. use one primary tag from the closed vocabulary in
   `tools/retrieval-eval/drift_validator.py`;
2. contain at least two concrete but non-sensitive technical artifacts;
3. follow the genre form: imperative runbook, past-tense postmortem,
   decision/trade-off ADR, or symptom-diagnosis-remediation troubleshooting;
4. invent all organizations, incidents, projects, and timelines;
5. use `example.com`, reserved/private IP ranges, and
   `<SYNTHETIC_...>` placeholders when an example identifier is needed;
6. never emit credentials, personal contact details, customer data, or text
   copied from a supplied document.

The model output is only a draft. A maintainer validates the tags, genre,
language quality, technical plausibility, duplicate overlap, and privacy
audit before committing it. Benchmark execution consumes only the committed
files and never calls an LLM.

## Query generation

Queries are authored against the closed primary tags, not extracted from
search results. The fixed distribution is direct, paraphrase,
underspecified, multi-topic, negation, and genre-primary. Paraphrase queries
must avoid copying a complete corpus sentence. Every target must exist as a
primary tag in the query language.
