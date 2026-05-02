# `web/static/vendor/` — third-party browser assets

This directory holds version-pinned copies of the third-party JavaScript
and CSS libraries that `mm web` depends on:

- **SPA assets** (DOMPurify, marked, Prism + 5 language plugins): served
  directly from `web/static/vendor/`.
- **Swagger UI bundle** (`swagger/swagger-ui-bundle.js`,
  `swagger/swagger-ui.css`): served from `web/static/vendor/swagger/`
  and pulled in by the custom `/api/docs` route registered in
  `memtomem.web.app.create_app`.

Vendoring keeps both surfaces functional offline / behind firewalls / in
air-gapped deployments, and lets the FastAPI Content-Security-Policy stay
on `script-src 'self'` instead of allow-listing `cdnjs.cloudflare.com`
or `cdn.jsdelivr.net`.

See `THIRD_PARTY_LICENSES.md` (alongside this file) for the version pin
table, source URLs, SHA-256 hashes, and full upstream license texts.

## Updating a pinned version

1. Update the `curl` URL list below to the new version and re-run the
   block. cdnjs / jsdelivr are the canonical fetch sources — do not pull
   from arbitrary mirrors.

   ```bash
   cd packages/memtomem/src/memtomem/web/static/vendor

   # SPA assets — bump versions here, then run:
   curl -sSfL -o prism-tomorrow.min.css   https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css
   curl -sSfL -o purify.min.js            https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js
   curl -sSfL -o marked.min.js            https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js
   curl -sSfL -o prism.min.js             https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js
   curl -sSfL -o prism-python.min.js      https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-python.min.js
   curl -sSfL -o prism-typescript.min.js  https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-typescript.min.js
   curl -sSfL -o prism-json.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-json.min.js
   curl -sSfL -o prism-bash.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-bash.min.js
   curl -sSfL -o prism-yaml.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-yaml.min.js

   # Swagger UI — pin the same version across all three files:
   curl -sSfL -o swagger/swagger-ui-bundle.js                  https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.32.5/swagger-ui-bundle.js
   curl -sSfL -o swagger/swagger-ui.css                        https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.32.5/swagger-ui.css
   curl -sSfL -o swagger/swagger-ui-bundle.js.LICENSE.txt      https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.32.5/swagger-ui-bundle.js.LICENSE.txt

   shasum -a 256 *.js *.css swagger/*.js swagger/*.css
   ```

   **Supply-chain check.** If you are *not* bumping a version (e.g. you
   are re-running the curl block to verify reproducibility), the new
   `shasum` output **MUST** match the SHA-256 column in
   `THIRD_PARTY_LICENSES.md` byte-for-byte. A mismatch on a same-version
   re-fetch is a supply-chain red flag (cdnjs serving a different build
   under the same version path) — do not commit. Investigate upstream
   first, then file an issue before any update.

2. Replace the upstream LICENSE files in this directory if the new
   version's LICENSE has changed:

   ```bash
   curl -sSfL https://raw.githubusercontent.com/markedjs/marked/v<NEW>/LICENSE.md       -o marked-LICENSE.md
   curl -sSfL https://raw.githubusercontent.com/PrismJS/prism/v<NEW>/LICENSE            -o prism-LICENSE.txt
   curl -sSfL https://raw.githubusercontent.com/cure53/DOMPurify/<NEW>/LICENSE          -o dompurify-LICENSE.txt
   curl -sSfL https://raw.githubusercontent.com/swagger-api/swagger-ui/v<NEW>/LICENSE   -o swagger/swagger-ui-LICENSE
   ```

3. Update `THIRD_PARTY_LICENSES.md` — bump the version column, replace
   the SHA-256, and update the `Source` link's tag.

4. Bump `?v=N` on the matching `<script>` / `<link>` references so users
   get the new bytes past their disk cache. The references live in:
   - `packages/memtomem/src/memtomem/web/static/index.html` (SPA assets)
   - `packages/memtomem/src/memtomem/web/app.py` (Swagger UI URLs passed
     into `get_swagger_ui_html`)

   See `feedback_static_asset_cache_bust.md`.

5. Smoke-test in a browser:
   - Open `mm web`, render a markdown chunk and a syntax-highlighted
     code block, confirm no console errors and no CSP violations.
   - Open `<host>:<port>/api/docs`, confirm the Swagger UI renders, the
     "Try it out" buttons work, and DevTools shows no requests to
     `cdn.jsdelivr.net` / `cdnjs.cloudflare.com`.

## Why vendor instead of an `npm`/build step

The `memtomem` package has no JavaScript build pipeline — the SPA ships
raw `.js` and `.css` files, and `mm web` is a pure-Python install. Adding
`npm install` purely to pin a handful of browser libraries would force
every contributor (and `uv tool install` user via sdist) to also have
Node available. Direct vendoring of the minified CDN builds keeps the
install surface Python-only.
