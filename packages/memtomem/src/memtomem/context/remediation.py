"""Per-surface action hints for context refusals (#1869).

The engine states the **condition** ("the Store already has agents/foo"); each
surface states its own **remediation** ("pass --overwrite" / "re-call with
overwrite=True" / the Overwrite checkbox). Before this module the engine spelled
CLI flags into the reason itself, so an MCP client was told to ``pass --from``
— a flag it cannot pass (its parameter is ``from_runtime``) — and the web Pull
picker rendered the same CLI wording verbatim.

Neutral-text-only was considered and rejected (#1869): it would silently
downgrade the CLI, which today hands the user a copy-pasteable flag.

Two rules keep this from rotting:

* **The engine never names a surface's vocabulary.** Enforced by
  ``tests/test_context_refusal_neutrality.py``, which sweeps the whole
  ``context`` package for flag spellings inside refusal strings — a bug-shape
  sweep, not an enumeration of today's sites.
* **An unknown code yields no hint, never a wrong one.** Every lookup here
  fails open to ``""``, so the surface renders the neutral condition alone. A
  future skip code degrades to today's baseline rather than inheriting a
  remediation that does not apply to it.

The web column is deliberately empty on this side: the browser already maps
``reason_code`` → localized copy (``_ctxImportSkipText`` in
``web/static/context-gateway-core.js`` over the ``settings.ctx.import_skip_*``
/ ``settings.ctx.pull_hint_*`` keys), and an English clause baked into the JSON
payload would bypass i18n. ``action_hint(code, "web")`` returning ``""`` is the
contract, not an omission.
"""

from __future__ import annotations

from typing import Literal, cast

from memtomem.context._skip_reasons import SkipCode

#: Which surface's vocabulary to render remediation in.
HintSurface = Literal["cli", "mcp", "web"]

#: Keys are ``reason_code`` values, which every refusal already carries all the
#: way to the surface. That is the whole addressing scheme: no refusal here is
#: identified by anything a caller has to remember to pass.
#:
#: The Gate A ``project_shared`` hard-abort is deliberately ABSENT even though
#: it is a refusal. Its only conceivable remediation was "retry in another
#: tier", and there is no such tier: ``project_local`` has no runtime fan-out
#: (ADR-0011 §3) and ``user`` resolves its runtime sources from ``$HOME``,
#: ignoring ``project_root`` (``_runtime_targets.runtime_fanout_root``) — so it
#: reads a DIFFERENT copy, not the one that was blocked. The honest remediation
#: is "remove the secret", which is identical on every surface and therefore
#: belongs in the neutral text (Codex review, round 2).
HintKey = SkipCode

#: ``(key, surface) → remediation clause``. Only conditions the user can
#: actually act on appear; ``write_failed`` / ``plan_stale`` and friends carry
#: their remediation ("re-run") in the neutral text because it is identical on
#: every surface.
#:
#: Every clause is a COMPLETE sentence, so it appends cleanly after any reason
#: without the caller re-punctuating (the engine's reasons vary between
#: colon-lists, dashes and plain prose). ``append_hint`` owns the join.
_HINTS: dict[HintKey, dict[HintSurface, str]] = {
    "canonical_exists": {
        "cli": "Pass --overwrite to replace it.",
        "mcp": "Re-call with overwrite=True to replace it.",
        "web": "",  # settings.ctx.pull_hint_canonical_exists
    },
    "source_conflict": {
        "cli": "Pass --from <runtime> to choose a source.",
        "mcp": 'Re-call with from_runtime="<runtime>" to choose a source.',
        "web": "",  # settings.ctx.pull_hint_source_conflict
    },
    "privacy_blocked": {
        "cli": "Pass --force-unsafe-import to bypass after review.",
        "mcp": "Re-call with force_unsafe_import=True to bypass for a reviewed "
        "false positive (user tier only).",
        "web": "",  # settings.ctx.pull_hint_privacy_blocked
    },
}


def action_hint(code: str | None, surface: HintSurface) -> str:
    """The remediation clause for *code* in *surface*'s vocabulary, else ``""``.

    ``code`` is deliberately typed ``str | None``: callers hold a ``reason_code``
    that already crossed a result dataclass (and may be ``None`` for rows that
    predate typed codes), and an unmapped value must degrade to the neutral
    reason rather than raise inside an error path.
    """
    if code is None:
        return ""
    # ``cast`` only to satisfy the keyed-dict lookup: an unmapped string lands
    # on the ``{}`` default, which is exactly the fail-open behavior above.
    return _HINTS.get(cast("HintKey", code), {}).get(surface, "")


def append_hint(reason: str, code: str | None, surface: HintSurface) -> str:
    """*reason* with *surface*'s remediation appended, or *reason* unchanged.

    The single joining site, so CLI skip lines, CLI exceptions and MCP result
    lines cannot render the same hint three different ways. A reason that does
    not already end in sentence punctuation gets a period first — engine skip
    reasons are terse fragments ("canonical exists") as often as sentences.
    """
    hint = action_hint(code, surface)
    if not hint:
        return reason
    body = reason.rstrip()
    if body and body[-1] not in ".!?:":
        body += "."
    return f"{body} {hint}"
