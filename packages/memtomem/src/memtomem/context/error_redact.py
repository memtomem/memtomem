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

These are kept in lock-step with the two web twins named above; a later PR can
collapse the duplication by having those web functions delegate here. The
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

# Residual absolute-path backstop — the neutral twin of
# ``web/routes/context_gateway._ABS_PATH_RE``. Root-stripping + the ``$HOME``
# collapse only cover paths under a root we know about; an engine ``OSError``
# can still name a path under neither (a runtime dir symlinked to
# ``/Volumes/shared/…``, a frozen-``_HOME`` mismatch). Two-or-more segments so
# ordinary prose with a slash is left alone. Spaces are INCLUDED in a segment so
# a mount like ``/Volumes/My Drive/x`` is scrubbed whole rather than leaving
# ``Drive/x`` behind (the reason this is not ``[\w.\-]``).
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][^/\\'\"\n]+){2,}")
_PATH_REDACTED_MARKER = "<path>"


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
    return _ABS_PATH_RE.sub(_PATH_REDACTED_MARKER, message)
