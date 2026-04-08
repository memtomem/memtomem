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
uv run ruff check packages/memtomem/src packages/memtomem-stm/src --fix
uv run ruff format packages/memtomem/src packages/memtomem-stm/src

# Type check
uv run mypy packages/memtomem/src
```

## Project Structure

- `packages/memtomem/` — Core: MCP server, CLI, web UI, search, storage, indexing
- `packages/memtomem-stm/` — STM proxy gateway for proactive memory surfacing

## Pull Request Guidelines

1. Create a feature branch from `main`
2. Keep changes focused — one feature or fix per PR
3. Add tests for new functionality
4. Ensure `uv run ruff check` and `uv run ruff format --check` pass
5. Ensure `uv run pytest -m "not ollama"` passes
6. Write a clear commit message describing the "why"

## Reporting Issues

Open an issue at https://github.com/memtomem/memtomem/issues with:
- Steps to reproduce
- Expected vs actual behavior
- Environment (OS, Python version, memtomem version)
