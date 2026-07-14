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
- **Path traversal protection**: General file access endpoints validate against
  indexed sources and reject symlink escapes. The explicit one-shot indexing
  exception is documented below.
- **Error masking**: Filesystem paths are stripped from error responses

#### CSRF / Origin / Host guard (RFC #787)

`mm web` ships an unauthenticated SPA on the loopback interface. CORS is a
browser readability boundary, not a write boundary — a malicious tab can
issue CORS-simple `POST` requests against `http://127.0.0.1:<port>` and the
handler still runs even if the response is unreadable. DNS rebinding makes
an attacker page same-origin, so it can also *read* GET responses. To close
both gaps, a single middleware gates **every** request to `/api/**` — reads
included; only `OPTIONS` preflights (owned by the CORS middleware) are
skipped:

1. **Host header** — must be a loopback hostname or an operator-supplied
   `--trusted-host` entry. Checked on **every** `/api/**` request,
   including GET reads. Defends DNS rebinding, where the socket peer is
   `127.0.0.1` but the browser's URL bar (and the `Host:` header) is
   attacker-controlled — without this a rebound page could read
   `GET /api/export`.
2. **Origin / Referer** — when present, must resolve to a loopback host
   or an operator-supplied `--trusted-origin` entry. Checked on every
   `/api/**` request. Defends drive-by tabs whose Origin reveals the
   attacker domain.
3. **CSRF token** — `X-Memtomem-CSRF` must match the per-process token in
   `app.state.csrf_token`. Required for **unsafe** methods (POST / PUT /
   PATCH / DELETE) only. The SPA fetches it via `GET /api/session` lazily
   (cached for the page lifetime); the token rotates on every restart and
   is never persisted. `GET /api/session` is token-exempt but still
   Host/Origin-checked.

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

#### Explicit one-shot indexing trust boundary (0.3.11 sign-off)

The Folder Index flow deliberately accepts an operator-selected local file or
directory even when it is outside configured `memory_dirs`. This is a
single-user, local-machine capability: it indexes the selected content once
but does not register the path for watching or startup reindexing.

Security controls and limits:

- index and namespace-preview requests are `POST` operations protected by the
  per-process CSRF token;
- Host and Origin/Referer validation applies to all API methods;
- the server binds to loopback unless the operator explicitly enables remote
  UI access and configures trusted hosts/origins;
- only supported, indexable files are discovered, namespace preview is capped
  at 200 files, and indexed content passes the normal redaction guard;
- a one-shot request does not modify `memory_dirs` or expand the watcher.

**Accepted residual risk:** a process or browser context that already has
same-origin access and the live CSRF token can ask the locally running server
to read and index any file the memtomem process account can read, subject to
the indexability and redaction controls above. This matches the authority of a
local user running `mm index <path>` and is accepted for the 0.3.11 local SPA
threat model. Do not expose the unauthenticated Web UI to untrusted users.

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
- **Pre-write privacy guard**: Managed memory, import, fetch, upload, session-summary, and Context Gateway write paths scan content before persistence. Git-tracked `project_shared` writes cannot bypass a finding.
- **Restricted persistence**: Managed files use owner-only directories/files and atomic promotion. Web uploads are streamed into an owner-only disk quarantine, capped by file count and per-file/aggregate byte limits, and promoted only after the full batch passes multipart, filename/type, UTF-8, and privacy adjudication; accepted files are then indexed.
- **Historical audits**: `mm mem rescan --scope <tier>`, `mm mem rescan-files`, and `mm context rescan --scope <tier>` are read-only checks that exit `1` on findings. See [Operations → Privacy audits](docs/guides/reference/operations.md#privacy-audits).

## Best Practices

- Never commit API keys or credentials
- Use MCP client `env` blocks for configuration
- Default storage is local SQLite — no network exposure
- Web UI binds to `127.0.0.1` by default — not publicly accessible
- Set `MEMTOMEM_TOOL_MODE=standard` to reduce tool surface area for AI agents
