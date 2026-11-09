# Vendored font: Nunito

| File | Font | Version | License |
|---|---|---|---|
| `nunito-latin-wght-normal.woff2` | [Nunito](https://github.com/googlefonts/nunito) variable (weights 200–1000), Latin subset | Fontsource `@fontsource-variable/nunito` 5.2.5 | SIL Open Font License 1.1 (`OFL.txt`) |
| `nunito-latin-ext-wght-normal.woff2` | Same, Latin Extended subset (accents of other European languages) | same | same |

Served by FastAPI under `/static/fonts/` and declared with `@font-face` in
`static/css/themes.css`. Stored locally instead of loading Google Fonts so that
opening the UI sends no request (and no IP address) to a third-party server.

The browser downloads a subset only when the page uses one of its characters
(`unicode-range`), so English/French pages usually need just the Latin file (39 KB).

Downloaded on 2026-11-09 from `cdn.jsdelivr.net/npm/@fontsource-variable/nunito@5.2.5/files/`.
Verify with `sha256sum -c SHA256SUMS`.
