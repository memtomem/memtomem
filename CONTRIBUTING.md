# Contributing to memtomem

Thank you for your interest in contributing to memtomem!

## Development Setup

```bash
# Clone
git clone https://github.com/memtomem/memtomem.git
cd memtomem

# Install (requires Python 3.12+ and uv)
uv sync

# Run tests
uv run pytest -m "not ollama"          # skip Ollama-dependent tests
uv run pytest                          # full suite (requires running Ollama)

# Lint and format
uv run ruff check packages/memtomem/src --fix
uv run ruff format packages/memtomem/src

# Type check
uv run mypy packages/memtomem/src
```

## Project Structure

- `packages/memtomem/` — Core: MCP server, CLI, web UI, search, storage, indexing
- `packages/memtomem-claude-plugin/` — Claude Code plugin (experimental)
- `packages/memtomem-openclaw-plugin/` — OpenClaw plugin (experimental)

The STM proxy gateway lives in a separate repository: [memtomem/memtomem-stm](https://github.com/memtomem/memtomem-stm).

## Pull Request Guidelines

1. Create a feature branch from `main`
2. Keep changes focused — one feature or fix per PR
3. Add tests for new functionality
4. Ensure `uv run ruff check` and `uv run ruff format --check` pass
5. Ensure `uv run pytest -m "not ollama"` passes
6. Write a clear commit message describing the "why"
7. Sign the CLA on your first pull request (see below)

## Maintainer response expectations

memtomem is maintained by a small team. We aim for the following
**best-effort** windows on contributions — these are targets, not legal
commitments:

| Event | Target |
|-------|--------|
| First response on a new issue or PR | within 5 business days |
| Decision on a PR (merge / changes-requested / close) | within 14 business days of the first response |
| Security report (see [`SECURITY.md`](SECURITY.md)) | acknowledgement within 2 business days |

If a PR sits without contributor activity for 30 days the [stale
bot](.github/workflows/stale.yml) marks it `stale`; another 30 days of
silence closes it. A single comment or push clears the label — closure is
never a judgement on the change, just queue hygiene. Re-opening is always
welcome.

Labels that affect triage:

- `do-not-stale`, `pinned`, `release`, `security`, `needs-maintainer-decision`
  — exempt from the stale sweep.
- `needs-cla` — CLA Assistant has not yet recorded a signature; merge is
  blocked until the bot re-checks (see CLA section below).
- `good-first-issue` — scoped to ≤3 files and no public API changes; check
  the comments for an existing claim before starting work.

If your PR is time-sensitive (security, regression on `main`, release
blocker) call it out in the description so we route it ahead of the queue.

## MCP Tool Error Response Contract

All MCP tool handlers use `@tool_handler` (`server/error_handler.py`) which
catches exceptions and returns one of four string prefixes:

| Prefix | When |
|--------|------|
| `Error: {msg}` | Known exceptions (`ValueError`, `StorageError`, etc.) or manual validation returns |
| `Error (retryable): {msg}` | `RetryableError` — transient failure, safe to retry |
| `Error (permanent): {msg}` | `PermanentError` — will not resolve with retries |
| `Error: internal error ({ExcType}: {msg})` | Unexpected exceptions |

**Key design decisions:**

- **Errors are always string returns, never raised exceptions.** The decorator
  catches all `Exception` subclasses and converts them to `"Error: …"` strings.
  This means the MCP protocol-level `isError` flag is never set by LTM tools.
  The STM proxy detects errors via `result.isError` (protocol level), not by
  parsing the `"Error: "` prefix — currently there is no programmatic consumer
  of the prefix in the STM proxy.
- **All new tools must use `@tool_handler`.** Without it, unhandled exceptions
  produce MCP protocol errors instead of user-friendly messages.
- **`str(exc)` is the message surface.** `FileNotFoundError` and
  `PermissionError` include full file paths in their default `str()`.
  See "Deployment assumptions" below.

**Deployment assumptions:** The error contract assumes a **local-only
server** (stdio or localhost). `str(exc)` for `FileNotFoundError` and
`PermissionError` exposes full filesystem paths in tool responses. If the
server is deployed over a network (SSE/HTTP), these messages must be
sanitised before reaching external clients — either by wrapping the
exceptions in the decorator or by adding a response filter. Changing the
deployment model without addressing this turns error messages into an
information disclosure surface.

## CLI output convention

When a CLI command needs machine-readable output, pick the option shape by
the command's output semantics, not by parity with any particular existing
command:

| When | Use |
|------|-----|
| The only meaningful alternative to default human-readable output is JSON (binary "human vs machine" scenario) | `--json` flag |
| There are genuine non-JSON output modes beyond cosmetic variants — e.g. `plain`, `context`, `smart`, `diff` | `--format [table\|json\|...]` |

Examples in the current CLI:

- `--json` — `mm watchdog status`, `mm watchdog run`, `mm config show`
  (alias of `--format json`).
- `--format` — `mm search` (has `context`, `smart`), `mm recall` (has
  `plain`), `mm config show` (keeps the original option alongside
  `--json`).

**If the two-mode nature of a new command is uncertain** — i.e. it's
plausible a `context` / `digest` / `diff` mode gets added later — choose
`--format` from the start. Migrating from `--json` flag to `--format` is a
breaking change for scripts; going the other way isn't necessary.

### JSON error shape

Pick the `--json` error shape by whether the command's success payload
already disambiguates from an error:

| Command kind | Success shape                       | Error shape                              |
|--------------|-------------------------------------|------------------------------------------|
| **Read** (`list`, `get`, `show`, `events`, `status`) | `{"<items>": [...], "count": N}` — the keys self-disambiguate | `{"error": "<reason>"}` |
| **Write** (`log`, `add`, `set`, `run`) | `{"ok": true, ...}` — explicit flag | `{"ok": false, "reason": "<reason>"}` |

Both shapes keep **exit code 0** under `--json`; the JSON body carries
the outcome, not the exit code, so `cmd --json | jq` pipelines don't
break on a handled failure. Unhandled exceptions (programmer errors,
not expected failure modes) should still surface through Click.

Rationale: read success payloads (`events: [...]`, `sessions: [...]`)
are naturally disambiguated from `{error: ...}` by presence-of-key, so
an explicit `ok` flag is redundant noise. Write acks have no such
natural disambiguator — `{"event_id": ...}` vs `{"error": ...}` forces
consumers to check by key rather than by a single boolean. An explicit
`ok` flag is clearer for writes.

The text-path behavior is unchanged by `--json`. No-op / no-session
cases that are silent under text (hook callers depend on silence)
should emit the JSON error shape only when `--json` is set, so adding
the flag never breaks an existing text-path caller.

## Contributor License Agreement (CLA)

Before we can merge your first pull request, you need to sign the
[Contributor License Agreement](CLA.md). The CLA Assistant bot will
automatically comment on your PR with instructions — you sign by replying
with:

> I have read the CLA Document and I hereby sign the CLA

You only need to sign once per GitHub account per repository. Because
memtomem and [memtomem-stm](https://github.com/memtomem/memtomem-stm) are
separate repositories with independent signature stores, contributors who
open pull requests against both projects need to sign in each repository
(still one-time per account). Your signature is stored in
`signatures/v1/cla.json` in whichever repository you signed.

The CLA is adapted from the Apache Software Foundation Individual
Contributor License Agreement with one additional section covering future
licensing rights. This preserves DAPADA Inc.'s ability to adopt different
license terms for the Work in the future (for example, a dual-licensing
arrangement) without needing to re-collect consent from every contributor.
The CLA does not change the current license of the Work, which remains
Apache License 2.0.

For questions about the CLA, contact contact@dapada.co.kr.

## Reporting Issues

Open an issue at https://github.com/memtomem/memtomem/issues with:
- Steps to reproduce
- Expected vs actual behavior
- Environment (OS, Python version, memtomem version)
