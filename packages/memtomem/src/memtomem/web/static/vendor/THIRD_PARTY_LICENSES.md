# Third-Party Licenses — `mm web` vendored assets

The Web UI and the FastAPI Swagger documentation page shipped with
`memtomem` bundle a small set of browser-side JavaScript and CSS
libraries so the SPA and the `/api/docs` page render correctly without
outbound network access (offline / air-gapped / firewalled deployments).
Each library is redistributed verbatim under its upstream license. The
table below records the upstream npm package, the pinned version, the
original CDN source URL, the upstream license, and the SHA-256 of the
binary as fetched. The `npm package` column is the canonical advisory key
— note cdnjs calls Prism `prism` but the npm package is `prismjs`; the
vendored-advisory CI check (`tools/check_vendored_advisories.py`) queries
OSV by this column, never by the cdnjs path.

| File                           | npm package       | Version | License               | Upstream                                                                          | SHA-256 (full)                                                       |
| ------------------------------ | ----------------- | ------- | --------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `purify.min.js`                | `dompurify`       | 3.4.10  | Apache-2.0 OR MPL-2.0 | https://cdn.jsdelivr.net/npm/dompurify@3.4.10/dist/purify.min.js                   | `9aca84b86a0c35926d47994f354b37116044aab0aac9874f35a44322a5c96565`   |
| `marked.umd.js`                | `marked`          | 18.0.3  | MIT                   | https://cdn.jsdelivr.net/npm/marked@18.0.3/lib/marked.umd.js                       | `8fe6e9d26d01533807fdb8d7d081a4de43bb3909f7ba15ac69606f5e11891599`   |
| `prism.min.js`                 | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/prism.min.js                   | `a4a8b1f682003b325dc9754e825ed0ba12ebac8fac5433b520eb43d6b24da11b`   |
| `prism-python.min.js`          | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/components/prism-python.min.js | `ed4385685bcf2d4935c8dbbab4bde16603da1329e092d2bf36c3dadd67e9a85c`   |
| `prism-typescript.min.js`      | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/components/prism-typescript.min.js | `852f5513bb9ca9db247f86ecfce74acc91c541749d34929157240518fef8152a`   |
| `prism-json.min.js`            | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/components/prism-json.min.js   | `956d86baa5ae7ec4106758f354ac2d140bdcd7fc103dece02f73ed12b8d663e4`   |
| `prism-bash.min.js`            | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/components/prism-bash.min.js   | `6260814110e5182f2956e3bd257429548d9dbf2a9b66a63719b26cf9fac966a7`   |
| `prism-yaml.min.js`            | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/components/prism-yaml.min.js   | `719c8e8b8c344dc9de510c729f65ba840b1502a0a8e7e25e2ad19ee715f65c02`   |
| `prism-tomorrow.min.css`       | `prismjs`         | 1.30.0  | MIT                   | https://cdnjs.cloudflare.com/ajax/libs/prism/1.30.0/themes/prism-tomorrow.min.css  | `1b15fe2971998a048aebb60f26f6eed76122071db9ef3b995abd003224f52a98`   |
| `swagger/swagger-ui-bundle.js` | `swagger-ui-dist` | 5.32.5  | Apache-2.0            | https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.32.5/swagger-ui-bundle.js           | `c887594e3ba3ec9f60143305bba97a47aee71f47468f59c8f1ca06ac36a17c54`   |
| `swagger/swagger-ui.css`       | `swagger-ui-dist` | 5.32.5  | Apache-2.0            | https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.32.5/swagger-ui.css                 | `ca238f7d7c2cf4480c1e77a9c3b9da915ab216e96ffd354e69076560c650c6de`   |

## DOMPurify (`purify.min.js`) — Apache-2.0 OR MPL-2.0

Copyright 2024 Dr.-Ing. Mario Heiderich, Cure53.

Distributed under the dual Apache-2.0 / MPL-2.0 license (npm
`license: "(MPL-2.0 OR Apache-2.0)"`, unchanged through 3.4.10). The
vendored `dompurify-LICENSE.txt` reproduces BOTH license texts verbatim;
note that upstream's `LICENSE` file at the `3.4.10` tag now ships only the
Apache-2.0 half, so the retained dual-text copy is the more complete
attribution for a still-dual-licensed package.

Source: https://github.com/cure53/DOMPurify/blob/3.4.10/LICENSE

## marked (`marked.umd.js`) — MIT

Copyright (c) 2018+, MarkedJS; Copyright (c) 2011-2018, Christopher Jeffrey.

Includes the legacy Markdown 3-clause BSD copyright by John Gruber. Full
upstream license text is reproduced verbatim in `marked-LICENSE`.

Source: https://github.com/markedjs/marked/blob/v18.0.3/LICENSE

## Prism — MIT

Copyright (c) 2012 Lea Verou.

Applies to `prism.min.js`, `prism-tomorrow.min.css`, and all
`prism-*.min.js` language components. Full upstream license text is
reproduced verbatim in `prism-LICENSE.txt`.

Source: https://github.com/PrismJS/prism/blob/v1.30.0/LICENSE

## Swagger UI (`swagger/swagger-ui-bundle.js`, `swagger/swagger-ui.css`) — Apache-2.0

Copyright 2017–2025 SmartBear Software.

`swagger-ui-bundle.js` is a webpack-built artifact that statically
embeds a number of MIT- and BSD-licensed transitive dependencies
(classnames, deep-extend, immutable.js, react, redux, etc.). Their
attribution headers — extracted by webpack into the companion
`swagger-ui-bundle.js.LICENSE.txt` — are vendored alongside the bundle
to satisfy the source-form attribution clauses of those licenses. Full
upstream Swagger UI license text is reproduced verbatim in
`swagger/swagger-ui-LICENSE`.

Source: https://github.com/swagger-api/swagger-ui/blob/v5.32.5/LICENSE
