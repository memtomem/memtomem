# ADR-0017: Rename target_scope identifiers to target_tier

**Status:** Accepted
**Date:** 2026-05-12
**Context:** ADR-0016 accepted the "tier" vocabulary while deferring the
implementation-level `target_scope` -> `target_tier` rename. The deferred
question was tracked in #922 and ADR-0016's open questions. The project is
still early enough in the tiered Context Gateway rollout that the public
surface can move without a long-lived second vocabulary, provided existing
configs and clients keep working through a compatibility window.

## Decision

Use `target_tier` as the canonical identifier for the canonical-residency
tier axis.

The canonical tier values remain unchanged:

- `user`
- `project_shared`
- `project_local`

The rename applies to:

- `hooks.target_tier` in config objects and persisted `config.json`
- `?target_tier=` on Web context/source routes
- `target_tier` response fields on Web rows and overview payloads
- the Python type name `TargetTier`

`target_scope` stays as a deprecated compatibility alias:

- config loading accepts `hooks.target_scope`
- `mm config set/unset hooks.target_scope` maps to `hooks.target_tier`
- Web routes accept `?target_scope=`
- Web responses temporarily include both `target_tier` and `target_scope`
- `TargetScope` remains importable as an alias for `TargetTier`

This ADR does not rename the memory/search `scope` axis, CLI `--scope`
flags, MCP `scope=` arguments, or internal function parameters that pass the
tier value into existing helpers named `scope`. Those names cover broader
compatibility surfaces and are not part of the confusing `target_scope`
identifier called out by ADR-0016.

## Compatibility

The compatibility rule is one-way: new writes use `target_tier`; old reads
continue to work.

When a legacy config file contains:

```json
{"hooks": {"target_scope": "project_local"}}
```

loading resolves it as `hooks.target_tier == "project_local"`. The next
normal config save writes:

```json
{"hooks": {"target_tier": "project_local"}}
```

Web clients should send `?target_tier=` and read `target_tier`. Existing
clients using `?target_scope=` or reading `target_scope` keep working during
the compatibility window.

When a client sends **both** `?target_tier=` and the deprecated
`?target_scope=` on the same request, the canonical `target_tier` wins. The
deprecated alias is the fallback for callers that have not been renamed yet;
letting it pre-empt the canonical query would invert the rename. (The same
precedence rule applies symmetrically to env vars: `MEMTOMEM_HOOKS__TARGET_TIER`
wins over `MEMTOMEM_HOOKS__TARGET_SCOPE` when both are set, via Pydantic's
`AliasChoices` ordering.)

The compatibility window has no calendar deadline. Removal of the
`target_scope` aliases (env/persisted key, query param, response field,
`TargetScope` import) is tracked as a single deferred row in
`docs/adr/TRACKER.md` under ADR-0017, with the trigger criteria listed there.

## Consequences

- New code and docs have a single spelling for the tier axis.
- ADR-0015 and ADR-0016 remain historical records for why the old spelling
  existed, but their identifier guidance is superseded by this ADR.
- #922's decision question is answered by this ADR. The separate issue-body
  cleanup can remain a follow-up.

## Considered

- **No rename.** Rejected. ADR-0016 already made "tier" the conceptual term,
  and leaving new APIs on `target_scope` would keep the original ambiguity.
- **Hard break with no aliases.** Rejected. Config files, Web clients, and
  tests already use the old spelling; accepting aliases is low risk and makes
  the rollout reversible if a missed client appears.
- **Rename every `scope` parameter.** Rejected. Many `scope` names refer to
  memory visibility, project-root selection, or helper-local tier values.
  Broad churn would obscure the actual public rename and risk regressions.

## References

- ADR-0015: Context Gateway scope vocabulary
- ADR-0016: Three-tier canonical context store
- #922: target_scope -> target_tier decision tracker
