# Vendored WebUI dependencies

These browser bundles are served locally by `webui.py` so the login page and WebUI work without public-CDN access.

| File | Version | Upstream URL | SHA-256 |
| --- | --- | --- | --- |
| `vue.global.prod.js` | Vue 3.5.13 | `https://unpkg.com/vue@3.5.13/dist/vue.global.prod.js` | `c459ba7cc8db65c982589fa5d64c7ff478877e8e5b0fd75683207cec6a4e89e8` |
| `marked.min.js` | marked 12.0.2 | `https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js` | `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894` |
| `purify.min.js` | DOMPurify 3.2.4 | `https://cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.min.js` | `8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0` |

The upstream license notices remain embedded in the downloaded bundles.
