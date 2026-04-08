# Security Policy

## Reporting Vulnerabilities

Please report security issues via GitHub private advisory. Do NOT open public issues for vulnerabilities.

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
