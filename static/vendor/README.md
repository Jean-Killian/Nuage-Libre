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
| `katex.min.js` | [KaTeX](https://katex.org) | 0.16.22 | MIT (`LICENSE-katex.txt`) | LaTeX formulas → HTML + MathML |
| `katex.min.css` | KaTeX stylesheet | 0.16.22 | MIT | Layout of formulas; loads the fonts below |
| `fonts/KaTeX_*.woff2` (20 files) | KaTeX math fonts | 0.16.22 | SIL OFL 1.1 | Math glyphs (only the `.woff2` format is vendored: every current browser uses it first, the `.woff`/`.ttf` fallbacks listed in the CSS are never requested) |

Downloaded on 2026-11-09 from `cdn.jsdelivr.net/npm/<package>@<version>/...`.
KaTeX's CSS references its fonts with relative URLs (`fonts/KaTeX_...woff2`), which
resolve to `/static/vendor/fonts/`: no request leaves the machine (checked by T-SEC-04).
To upgrade: download the new version, review the changelog, update this table and
`SHA256SUMS`.
