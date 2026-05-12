# Security Policy

## Reporting Vulnerabilities

Please report security issues via [GitHub private advisory](https://github.com/memtomem/memtomem/security/advisories/new) or email **contact@dapada.co.kr**. Do NOT open public issues for vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

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
   `app.state.csrf_token`. The SPA fetches it via `GET /api/session` on
   load; the token rotates on every restart and is never persisted.
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
