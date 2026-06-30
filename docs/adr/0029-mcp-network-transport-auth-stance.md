# ADR-0029: Network MCP transport auth stance — docs + bind-time banner, no first-party token

**Status:** Accepted
**Date:** 2026-06-30
**Context:** A security review asked whether the network MCP transports
(`memtomem-server --transport sse|http`) should gain an optional first-party
authentication token, or stay documented as trusted-network-only behind an
authenticated reverse proxy. This ADR records the decision (no first-party
token; harden docs + the bind-time banner instead), the rejected static-token
option and *why* it is rejected, and the triggers that would revisit it.
Tracking issue: [#1485](https://github.com/memtomem/memtomem/issues/1485).

## Background — current state (verified against source)

There is no first-party bearer / API-key authentication for the `sse` / `http`
transports. The MCP SDK's transport-security middleware validates only
`Host` / `Origin` (DNS-rebinding defense), not identity. Existing protections,
all in `server/__init__.py`:

- **Defaults are safe.** `--transport` defaults to `stdio` and `--host`
  defaults to `127.0.0.1` (`_parse_server_args`), so a network socket is
  opt-in and loopback-bound unless the operator changes both.
- **DNS-rebinding protection is on by default.** `_configure_network_transport`
  seeds `TransportSecuritySettings(enable_dns_rebinding_protection=True, ...)`
  with loopback `Host`/`Origin` allow-lists, extended by `--url` /
  `--allowed-host` / `--allowed-origin`. Under a bare `--host 0.0.0.0` the
  wildcard yields no host patterns (`_host_patterns` returns `[]`), so an
  external `Host:` is rejected (HTTP 421).
- **Turning the check off is explicitly dangerous.**
  `--disable-dns-rebinding-protection` is labelled "Advanced/dangerous …
  only safe behind an authenticated reverse proxy".
- **The posture is already documented at `--help`.** The argparse epilog
  states the `sse`/`http` transports are trusted-network-only and want an
  authenticated reverse proxy before public exposure.
- **`Host` matching is exact unless an allow-list entry ends in `:*`**
  (`_host_patterns` emits both `host` and `host:*`; SDK
  `transport_security.py`).

So reaching the endpoint from untrusted clients takes explicit,
documented-as-dangerous operator action. **Once** reachable, the absence of
auth means full LTM read plus file-touching tool access.

## Decision

**memtomem ships no first-party MCP transport authentication.** `stdio` is the
supported, locally-trusted transport; the network transports (`sse` / `http`)
are a trusted-LAN / authenticated-reverse-proxy opt-in. No change to transport
defaults. We harden the *posture* — docs, a bind-time banner, and this ADR —
rather than graft a bypassable identity layer onto the transport.

This keeps the trust boundary where the project has always put it: at
write / ingress (`privacy.enforce_write_guard`), per ADR-0011 §5 ("hard
refusal at the chokepoint") and ADR-0027 §8's valve-vs-gate stance — not at
the transport. Default-on DNS-rebinding protection already gives the network
transport the same "not exposed by accident" guarantee that
`--allow-remote-ui` gives the `mm web` SPA (ADR-0006).

Concretely this decision lands as four changes (this is "docs + a small
banner + ADR", not strictly docs-only):

1. An authenticated-proxy recipe (nginx TLS + HTTP Basic, proxying to the
   loopback listener, paired with `--url`) in `docs/guides/mcp-clients.md`.
2. A `Security: no first-party authentication …` line in
   `_print_network_server_info` (`server/__init__.py`) that fires at bind
   time for every network transport, mirroring the `--help` epilog.
3. An "MCP server transports" section in `SECURITY.md` (whose
   network-exposure guidance previously covered only the `mm web` SPA).
4. This ADR.

## Why not an optional static bearer token

A shared, off-by-default static token was the obvious "defense-in-depth"
counter-proposal. We reject it on four grounds:

1. **It satisfies neither side of the MCP spec's transport split.** The MCP
   authorization spec makes transport auth *optional*, but splits how it
   should be done when present: `stdio` transports SHOULD NOT authenticate
   (they derive identity from the environment — our compliant default), while
   HTTP-based transports that authenticate SHOULD conform to its OAuth 2.1
   profile (PKCE, RFC 9728 Protected Resource Metadata, RFC 8707 resource
   indicators). A shared static token is neither: it is not the OAuth 2.1
   profile the HTTP guidance points to, and it has no place on the stdio side
   the spec wants left unauthenticated.
2. **The SDK makes it actively costly.** In `mcp` 1.27.2, FastMCP raises
   `ValueError("Cannot specify auth_server_provider or token_verifier without
   auth settings")` — so a `token_verifier` requires `AuthSettings`, whose
   **required** fields are `issuer_url` and `resource_server_url`
   (`mcp/server/auth/settings.py`). Supplying them makes the server advertise
   `/.well-known/oauth-protected-resource` (`mcp/server/auth/routes.py`)
   pointing at an authorization server that does not exist. Those auth
   branches are `# pragma: no cover` upstream — i.e. an unexercised path we
   would be the first to lean on.
3. **It does not remove the reverse-proxy requirement.** A plaintext token
   over `--transport http` is LAN-sniffable and still needs the proxy for
   TLS — so it adds maintenance and a footgun without removing the layer that
   actually secures the deployment.
4. **A shared bearer has no identity, revocation, or expiry.** One token for
   all clients means a leak is full read + write access until a manual rotate
   + restart — strictly worse than delegating auth to a proxy that already
   does per-client credentials, TLS, and revocation.

## Consequences

- **No defense-in-depth is added.** This is a posture, not a code-level
  barrier. The residual risk is **accepted**: one operator misstep (network
  transport + `Host`/`Origin` widening or disabled DNS-rebinding, with an
  untrusted client on the path) is unauthenticated full read + write. We rate
  it Medium — off-by-default, misconfiguration-gated — and lean on the
  defaults, the docs, and the bind-time banner to lower the misconfiguration
  probability.
- **The bind-time banner is now a contract.** `_print_network_server_info`
  must keep emitting the no-first-party-auth line for every network bind; a
  regression test in `test_server_cli.py` pins it.
- **Docs are the primary control.** `SECURITY.md` and `mcp-clients.md` carry
  the authenticated-proxy recipe; drift between them and the flag behaviour is
  a docs-accuracy concern (see the docs-as-tests guards).

## Revisit triggers

Per the ADR-0006 `--allow-remote-ui` precedent, the stance is revisited on:

- a concrete remote / multi-tenant requirement (memtomem needs to serve
  network MCP clients it does not control), **or**
- MCP-client OAuth 2.1 support becoming table-stakes among the editors we
  document.

At either trigger the answer is **full OAuth 2.1 resource-server support**
(RFC 9728 PRM + RFC 8707, delegating to a real authorization server) — never
a static token. A tracker row records this deferral; #1485 is the closing
record for the docs/banner work.

## Alternatives considered

- **Optional static bearer / API-key token, off by default** — rejected for
  the four reasons above (spec non-conformance, SDK OAuth-metadata footgun,
  does not remove the proxy requirement, no identity/revocation/expiry).
- **Full OAuth 2.1 resource-server support now** — deferred, not rejected.
  Correct end-state, but unjustified before a concrete remote/multi-tenant
  requirement exists; it is the explicit answer at the revisit triggers above.
- **Docs-only (no banner)** — rejected as too weak: the `--help` epilog is
  easy to skip, so the warning must also fire at bind time, where the operator
  is actually exposing the port.

## References

- Issue [#1485](https://github.com/memtomem/memtomem/issues/1485) — decision
  thread and the maintainer comment this ADR records.
- ADR-0006 — `--allow-remote-ui` bind-gate precedent and revisit-trigger
  pattern.
- ADR-0011 §5 — privacy gates layered; trust boundary at the write chokepoint.
- ADR-0027 §8 — valve-vs-gate posture vocabulary.
- `server/__init__.py` — `_parse_server_args` (defaults, epilog),
  `_configure_network_transport` (DNS-rebinding allow-lists),
  `_print_network_server_info` (bind-time banner).
- MCP authorization specification — transport auth is optional; the split is
  `stdio` SHOULD NOT authenticate / HTTP-based transports SHOULD use the
  OAuth 2.1 profile (RFC 9728 PRM, RFC 8707, PKCE).
