# Third-Party Licenses — `mm web` vendored assets

The Web UI shipped with `memtomem` bundles a small set of browser-side
JavaScript and CSS libraries so that the SPA renders correctly without
outbound network access (offline / air-gapped / firewalled deployments).
Each library is redistributed verbatim under its upstream license. The
table below records the pinned version, the original cdnjs source URL,
the upstream license, and the SHA-256 of the binary as fetched from cdnjs.

| File                       | Version | License                       | Upstream                                                                                          | SHA-256 (full)                                                       |
| -------------------------- | ------- | ----------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `purify.min.js`            | 3.1.6   | Apache-2.0 OR MPL-2.0         | https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js                              | `c0845096a7c4a6741f362ac506c94c1c7d27dc603bcc1bf64a587f76f2dbe3a1`   |
| `marked.min.js`            | 9.1.6   | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js                                 | `6002af63485b043fa60ddaba1b34363b98d2a8b2c63b607004f3a2405a8a053a`   |
| `prism.min.js`             | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js                                  | `e7b88bddc6c757b2fc8cb113e2469801ab14a78ec1a8fada4d6391e3573f5f9f`   |
| `prism-python.min.js`      | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-python.min.js                | `ed4385685bcf2d4935c8dbbab4bde16603da1329e092d2bf36c3dadd67e9a85c`   |
| `prism-typescript.min.js`  | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-typescript.min.js            | `852f5513bb9ca9db247f86ecfce74acc91c541749d34929157240518fef8152a`   |
| `prism-json.min.js`        | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-json.min.js                  | `956d86baa5ae7ec4106758f354ac2d140bdcd7fc103dece02f73ed12b8d663e4`   |
| `prism-bash.min.js`        | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-bash.min.js                  | `6260814110e5182f2956e3bd257429548d9dbf2a9b66a63719b26cf9fac966a7`   |
| `prism-yaml.min.js`        | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-yaml.min.js                  | `719c8e8b8c344dc9de510c729f65ba840b1502a0a8e7e25e2ad19ee715f65c02`   |
| `prism-tomorrow.min.css`   | 1.29.0  | MIT                           | https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css                 | `1b15fe2971998a048aebb60f26f6eed76122071db9ef3b995abd003224f52a98`   |

## DOMPurify (`purify.min.js`) — Apache-2.0 OR MPL-2.0

Copyright 2024 Dr.-Ing. Mario Heiderich, Cure53.

Distributed under the dual Apache-2.0 / MPL-2.0 license. Full upstream
license text is reproduced verbatim in `dompurify-LICENSE.txt` alongside
this file.

Source: https://github.com/cure53/DOMPurify/blob/3.1.6/LICENSE

## marked (`marked.min.js`) — MIT

Copyright (c) 2018+, MarkedJS; Copyright (c) 2011-2018, Christopher Jeffrey.

Includes the legacy Markdown 3-clause BSD copyright by John Gruber. Full
upstream license text is reproduced verbatim in `marked-LICENSE.md`.

Source: https://github.com/markedjs/marked/blob/v9.1.6/LICENSE.md

## Prism — MIT

Copyright (c) 2012 Lea Verou.

Applies to `prism.min.js`, `prism-tomorrow.min.css`, and all
`prism-*.min.js` language components. Full upstream license text is
reproduced verbatim in `prism-LICENSE.txt`.

Source: https://github.com/PrismJS/prism/blob/v1.29.0/LICENSE
