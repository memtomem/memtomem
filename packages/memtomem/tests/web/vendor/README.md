# Vendored test-only third-party JavaScript

Files here are **only** loaded by Playwright tests (`page.add_script_tag`).
They are not shipped with the web UI and are not imported by application code.

## axe.min.js — axe-core 4.10.2

- Upstream: <https://github.com/dequelabs/axe-core>
- Version pinned: 4.10.2 (sha256 in this directory should match
  `https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js`)
- License: **Mozilla Public License 2.0** — full text at
  <https://github.com/dequelabs/axe-core/blob/v4.10.2/LICENSE>
- Used by: `packages/memtomem/tests/web/test_a11y_*.py` for issue #1053
  (A11Y-1/2/3 regression pins)

Per MPL-2.0 §3.1, the source form is available from the upstream repository
above and the file is distributed without modification.

To refresh:

```bash
curl -fsSL https://cdnjs.cloudflare.com/ajax/libs/axe-core/<version>/axe.min.js \
  -o packages/memtomem/tests/web/vendor/axe.min.js
```

and bump the version in this README.
