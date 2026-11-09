# Vendored front-end libraries

Third-party scripts are stored in the repository and served by FastAPI
(`/static/vendor/...`) instead of being loaded from a public CDN:

- **Sovereignty / privacy**: opening the UI makes no request to a third-party server.
- **Availability**: the UI works even without internet access.
- **Supply-chain safety**: the exact files are pinned and reviewed; a compromised
  CDN cannot change them. Verify with `sha256sum -c SHA256SUMS`.

| File | Library | Version | License | Purpose |
|---|---|---|---|---|
| `marked.umd.js` | [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT | Markdown → HTML |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.6 | Apache-2.0 / MPL-2.0 | Sanitises that HTML (XSS protection) |
| `highlight.min.js` | [highlight.js](https://highlightjs.org) | 11.11.1 | BSD-3-Clause | Syntax highlighting in code blocks |
| `highlight-github-dark.min.css` | highlight.js theme | 11.11.1 | BSD-3-Clause | Dark colour theme for code |

Downloaded on 2026-11-09 from `cdn.jsdelivr.net/npm/<package>@<version>/...`.
To upgrade: download the new version, review the changelog, update this table and
`SHA256SUMS`.
