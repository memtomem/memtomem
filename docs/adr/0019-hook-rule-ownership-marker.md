# ADR-0019: Hook-rule ownership marker for idempotent re-sync

**Status:** Accepted
**Date:** 2026-05-25
**Context:** ADR-0018 fans the canonical `.memtomem/settings.json` hooks record
out to Claude / Codex / Gemini through `_merge_hooks_record`, whose conflict
detection keys on `(event, matcher)` and keeps the existing rule (with a
warning) on a same-key collision — the "user rules always win" contract. That
contract cannot tell a *user-authored* rule apart from a rule **memtomem itself
wrote in a previous release**. So when memtomem's generated output for an
`(event, matcher)` changes between two releases a user syncs across (a
`command`, `timeout`, or handler-`name` change), the next
`mm context sync --include=settings` treats memtomem's own stale rule as a user
conflict, refuses to update it, and the install stays out-of-sync until the
user deletes the rule by hand (issue #1110). This ADR records the fix. It
**layers onto** ADR-0018 rather than amending it, per the repo convention
(ADR-0018 itself layers onto ADR-0010; ADR-0010 rejects in-place amendment of
an Accepted ADR).

## Decision

1. **Ownership marker, using only documented handler fields.** memtomem stamps
   every hook rule it generates with a content-independent ownership marker so
   re-sync can recognize — and replace — its own rules. A *custom* key (e.g.
   `_memtomem`) was rejected: the runtime hook schemas do not document
   tolerance for unknown keys, and a strict validator could reject the entire
   settings file. The marker therefore reuses officially documented fields:

   | Runtime | Marker field | Reserved value |
   |---|---|---|
   | Claude / Codex | command-handler `statusMessage` | prefix `"memtomem · "` |
   | Gemini | handler `name` | prefix `"memtomem-"` |

   `statusMessage` is the only free-string field both Claude and Codex document
   on command hooks; Gemini handlers already carry a synthesized `memtomem-`
   `name` (ADR-0018), so that prefix is reused. Both prefixes are a **reserved
   namespace**: a rule whose handler `name`/`statusMessage` starts with them is
   memtomem-owned and will be overwritten on re-sync. Hand-editing such a rule
   while keeping the prefix loses the edit — to take ownership, drop the marker.

2. **Stamping is idempotent and preserves author text.** `statusMessage` is set
   to `"memtomem · {event}"` when absent, prefixed (`"memtomem · {text}"`) when
   the canonical handler already supplies text, and left untouched when already
   prefixed — a pure, deterministic function of the handler, so an unchanged
   re-sync is byte-stable. For Gemini, the `memtomem-` `name` is **always
   (re)stamped**, overriding any canonical-provided `name`, because the name is
   the ownership marker and memtomem must own it.

3. **Ownership-aware merge.** On a same-`(event, matcher)` collision in
   `_merge_hooks_record`:
   - an existing **memtomem-owned** rule is replaced *in place* by the freshly
     generated contribution (memtomem updates its own rule), while user rules —
     including a user rule under the same matcher — are preserved verbatim;
   - an existing **user** rule wins and a guided warning is emitted. Rule
     equality for the "already in sync" check ignores the marker fields
     (`_rule_content_equal`) so a user's byte-identical hand-written rule is
     never flagged as a conflict the moment memtomem stamps its own copy.

4. **Legacy rules: warn, never clobber.** A rule memtomem wrote *before* this
   shipped carries no marker. On the first post-upgrade sync it is still a user
   rule, so memtomem does **not** silently overwrite it (a genuine user rule
   that merely shares a command must not be clobbered). Instead, when an
   unmarked colliding rule shares a `command` with the contribution, the warning
   is sharpened to "looks like a memtomem-managed rule from a previous version —
   remove it and re-run sync." After one marked sync, the rule self-updates.

5. **All Claude write paths agree.** The CLI merge, the web `GET
   /settings-sync` diff (`_compare_hooks` stamps the canonical before comparing
   and classifies a differing memtomem-owned target rule as *pending*, not a
   conflict), and the web `POST /settings-sync/resolve` "use memtomem's version"
   action all stamp through the one shared helper, so no path writes an unmarked
   rule.

## Consequences

- memtomem can update its own hook rules across releases without manual user
  cleanup; the long-standing "user rules always win" contract is unchanged for
  genuinely user-authored rules.
- The marker is visible: Claude/Codex users see `memtomem · …` as the hook's
  spinner message. This is intentional (transparent provenance).
- No new runtime-schema risk: only documented fields are written; the Gemini
  file never receives `statusMessage`, and Claude/Codex never receive a
  Gemini-only field.
- One-time upgrade gap: pre-marker rules warn once (command-changed) or after a
  single re-sync (command-unchanged) before they become owned.

## Considered & rejected

- **Custom marker key (`_memtomem: true`).** Simplest and uniform, but the
  runtime docs don't confirm unknown keys are ignored; a strict validator could
  reject/ignore the whole settings file and silently break the user's hooks.
- **Signature-based ownership only** (reuse `settings_doctor`'s
  `(event, matcher, command-shape)` signature). Zero new fields, but the #1110
  case is precisely a `command` change, which breaks signature matching — so it
  cannot recognize the very rules it needs to update. Kept only as the
  best-effort legacy heuristic in decision 4.
