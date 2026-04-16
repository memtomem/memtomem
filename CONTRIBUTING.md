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
  This is acceptable for a local-only server but would need sanitisation
  if the server were exposed over a network.

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
