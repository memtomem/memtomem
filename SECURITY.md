# Security Policy

## Reporting Vulnerabilities

Please report security issues via [GitHub private advisory](https://github.com/memtomem/memtomem/security/advisories/new) or email **contact@dapada.co.kr**. Do NOT open public issues for vulnerabilities. We aim to acknowledge security reports within 2 business days.

## Supported Versions

memtomem is alpha (`0.x`); only the latest published minor receives security fixes.

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| < 0.3   | No        |

## Security Measures

### Web UI

- **XSS prevention**: All markdown rendering uses DOMPurify sanitization
- **Content Security Policy**: Strict CSP header limits script/style sources to self + cdnjs
- **Frame protection**: `X-Frame-Options: DENY` prevents clickjacking
- **CORS**: Restricted to localhost origins only
- **Path traversal protection**: All file access endpoints validate against indexed sources; symlinked files are rejected
- **Error masking**: Filesystem paths are stripped from error responses

#### CSRF / Origin / Host guard (RFC #787)

`mm web` ships an unauthenticated SPA on the loopback interface. CORS is a
browser readability boundary, not a write boundary — a malicious tab can
issue CORS-simple `POST` requests against `http://127.0.0.1:<port>` and the
handler still runs even if the response is unreadable. To close that gap,
every unsafe-method request to `/api/**` (POST / PUT / PATCH / DELETE) is
gated by a single middleware that checks three things at once:

1. **CSRF token** — `X-Memtomem-CSRF` must match the per-process token in
   `app.state.csrf_token`. The SPA fetches it via `GET /api/session`
   lazily on the first unsafe-method request (cached for the page
   lifetime); the token rotates on every restart and is never persisted.
2. **Origin / Referer** — when present, must resolve to a loopback host
   or an operator-supplied `--trusted-origin` entry. Defends drive-by
   tabs whose Origin reveals the attacker domain.
3. **Host header** — must be a loopback hostname or an operator-supplied
   `--trusted-host` entry. Defends DNS rebinding, where the socket peer
   is `127.0.0.1` but the browser's URL bar (and the `Host:` header) is
   attacker-controlled.

Failures return `403` with a JSON `{"detail": "..."}` body and a
structured `web.csrf.observe` log record for after-the-fact auditing.

**Operator surface:**

- `--allow-remote-ui` — required when `--host` is non-loopback. Startup
  refuses without it; the unauthenticated SPA must not be exposed to
  the network by accident.
- `--trusted-origin <host>` (repeatable) — extends the Origin/Referer
  allow-list. Pair with `--allow-remote-ui`.
- `--trusted-host <host>` (repeatable) — extends the Host-header
  allow-list. Pair with `--allow-remote-ui`.
- `MEMTOMEM_WEB__CSRF_ENFORCE` — emergency rollback to observe-only.
  Set to `0` / `false` / `no` / `off` to keep the structured log line
  but skip the 403. Any other value (including unset and typos) keeps
  enforcement on.

### MCP server transports

`memtomem-server` exposes the MCP protocol over one of two transport
families. They have different trust models:

- **`stdio` (default).** The MCP client spawns the server as a child
  process and talks to it over stdin/stdout. There is no network socket and
  no authentication — consistent with the MCP spec, which says `stdio`
  servers SHOULD NOT implement transport auth and SHOULD derive identity
  from the environment. This is the supported, locally-trusted transport.
- **Network (`--transport sse` / `http`).** Opt-in, off by default. These
  bind a TCP socket and ship **no first-party authentication**. memtomem
  deliberately does not add a static bearer/API-key token; the supported way
  to authenticate a network transport is a TLS-terminating, authenticating
  reverse proxy. The full rationale — including the rejected static-token
  option and its OAuth-metadata footgun, and the triggers that would revisit
  it — is recorded in
  [ADR-0029](docs/adr/0029-mcp-network-transport-auth-stance.md).

Defense-in-depth for the network transports:

- **Loopback by default.** `--host` defaults to `127.0.0.1`; reaching the
  port from another machine is an explicit operator action.
- **DNS-rebinding protection on by default.** Every request's `Host` and
  `Origin` headers are validated. The allow-lists are seeded
  asymmetrically: the `Host` list is loopback-seeded, while the `Origin` list
  is derived from `--url` (both extendable via `--allowed-host` /
  `--allowed-origin`). A `Host` mismatch returns HTTP 421, an `Origin`
  mismatch 403. Turning the check off requires the explicitly dangerous
  `--disable-dns-rebinding-protection`, documented as safe only behind an
  authenticated reverse proxy.
- **Bind-time warning.** Starting a network transport prints a
  `Security: no first-party authentication ...` banner, mirroring the
  `--help` epilog, so the no-auth posture is visible before exposure.

**Operator guidance:** never expose `sse`/`http` to an untrusted network
without an authenticating reverse proxy in front. A copy-paste nginx recipe
(TLS + HTTP Basic, proxying to the loopback listener) is in the
[MCP client guide → Authenticated reverse proxy](docs/guides/mcp-clients.md#authenticated-reverse-proxy-required-for-public-exposure).

**Residual risk (accepted):** this is a posture, not a code-level barrier.
One operator misstep — a network transport plus Host/Origin widening or
disabled DNS-rebinding protection, with an untrusted client on the path — is
unauthenticated full read + write tool access. We accept this as a Medium,
off-by-default, misconfiguration-gated risk and rely on the defaults, the
docs, and the bind-time banner to keep the misconfiguration probability low.

### URL Fetching (`mem_fetch`)

- **SSRF protection**: Private/reserved IP ranges blocked (10.x, 172.16-31.x, 192.168.x, 169.254.x, localhost, ::1)
- **Protocol restriction**: Only `http://` and `https://` allowed
- **Redirect validation**: Each redirect hop is validated against the same IP blocklist
- **Internal hostname blocking**: `.local`, `.internal` TLD hosts are rejected

### Data Security

- **SQL injection**: All queries use parameterized statements
- **No unsafe deserialization**: No pickle, no unsafe YAML loading
- **No command injection**: No subprocess/eval/exec with user input
- **Path validation**: CLI uses `Path.relative_to()` for directory containment checks

## Best Practices

- Never commit API keys or credentials
- Use MCP client `env` blocks for configuration
- Default storage is local SQLite — no network exposure
- Web UI binds to `127.0.0.1` by default — not publicly accessible
- Set `MEMTOMEM_TOOL_MODE=standard` to reduce tool surface area for AI agents
