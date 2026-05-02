# `web/static/vendor/` — third-party browser assets

This directory holds version-pinned copies of the third-party JavaScript
and CSS libraries the `mm web` SPA depends on (DOMPurify, marked, Prism).
Vendoring keeps the Web UI functional offline / behind firewalls / in
air-gapped deployments, and lets the FastAPI Content-Security-Policy stay
on `script-src 'self'` instead of allow-listing `cdnjs.cloudflare.com`.

See `THIRD_PARTY_LICENSES.md` (alongside this file) for the version pin
table, source URLs, SHA-256 hashes, and full upstream license texts.

## Updating a pinned version

1. Update the `curl` URL list below to the new version and re-run the
   block. cdnjs is the canonical fetch source — do not pull from arbitrary
   mirrors.

   ```bash
   cd packages/memtomem/src/memtomem/web/static/vendor

   # bump versions here, then run:
   curl -sSfL -o prism-tomorrow.min.css   https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css
   curl -sSfL -o purify.min.js            https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js
   curl -sSfL -o marked.min.js            https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js
   curl -sSfL -o prism.min.js             https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js
   curl -sSfL -o prism-python.min.js      https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-python.min.js
   curl -sSfL -o prism-typescript.min.js  https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-typescript.min.js
   curl -sSfL -o prism-json.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-json.min.js
   curl -sSfL -o prism-bash.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-bash.min.js
   curl -sSfL -o prism-yaml.min.js        https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-yaml.min.js

   shasum -a 256 *.js *.css
   ```

2. Replace the upstream LICENSE files in this directory if the new
   version's LICENSE has changed:

   ```bash
   curl -sSfL https://raw.githubusercontent.com/markedjs/marked/v<NEW>/LICENSE.md -o marked-LICENSE.md
   curl -sSfL https://raw.githubusercontent.com/PrismJS/prism/v<NEW>/LICENSE     -o prism-LICENSE.txt
   curl -sSfL https://raw.githubusercontent.com/cure53/DOMPurify/<NEW>/LICENSE   -o dompurify-LICENSE.txt
   ```

3. Update `THIRD_PARTY_LICENSES.md` — bump the version column, replace
   the SHA-256, and update the `Source` link's tag.

4. Bump `?v=N` on the matching `<script>` / `<link>` tags in
   `packages/memtomem/src/memtomem/web/static/index.html` so users get
   the new bytes past their disk cache. (See
   `feedback_static_asset_cache_bust.md`.)

5. Smoke-test in a browser: open `mm web`, render a markdown chunk and a
   syntax-highlighted code block, confirm no console errors and no CSP
   violations.

## Why vendor instead of an `npm`/build step

The `memtomem` package has no JavaScript build pipeline — the SPA ships
raw `.js` and `.css` files. Adding `npm install` purely to pin nine
browser libraries would force every contributor (and `uv tool install`
user via sdist) to also have Node available. Direct vendoring of the
minified cdnjs builds keeps the install surface Python-only.
