"""Neutral engine-reason / error-message redactors for the MCP context tools.

The web wire boundary already display-sanitizes raw engine exception text
before it leaves the loopback dashboard:

* ``web/routes/_errors._redact_message`` — collapse ``$HOME`` → ``~``, drop
  secret-shape messages whole, then truncate to 200 chars.
* ``web/routes/context_gateway.sanitize_diff_reason`` — strip the project root
  (both the given and the ``.resolve()``'d form) wherever it appears inside the
  message, then apply ``_redact_message``.

The MCP context tools in ``server/tools/context.py`` are a *second* wire
boundary for the SAME engine reasons: their string results flow into the
calling agent's transcript and on to the model provider / any telemetry, so an
absolute host path leaks ``$HOME`` (and the OS username) just as it would on
the dashboard. The MCP layer cannot import from ``memtomem.web.*`` (the web
depends on the server tools' package, not the reverse, and MCP↔web coupling is
disallowed), so these functions mirror the web contract in a neutral
``memtomem.context`` leaf both layers can reach.

The web twins now delegate their absolute-path backstop here
(``context_gateway.redact_wire_reason`` calls :func:`scrub_absolute_paths`),
so that half is genuinely shared rather than kept in lock-step by hand. The
root-stripping half still has a web copy (``sanitize_diff_reason``) because the
signatures differ.

**The two layers do NOT agree about relative remainders, and that is
deliberate.** Web scrubs anything path-shaped, including the remainder left
after stripping a root; MCP keeps that remainder, because on this surface it is
the remediation — ``blocked foo: privacy hits in .claude/agents/foo.md`` names
the file to go fix. Hence two scrubs, not one: :func:`scrub_absolute_paths` for
the web posture and :func:`scrub_residual_absolute_paths` for this one. The
remediation-critical ``privacy block: …`` message is deliberately NOT routed
through here — it round-trips the full canonical path so the agent can act on
it (``test_sync_privacy_block_surfaces``), matching the web privacy 422 that
keeps its own fixed, path-free detail.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from memtomem.privacy import scan as _privacy_scan

# Frozen at import time (cross-platform #1506 discipline): a process that
# rewrites ``$HOME`` after startup keeps the original collapse anchor, matching
# the ``web/routes/_errors`` constant this mirrors.
_HOME = str(Path.home())
_ERROR_MESSAGE_LIMIT = 200
# Fixed display marker emitted after redaction; it is not authentication material.
_SECRET_REDACTED_MARKER = "<redacted: secret-shape>"  # nosec B105

# Residual absolute-path backstop, shared with the web (which imports
# :func:`scrub_absolute_paths` rather than keeping its own copy of this).
# Root-stripping + the ``$HOME`` collapse only cover paths under a root we know
# about; an engine ``OSError`` can still name a path under neither (a runtime
# dir symlinked to
# ``/Volumes/shared/…``, a frozen-``_HOME`` mismatch). Two-or-more segments so
# ordinary prose with a slash is left alone. Spaces are INCLUDED in a segment so
# a mount like ``/Volumes/My Drive/x`` is scrubbed whole rather than leaving
# ``Drive/x`` behind (the reason this is not ``[\w.\-]``).
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][^/\\'\"\n]+){2,}")
# Same run, but only where it actually STARTS a path — see
# :func:`scrub_residual_absolute_paths`. The lookbehind class is "characters a
# path segment can end with", so ``~/x/y`` and ``.claude/agents/foo.md`` are
# left intact while ``/Volumes/Shared/x`` after any boundary is not.
#
# ``\w``, not ``[A-Za-z0-9_]``: Python's ``\w`` is Unicode-aware by default, and
# an ASCII-only class treats a non-ASCII segment as a boundary — ``자료/x/y``
# scrubbed to ``자료<path>``, destroying a relative remainder this function
# exists to preserve (PR review). Filenames here come from user directories.
_RESIDUAL_ABS_PATH_RE = re.compile(r"(?<![\w.\-~])(?:[A-Za-z]:)?(?:[/\\][^/\\'\"\n]+){2,}")

# A single path segment is lexically indistinguishable from slash-bearing prose
# (``read/write``, a slash command, a URL host). Keep the established broad
# two-segment patterns above and add only the contexts engine errors actually
# produce: a filename enclosed in matching quotes, or a bare terminal path that
# is the whole message / the final value after ``: ``. Quoted segments may
# contain spaces; bare ones may not, so they cannot swallow trailing prose.
_QUOTED_SINGLE_SEGMENT = r"[^/\\'\"\n]+"
_TERMINAL_SINGLE_SEGMENT = r"[^\s/\\'\"\n]+"
# ``OSError`` filename rendering escapes a Windows separator as ``\\`` in the
# displayed message, while custom engine reasons commonly contain the native
# single separator. Accept both textual shapes after an explicit drive letter.
_WINDOWS_TEXT_SEPARATOR = r"(?:/|\\{1,2})"
_QUOTED_SINGLE_ABS_PATH_RE = re.compile(
    rf"(?P<quote>['\"])(?:/{_QUOTED_SINGLE_SEGMENT}/?|"
    rf"[A-Za-z]:{_WINDOWS_TEXT_SEPARATOR}{_QUOTED_SINGLE_SEGMENT}"
    rf"(?:{_WINDOWS_TEXT_SEPARATOR})?)(?P=quote)"
)
_TERMINAL_SINGLE_ABS_PATH_RE = re.compile(
    rf"(?P<prefix>^|:[ \t]+)(?:/{_TERMINAL_SINGLE_SEGMENT}/?|"
    rf"[A-Za-z]:{_WINDOWS_TEXT_SEPARATOR}{_TERMINAL_SINGLE_SEGMENT}"
    rf"(?:{_WINDOWS_TEXT_SEPARATOR})?)"
    rf"(?P<trailing>[ \t]*)\Z"
)
_PATH_REDACTED_MARKER = "<path>"


def _scrub_single_segment_absolute_paths(message: str) -> str:
    """Scrub conservatively delimited one-segment POSIX/drive-root paths."""
    quoted = _QUOTED_SINGLE_ABS_PATH_RE.sub(rf"\g<quote>{_PATH_REDACTED_MARKER}\g<quote>", message)
    return _TERMINAL_SINGLE_ABS_PATH_RE.sub(
        rf"\g<prefix>{_PATH_REDACTED_MARKER}\g<trailing>", quoted
    )


def redact_message(message: str) -> str:
    """Collapse ``$HOME`` → ``~``, drop secret-shape messages, then truncate.

    Mirror of ``web/routes/_errors._redact_message``: a catch-all / ``OSError``
    ``str(exc)`` may incidentally carry provider tokens or ``api_key=…``
    fragments pulled from a config parse or a third-party library, so
    truncation alone is not enough at this trust boundary. We reuse the LTM
    secret-class scanner; any hit replaces the *whole* message with a fixed
    marker (span-splicing was rejected because assignment-anchored patterns
    like ``api_key=…`` would leave the secret value behind).
    """
    redacted = message.replace(_HOME, "~") if _HOME else message
    if _privacy_scan(redacted):
        return _SECRET_REDACTED_MARKER
    if len(redacted) > _ERROR_MESSAGE_LIMIT:
        redacted = redacted[:_ERROR_MESSAGE_LIMIT]
    return redacted


def redact_engine_reason(message: str | None, *project_roots: Path) -> str | None:
    """Display-sanitize a raw engine reason / error string for the MCP wire.

    Mirror of ``web/routes/context_gateway.sanitize_diff_reason`` generalized to
    accept more than one root (a transfer straddles a source and a destination
    project). Engine reasons embed absolute source paths inside arbitrary
    message text, so ``Path.relative_to`` doesn't apply: strip each root prefix
    — both the given form and its ``.resolve()``'d form (macOS ``/tmp`` →
    ``/private/tmp``, a symlinked home, a case-variant mount) — wherever it
    appears, longest-first so a root that contains another as a prefix can't be
    half-stripped, then apply :func:`redact_message`.

    Returns ``None`` for an empty/absent message so callers can keep their
    ``if reason`` truthiness checks.
    """
    if not message:
        return None
    roots: set[str] = set()
    for project_root in project_roots:
        roots.add(str(project_root))
        try:
            roots.add(str(project_root.resolve()))
        except (AttributeError, OSError):
            pass  # PurePath / unresolvable root — the bare form still strips
    cleaned = message
    for root in sorted(roots, key=len, reverse=True):
        cleaned = cleaned.replace(root + os.sep, "").replace(root, ".")
    return redact_message(cleaned)


def scrub_absolute_paths(message: str) -> str:
    """Replace any residual absolute path with ``<path>`` (defense in depth).

    Neutral twin of the web ``context_gateway._redact_pull_reason`` backstop.
    :func:`redact_engine_reason` only strips roots it was handed plus the
    import-frozen ``$HOME``; a path under neither still reaches the wire. Pull
    surfaces run engine reasons through this afterwards so an unreadable
    runtime dir outside every known root cannot disclose its location to the
    calling agent's transcript.
    """
    scrubbed = _ABS_PATH_RE.sub(_PATH_REDACTED_MARKER, message)
    return _scrub_single_segment_absolute_paths(scrubbed)


def scrub_residual_absolute_paths(message: str) -> str:
    """Replace a still-ABSOLUTE path with ``<path>``, keeping relative ones.

    The MCP posture, and the reason there are two scrubs in this module.
    :func:`scrub_absolute_paths` eats anything path-shaped, which is correct for
    the web — there the remainder is disclosure, and
    ``test_context_status_global.py::test_route_redacts_error_reason`` pins that
    it must go. On this surface the remainder is the *remediation*: a row
    reading ``privacy hits in .claude/agents/foo.md`` names the file to go fix,
    and ``test_server_tools_context_redaction.py::
    test_init_privacy_blocked_skip_keeps_relative_remainder`` pins that it must
    stay. Success-path formatters likewise render ``~``-collapsed paths as
    intended output.

    So the leading lookbehind: a separator that CONTINUES a name is mid-path,
    never the start of an absolute one, which is exactly what root stripping
    and ``$HOME`` collapse leave behind (``.claude/…``, ``~/…``). Anything
    still starting at a separator after a boundary was under no root we knew
    about — a runtime dir symlinked onto a shared volume, a frozen-``_HOME``
    mismatch — and is pure disclosure. It cannot weaken the scrub relative to
    doing nothing, which is what this surface did before.
    """
    scrubbed = _RESIDUAL_ABS_PATH_RE.sub(_PATH_REDACTED_MARKER, message)
    return _scrub_single_segment_absolute_paths(scrubbed)
