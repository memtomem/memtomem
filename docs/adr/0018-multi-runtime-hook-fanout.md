# ADR-0018: Multi-runtime hook fan-out (Codex + Gemini)

**Status:** Accepted
**Date:** 2026-05-25
**Context:** ADR-0010 established the `hooks.target_scope` tiers but fan-out
was Claude-only (`~/.claude/settings.json`). Codex CLI and Gemini CLI have
since shipped their own hook systems, so the single canonical
`.memtomem/settings.json` hooks record can now fan out to all three runtimes —
the highest-automation-value artifact in the gateway, previously the only one
locked to one runtime. This ADR records that decision and the conversion
rules. It **layers onto** ADR-0010 rather than amending it, per the repo
convention (ADR-0010 §"Considered & rejected" rejects in-place amendment of an
Accepted ADR; ADR-0007 / ADR-0008 layer onto ADR-0001 the same way).

## Decision

1. **Three generators.** `SETTINGS_GENERATORS` gains `CodexSettingsGenerator`
   and `GeminiSettingsGenerator` next to `ClaudeSettingsGenerator`. The
   canonical hooks record (keyed by Claude event name) fans out to:

   | Runtime | user scope | project_shared | project_local |
   |---|---|---|---|
   | Claude | `~/.claude/settings.json` | `<proj>/.claude/settings.json` | `<proj>/.claude/settings.local.json` |
   | Codex  | `~/.codex/hooks.json` | `<proj>/.codex/hooks.json` | — (none) |
   | Gemini | `~/.gemini/settings.json` | `<proj>/.gemini/settings.json` | — (none) |

   > **2026-06 (#1247):** A fourth runtime shipped after this ADR: **Kimi
   > CLI** (`KimiSettingsGenerator`, `context/settings.py`). Targets: user →
   > `~/.kimi/config.toml`, project_shared → `<proj>/.kimi/config.toml`,
   > project_local → none (the same `target_file() → None` skip contract as
   > Codex/Gemini, decision 4). Kimi's config is TOML, not JSON: instead of
   > the additive JSON merge, sync preserves the user's file verbatim and
   > replaces only a memtomem-managed block delimited by
   > `# BEGIN memtomem managed hooks` / `# END memtomem managed hooks`.
   > Ownership is block-scoped, not per-rule — see the companion note on
   > ADR-0019.

2. **Codex is near-identical.** Codex shares Claude's event names and accepts
   `Bash` / `Edit` / `Write` matchers natively, so it reuses the same
   `_merge_hooks_record` additive merge as Claude. Events Codex lacks
   (`Notification`, `SessionEnd`) are dropped with a warning. Codex hooks are
   written to a dedicated `hooks.json` (the user's `config.toml` is left
   untouched).

3. **Gemini is a lossy remap** (`_remap_for_gemini`), applied before the shared
   merge:
   - *Event map*: `PreToolUse`→`BeforeTool`, `PostToolUse`→`AfterTool`,
     `SessionStart`, `SessionEnd`, `Notification`, `PreCompact`→`PreCompress`.
     Plus two **best-effort lifecycle mappings — approximate, not a verified
     1:1** (Gemini documents no exact analog): `UserPromptSubmit`→`BeforeAgent`
     (prompt-time context injection) and `Stop`→`AfterAgent` (session/agent
     close). These preserve those memtomem hook paths on Gemini while
     acknowledging the firing timing is not formally identical. Canonical
     events with no Gemini equivalent (`SubagentStop`, `PermissionRequest`)
     are dropped with a warning.
   - *Tool-name matcher map* (tool events only): `Bash`→`run_shell_command`,
     `Edit`/`MultiEdit`→`replace` (Gemini's in-place edit tool), `Write`→
     `write_file` (whole-file create/overwrite), `Read`→`read_file`. A matcher
     whose tokens don't all map is dropped with a warning — a hook that can't
     match its tool would never fire. Empty / separator-only matchers map to
     `*` (all tools). Handlers gain a synthesized `name` (Gemini hook entries
     carry one).

4. **No `project_local` fan-out for Codex/Gemini.** `target_file()` returns
   `None` there, which widens the `SettingsGenerator.target_file` contract to
   `Path | None`. Every consumer (`generate_all_settings`, `diff_settings`,
   `host_write_targets`, `detector.detect_settings_files`) treats `None` as
   *skipped* (no write, not an error).

5. **Lossy conversion is surfaced, never silent.** Every dropped event or
   matcher produces a message in `SettingsSyncResult.warnings`, shown by the
   CLI / MCP / Web front-ends. Front-ends must also report a per-runtime
   `error` / `aborted` status (not just Claude's) — a partial failure is not a
   success. A `--strict` mode (fail instead of warn) is future work.

## Verification

Mappings were verified against the official docs (2026-05):

- Claude — <https://code.claude.com/docs/en/hooks>
- Codex — <https://developers.openai.com/codex/hooks> (event names; matchers
  accept `Bash` / `apply_patch` and the `Edit` / `Write` aliases)
- Gemini hooks — <https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/writing-hooks.md>
  (event list, hook-entry `name`/`type`/`command` shape)
- Gemini tools — <https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/file-system.md>
  (`replace` = in-place edit, `write_file` = create/overwrite, `read_file`,
  `run_shell_command`). The `UserPromptSubmit`→`BeforeAgent` and
  `Stop`→`AfterAgent` lifecycle mappings are best-effort — Gemini documents no
  exact analog — see Decision §3.

## Consequences

- Settings is now the only artifact that fans a single canonical record out to
  three runtimes. The conversion tables in `context/settings.py`
  (`_GEMINI_EVENT_MAP`, `_GEMINI_TOOL_MAP`, `_CODEX_EVENTS`) are the source of
  truth and must be extended as the runtimes add events/tools.
- Gemini fan-out is inherently lossy; the emitted warnings are the contract,
  not a defect. Users who need 1:1 fidelity author per-runtime hooks directly.
- Rich Web surfacing of per-runtime conversion status (a runtime-readiness
  doctor + per-artifact compatibility badges) is intentionally deferred to a
  follow-up PR. This PR keeps the Web change minimal: the sync result list
  already renders per-runtime `status` / `warnings` generically, and the
  Settings hooks Sync button no longer masks a per-runtime `error` / `aborted`
  as success.

## References

- ADR-0010 — settings hooks target scope (this ADR layers on it).
- ADR-0001 §1 — canonical = project-scope policy; ADR-0007 / ADR-0008 layering
  precedent.
- `packages/memtomem/src/memtomem/context/settings.py` — generators + mapping
  tables; `tests/test_context_settings_multiruntime.py` — conversion pins.
