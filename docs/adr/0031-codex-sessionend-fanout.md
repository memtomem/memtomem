# ADR-0031: SessionEnd fan-out to Codex, and per-event contract translation

**Status:** Accepted
**Date:** 2026-08-01
**Context:** ADR-0018 §2 recorded Codex hook fan-out as "near-identical" to
Claude: supported events pass through verbatim, and the events Codex lacks —
recorded there as `Notification` **and `SessionEnd`** — are dropped with a
warning. The `SessionEnd` half of that was wrong. Codex documents the event
("When the main thread ends `SessionEnd`",
<https://learn.chatgpt.com/docs/hooks>), so every canonical `SessionEnd` hook
was being withheld from `~/.codex/hooks.json` under a warning that blamed
Codex for a limitation memtomem invented (#1976).

Correcting the event list alone would not have been enough, and that is why
this needs an ADR rather than a bug fix note. `SessionEnd` turns out to be the
first event whose **contract** — not just its name — differs between runtimes,
which ADR-0018's verbatim-pass-through model has no place for. Routing it to
Codex unchanged would trade a loud, wrong drop for a quiet misconfiguration.

This ADR layers onto ADR-0018 and **supersedes its §2 in part**: the event list
and the "verbatim pass-through" rule. Everything else in ADR-0018 — the three
(now four) generators, the additive merge, `target_file() → None` for
`project_local`, and "lossy conversion is surfaced, never silent" — stands
unchanged. Per the repo convention ADR-0018 §Context itself cites, the earlier
decision is left intact rather than rewritten.

## The contract difference

| | Claude | Codex |
|---|---|---|
| `timeout` | 1.5s shared budget; a per-hook `timeout` raises it, up to 60s | **1s default, 3s maximum** — every other Codex event defaults to 600s |
| `matcher` | filters `reason`: `clear`, `resume`, `logout`, `prompt_input_exit`, `bypass_permissions_disabled`, `other` | a **regex string**; `reason` is documented as always `other` |

## Decision

1. **`SessionEnd` is a supported Codex event.** It joins `_CODEX_EVENTS`.
   `Notification` remains genuinely absent from Codex's documented event list
   and is still dropped with a warning, so ADR-0018 §2's drop rule survives —
   with a one-event list rather than two.

2. **Per-event contract translation is now part of the Codex path.** Codex
   fan-out is "near-identical" *plus* a narrow, per-event normalization step
   (`_clamp_codex_session_end`). Today `SessionEnd` is its only member;
   the extension point exists because the runtimes have started diverging on
   per-event semantics rather than only on event names.

3. **Over-limit timeouts are clamped, not dropped.** A canonical
   `timeout: 30` is legal on Claude and out of contract on Codex. The Codex
   copy is clamped to 3s with a warning. Clamping is the lossier-but-live
   option and it is chosen deliberately: a hook that runs with less time still
   runs, while a dropped hook does nothing at all.

4. **Matcher filtering is a deny-list of five literals, not an allow-list.**
   Codex's `matcher` is a regex string, so `^other$`, `.*`, `*`, and
   `other|clear` all fire there. Only the five Claude-only `reason` literals
   (`clear`, `resume`, `logout`, `prompt_input_exit`,
   `bypass_permissions_disabled`) cannot match `other`, and only those are
   dropped, with a warning.

   An allow-list of `{"", "other"}` was implemented first and rejected in
   review: it dropped every regex above under a "could never fire" warning
   that was simply false, and it would have kept doing so for any `reason`
   Codex adds later. **When a translation cannot decide, it passes the rule
   through** — a hook that fires more often than intended is recoverable; one
   silently deleted in translation is not.

5. **A non-string `matcher` is dropped with a warning.** Codex's field is a
   regex *string*; a list or dict reaches the matcher-keyed additive merge as
   an unhashable key. An **absent** `matcher` is valid and means match-all —
   only a present non-string is malformed.

6. **Translations are Codex-local.** The canonical record and every other
   runtime keep the author's original values. A clamp or drop is a statement
   about one runtime's contract, never an edit of the user's intent.

## Consequences

- ADR-0018's claim that Codex fan-out is a pure pass-through no longer holds;
  `context/settings.py` is the source of truth, and `_CODEX_SESSION_END_*`
  sits next to `_CODEX_EVENTS` as a table to extend as runtimes diverge.
- Two warnings can now fire for one event, and the emitted text names the
  runtime whose contract caused it — ADR-0018 §5's "surfaced, never silent"
  rule applies unchanged.
- Decision 4's bias (pass through when undecidable) means a canonical hook can
  reach Codex and fire on reasons the author did not intend, once Codex ships
  a second `reason`. That is accepted: the alternative costs working hooks
  today for a hypothetical tomorrow.

## Known adjacent defect (not fixed here)

A non-string `matcher` crashes the **Gemini** generator before any Codex code
runs, in `_ensure_gemini_handler_names`' `re.sub`, for *any* event. That
predates this change and is out of scope; decision 5 is scoped to the Codex
generator, and the test pinning it calls that generator directly for exactly
this reason.

## Verification

Verified against the official docs on 2026-08-01:

- Codex — <https://learn.chatgpt.com/docs/hooks> (`SessionEnd` supported;
  `Notification` absent; timeout 1s default / 3s max for `SessionEnd`;
  "The matcher field is a regex string"; match-all is `*`, `""`, or omitted;
  "For now, `reason` is always `other`")
- Claude — <https://code.claude.com/docs/en/hooks> (`SessionEnd` `reason`
  values; per-hook `timeout` raises the 1.5s budget up to 60s)

## References

- ADR-0018 — multi-runtime hook fan-out (this ADR supersedes its §2 in part).
- ADR-0010 — settings hooks target scope.
- #1976 — the dropped-`SessionEnd` report; #1975 — the link refresh whose
  review surfaced it.
- `packages/memtomem/src/memtomem/context/settings.py` —
  `_CODEX_EVENTS`, `_CODEX_SESSION_END_MAX_TIMEOUT`,
  `_CODEX_SESSION_END_DEAD_MATCHERS`, `_clamp_codex_session_end`;
  `tests/test_context_settings_multiruntime.py` — the conversion pins.
