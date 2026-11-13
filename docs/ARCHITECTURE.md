# Software architecture

This document describes how the Nuage Libre application is organised, how a
request travels through it, and **why** each technical choice was made.

## 1. Context

```
 ┌──────────── Tailscale private network (WireGuard, Zero-Trust) ────────────┐
 │                                                                           │
 │  MacBook ─┐                                                               │
 │           ├── HTTPS/HTTP ──▶  Bazzite server (RX 7900 XTX, 32 GB RAM)      │
 │  Android ─┘                    ├── FastAPI app         (venv, port 8000)   │
 │                                ├── PostgreSQL 18       (container, 5432)   │
 │                                ├── Ollama + ROCm       (container, 11434)  │
 │                                ├── Cloud storage folder (2 TB NVMe)        │
 │                                └── Samba file share    (2 TB NVMe)         │
 └───────────────────────────────────────────────────────────────────────────┘
```

The FastAPI application is the single entry point for the browser. It is the
only component allowed to talk to the database, to the AI engine and to the
cloud storage folder; none of them is meant to be reached directly by client
devices.

## 2. Layered architecture

| Layer | Folder | Responsibility | Must NOT |
|---|---|---|---|
| Presentation | `app/routers/`, `app/schemas.py`, `static/` | Parse and validate HTTP requests, map results/errors to HTTP status codes, serve the UI | Contain SQL or business rules |
| Business | `app/services/` | Chat orchestration, prompt building, transaction boundaries, talking to Ollama | Know about HTTP status codes or write SQL |
| Data access | `app/repositories/`, `app/models.py` | Every database query, ORM mapping; every file read/write on the storage disk (`file_storage.py`) | Commit transactions or contain business rules |
| Infrastructure | `app/database.py`, `app/config.py`, `migrations/`, `db/` | Connection pool, configuration, schema versioning | — |

**Why layers?**

- **Separation of concerns**: a change in one concern (e.g. optimising a query)
  touches one file.
- **Testability**: each layer can be tested in isolation, and external systems
  (Ollama) can be replaced with fakes (see `tests/conftest.py`).
- **Security**: validation happens once, at the boundary (presentation layer),
  before any untrusted data reaches the business or data layers.
- **Replaceability**: switching from Ollama to another inference engine only
  changes `ollama_client.py`.

### Dependency injection

Routes receive their collaborators through FastAPI's `Depends()` (see
`app/dependencies.py`): settings → Ollama client → chat service, settings →
file storage → file service, and a database session per request. Nothing is
instantiated inside route functions, which lets the tests replace any
dependency with `app.dependency_overrides`.

The identity of the caller is injected the same way:
`user_id = Depends(get_current_user_id)` (`app/auth.py`). It is the single
place that will change when authentication arrives (§4.11).

## 3. Request lifecycle: sending a chat message

1. The browser sends `POST /api/chat` with `{conversation_id, message}`.
2. **Validation** (`schemas.ChatRequest`): the message must be a non-blank
   string; `conversation_id` must be a valid UUID or `null`. Otherwise FastAPI
   answers **422** automatically. The router also enforces the configurable
   maximum length (**413**).
3. **`ChatService.prepare()`** (read-only):
   - new conversation → a UUID is generated in Python and the title is derived
     from the first line of the message;
   - existing conversation → it is loaded (**404** if unknown) together with its
     last *N* messages (`NUAGE_MAX_HISTORY_MESSAGES`).
   - The prompt is built: history (final answers only, without the stored
     reasoning) + the new message.
4. The router returns a `StreamingResponse`; HTTP 200 and headers are sent
   immediately.
5. **`ChatService.stream_reply()`** relays the Ollama stream, converting each
   chunk into a typed event (`thinking` / `content`).
6. When the model is done, **one transaction** inserts the conversation (if new),
   the user message and the assistant answer, then a `done` event is sent with
   the generation statistics.

### Uploading a file (summary)

1. The browser sends `POST /api/files` with the **raw bytes** as the body,
   `Content-Type: application/octet-stream` and the name in `X-File-Name`.
2. The router checks the protocol (415 / 411 / 422); the service checks the
   rules (name, size limit 413, free space 507) **before reading the body**.
3. `FileStorage` streams the body to `<root>/<user_id>/.<file_id>.part`,
   `fsync`s it, renames it atomically to `<file_id>`.
4. Only then is the `files` row inserted; if that fails, the bytes are deleted.

Full life cycle, failure table and diagrams: [CLOUD_STORAGE.md](CLOUD_STORAGE.md) §3.

## 4. Design decisions

### 4.1 FastAPI (async) rather than a synchronous framework

LLM answers take seconds to minutes. With a synchronous server, each waiting
request would block a worker thread. FastAPI runs on an event loop: while one
request waits for tokens from the GPU, the same process keeps serving other
requests (conversation list, health checks). This is why the whole chain is
async: `httpx.AsyncClient` for Ollama, `asyncpg` + SQLAlchemy async for
PostgreSQL.

### 4.2 Streaming with NDJSON

| Option | Pros | Cons |
|---|---|---|
| Wait for the full answer | Simplest | User stares at a spinner for 30+ s |
| WebSocket | Bidirectional | Extra protocol, connection state, overkill for request/response |
| Server-Sent Events | Standard for push | `EventSource` only supports GET, so the message would go in the URL |
| **NDJSON over a POST response** ✅ | Plain HTTP, works with `fetch()`, same format Ollama uses | Client must split lines itself (10 lines of JS) |

Each line is a JSON object with a `type` field. Typed events let the UI tell
reasoning from answer from errors, which the first version (raw text) could not.

### 4.3 One atomic transaction at the end of generation

The question and its answer are saved together, after generation, in a single
transaction (`async with session.begin()`).

- If Ollama fails (down, out of memory, timeout) → nothing is written. The
  database never contains a question without an answer.
- If the database write fails → automatic rollback, no partial exchange.

Trade-off accepted: if the user closes the tab mid-answer, the exchange is lost.

The streaming generator runs *after* the route function has returned, so it does
not reuse the request's database session: it opens its own short-lived session
from `SessionLocal` just for this final write.

### 4.4 Explicit timestamps

PostgreSQL's `now()` returns the start time of the *transaction*. Since the user
message and the answer are inserted in the same transaction, both would get the
same `created_at` and their order would be undefined. The service therefore sets
`created_at` explicitly: the request time for the question, the end of generation
for the answer.

### 4.5 UUID primary keys

Ids appear in URLs (`/api/conversations/{id}`, `/api/files/{id}/download`).
Sequential integers would reveal how many conversations exist and let anyone try
`1, 2, 3…`. Random UUIDv4s are not guessable. Combined with the ownership filter
of every query (§4.11), knowing an id is never enough to reach someone else's data.

### 4.6 PostgreSQL rather than SQLite

- Real concurrent access and a client/server model that mirrors production setups.
- Role-based privileges (least privilege, see `docs/DATABASE.md`).
- **pgvector** extension: the future RAG feature will store document embeddings
  in the same database and query them by vector similarity, so relational data
  and vector data live in one place with one backup.

### 4.7 Bounded history and explicit context size (eco-design & stability)

Every token in the prompt costs GPU time and VRAM. The application:

- re-sends at most `NUAGE_MAX_HISTORY_MESSAGES` messages;
- never re-sends the stored reasoning of previous turns (often longer than the
  answer itself);
- fixes `num_ctx` explicitly, so VRAM use is predictable (≈ 18 GB at 16k tokens
  with the Q4_K_M model) and out-of-memory crashes are avoided;
- records `token_count` and `generation_ms` for each answer, which makes it
  possible to measure usage (and, combined with a power meter, energy per answer).

### 4.8 Model selection

`GET /api/models` reads the installed models from Ollama (`/api/tags`) and keeps
only those with the `completion` capability (embedding-only models cannot chat).
They are sorted by size so the fast models come first, and the menu shows
parameter count, size on disk and whether the model exposes its reasoning (🧠).

The model name sent by the browser is **untrusted input**. `ModelCatalog.validate()`
checks it against the installed models (allow-list) before use: an unknown name
is rejected with 422 and never reaches Ollama.

Resolution order for each message:

1. the model explicitly chosen in the menu (validated);
2. otherwise, for an existing conversation, the model of its latest answer;
3. otherwise the default model (`NUAGE_MODEL`).

Each assistant message stores its own `model`, because a conversation can mix
several models; `conversations.model` holds the most recent one.

### 4.9 Markdown rendering

Models answer in Markdown. The UI converts it to HTML with **marked**, then
sanitises that HTML with **DOMPurify** before inserting it, and colours code blocks
with **highlight.js**.

- *Why sanitise?* The model's output is untrusted: it can contain raw HTML (by
  mistake, or because a prompt or — later — a document asked it to). Without
  DOMPurify, `<img src=x onerror=...>` in an answer would execute JavaScript in
  the user's browser. DOMPurify removes scripts, event handlers, iframes and
  `javascript:` URLs while keeping formatting. Verified in the end-to-end test
  (`docs/TEST_PLAN.md`, T-SEC-03).
- *Why vendor the libraries?* They are stored in `static/vendor/` with pinned
  versions and SHA-256 checksums instead of being loaded from a CDN: no request
  leaves the machine, the UI works offline, and a compromised CDN cannot inject code.
- *Streaming performance*: re-rendering on every token would be wasteful (up to
  150 tokens/s with small models). Rendering is throttled to at most one per
  screen frame with `requestAnimationFrame`, and syntax highlighting runs only once
  the answer is complete.
- User messages are never interpreted as Markdown: they stay plain text.

**Mathematics (KaTeX).** Formulas written in LaTeX are typeset with KaTeX
(vendored). The four notations models use are recognised: `$…$` and `\(…\)`
inline, `$$…$$` and `\[…\]` displayed.

- *Integration*: a small **marked extension** (custom tokenizers in
  `static/js/app.js`) detects formulas *before* Markdown rules apply, otherwise
  `a_1 * b_1` would become italics and `\(` would lose its backslash (Markdown
  escape). The extension calls `katex.renderToString()`.
- *Pipeline unchanged*: Markdown → marked (+ math) → **DOMPurify** → `innerHTML`.
  KaTeX's HTML is sanitised like everything else (defence in depth); DOMPurify
  keeps what KaTeX needs (`span` + `class`/`style`, MathML, SVG paths).
- *KaTeX hardening*: `trust: false` refuses `\href`, `\url`, `\htmlClass`,
  `\includegraphics`…; `maxExpand` and `maxSize` stop recursive macros and giant
  sizes (a "macro bomb" `\def\a{\a\a}\a` is reported as an error instead of
  freezing the tab); invalid formulas are shown in the theme's error colour with
  their source (`throwOnError: false`) instead of breaking the answer.
- *Accessibility*: output is HTML (visual) + MathML (read by screen readers).
  DOMPurify forbids MathML `<semantics>`/`<annotation>` but keeps their text; a
  DOMPurify hook empties `<annotation>` first so the raw TeX is not read aloud.
- *Dollar ambiguity*: `$5 and $10` must stay text. Rule from Pandoc: no space
  just inside the dollars, and no digit right after the closing one. Exception
  added after the end-to-end test: `$ x^2 $` (space on **both** sides), a form
  the real model writes.
- *Streaming*: a formula is rendered once its closing delimiter has arrived;
  rendered HTML is cached (bounded `Map`), so 60 re-renders per second do not
  re-typeset the same formula.

### 4.10 Front-end: visual identity and themes

The UI has its own visual identity, built around the logo (a small *kawaii*
cloud): "organic and fluid", no rectangular cards, chat bubbles shaped like
soft asymmetric clouds floating over a living sky.

**Files and how they are linked.** No build step: FastAPI serves the files as
they are. `GET /` returns `static/index.html` (route in `app/main.py`), and
every other file is served by the `StaticFiles` mount at `/static/...`.

```
static/
├── index.html          structure only; links everything below
├── css/
│   ├── themes.css      design tokens (CSS custom properties) + the 3 themes
│   ├── app.css         layout (app bar, split-screen), chat, cloud bubbles, sky
│   ├── files.css       file manager panel (private cloud)
│   └── markdown.css    typography of the model's answers
├── js/
│   ├── theme.js        theme switcher, circular reveal, "themechange" event
│   ├── layout.js       view switcher (split / chat / files), "viewchange" event
│   ├── sky.js          drifting clouds (day), star canvas (night)
│   ├── files.js        file manager (list, drag & drop upload, download)
│   └── app.js          chat logic (API, streaming, conversations, models, RAG switch)
├── fonts/              Nunito variable font (woff2), SIL Open Font License
└── vendor/             marked, DOMPurify, highlight.js (pinned)
```

```html
<link rel="stylesheet" href="/static/css/themes.css" />   <!-- tokens first -->
<link rel="stylesheet" href="/static/css/app.css" />      <!-- then components -->
<script defer src="/static/js/app.js"></script>            <!-- defer: runs after parsing, in order -->
```

**Design tokens.** Components never use a raw colour: they use variables such
as `var(--bubble-ai-bg)` or `var(--accent-ink)`. A theme is only a set of
values for these variables, selected by `<html data-theme="day|night|pro">`.
Switching theme changes one attribute; the browser recomputes every colour.
Adding a fourth theme would mean adding one block of variables, no component
change. This is the same principle as the design tokens of design systems
(Material, Carbon...).

| Theme | Intent | Background | Specific effects |
|---|---|---|---|
| **Ciel de Jour** | Creative, light | Dawn gradient, drifting blurred colour blobs ("aurora"), 7 soft clouds crossing the sky | White fluffy bubbles with "puffs", indigo text `#1E293B` |
| **Ciel de Nuit** | Creative, dark | `#0F172A`, two breathing nebulae, star field on `<canvas>` | Twinkling, mouse parallax, shooting stars, glowing accents |
| **Mode Pro** | Jury / technical audience | Pearl grey with a faint dot grid, nothing moves | Pure white surfaces, 1px borders, near-black primary buttons, discreet rounded bubbles |

**Main techniques**

- *Cloud bubbles*: asymmetric `border-radius` (8 values: horizontal / vertical
  radius of each corner) plus two circles drawn with `::before` / `::after`
  overlapping the top edge, which turns the rounded shape into a cloud. The
  pointed corner shows who is speaking.
- *Star field*: a single `<canvas>` redrawn with `requestAnimationFrame`
  (hundreds of DOM elements would be much heavier). Each star has a depth: near
  stars move more with the mouse (parallax). Drawn at `devicePixelRatio` for
  sharpness on high-density screens.
- *Theme reveal*: the View Transitions API snapshots the page, the theme
  changes, and the new snapshot is revealed by a growing `clip-path` circle
  from the clicked button. Browsers without the API simply switch instantly.
- *No flash of the wrong theme*: a 10-line inline script in `<head>` applies
  the saved theme before the first paint (first visit: follows the OS light/dark
  preference).
- *Mascot*: an SVG (three circles + a rounded rectangle = cloud) defined once in
  a `<template>` and cloned where needed. CSS makes it blink, float, and bounce
  while the model is generating.

**Accessibility (RGAA / WCAG)**

- The pastel brand colours (`#5BCEFA`, `#F5A9B8`) have a contrast of ~1.9:1 on
  white, below the 4.5:1 minimum for text. They are used for surfaces and
  decoration only; text and links use darker "ink" variants of the same hue
  (`--accent-ink`, 5.6:1 in the day theme).
- `prefers-reduced-motion`: all animations are disabled and the sky is drawn
  once, without twinkling or shooting stars (RGAA 13.8).
- The theme switcher follows the WAI-ARIA *radio group* pattern
  (`role="radio"`, `aria-checked`, arrow keys, roving `tabindex`).
- Visible keyboard focus everywhere (`:focus-visible`); Escape closes the
  mobile menu; decorative layers are `aria-hidden`.

**Eco-design and performance**

- No framework, no CSS library: 3 hand-written stylesheets.
- The star animation runs only while the night theme is shown *and* the tab is
  visible (`visibilitychange`); inactive sky layers are `visibility: hidden`
  so their CSS animations are not painted.
- Animations use `transform` and `opacity` only, which the GPU composites
  without recalculating the layout.

**Sovereignty.** The previous version loaded Tailwind CSS from a public CDN.
It is gone: fonts are served locally too (`static/fonts/`), so opening the UI
makes no request outside the machine. An automated test enforces it
(`T-SEC-04`).

### 4.11 Multi-user foundation: the "Ghost User"

Authentication comes in v0.6, but the data model is multi-user **now**: a
minimal `users` table, `conversations.user_id` and `files.user_id` as NOT NULL
foreign keys, and every repository query filtered by `user_id`. Until login
exists, that id is always the **Ghost User**, a real row seeded by migration
`c3e9a1d4b7f2` with a hard-coded UUID (`GHOST_USER_ID` in `app/auth.py`).

The caller's identity comes from one FastAPI dependency,
`get_current_user_id()` (the *seam*). In v0.6 only its body changes (verify a
JWT); routers, services, repositories and tables stay as they are. The tests
already impersonate a second user through that seam and prove the isolation
(T-USER-03/04). Another user's resource answers 404, not 403, so the API never
confirms that an id exists. Details and alternatives considered:
[CLOUD_STORAGE.md](CLOUD_STORAGE.md) §2.3.

### 4.12 Private cloud storage

- **Bytes on disk, index in PostgreSQL.** Files are stored under
  `NUAGE_STORAGE_DIR` (2 TB NVMe) as `<user_id>/<file_id>`; the `files` table
  holds the display name, type, size, owner and a *relative* path.
- **Raw-body upload streamed to disk** rather than FastAPI's multipart
  `UploadFile`: on this server `/tmp` is a tmpfs (RAM shared with the LLM), where
  Starlette would first copy every upload. Streaming writes once, in constant
  memory, and the size limit is checked before reading.
- **Disk first, database last**, with a compensating delete if the `INSERT`
  fails: a row never points to missing bytes.
- **Two storage technologies, two data-access classes**: `FileRepository` (SQL)
  and `FileStorage` (disk), coordinated by `FileService`.
- **Folders are logical** (v0.5.1): a `folders` table (adjacency list,
  `parent_id`), never directories on disk. Composite foreign keys keep every
  tree within one user; `UNIQUE … NULLS NOT DISTINCT` forbids duplicate names,
  at the root too. A folder dropped from the computer is recreated by the
  browser (folders first, then each file through the normal streamed upload):
  the server never extracts an archive (no zip bomb, no zip slip).

Full rationale: [CLOUD_STORAGE.md](CLOUD_STORAGE.md) §5 and §11.

### 4.13 Split-screen workspace

The main screen is a CSS grid with two panels (file manager, chat) whose column
widths follow `<html data-view="split|chat|files">` (`1fr 1fr`, `0fr 1fr`,
`1fr 0fr`, animated). `js/layout.js` only sets the attribute and makes the hidden
panel `inert`. Inside the panels, layout decisions use **container queries**
(the width of the panel, not of the screen): e.g. the conversation history is a
column beside the chat when the chat panel is wide, a drawer otherwise. Below
900 px, "split" falls back to the chat. Details: [CLOUD_STORAGE.md](CLOUD_STORAGE.md) §6.

### 4.14 Following the answer while it is written (v0.5.1)

While the model writes, the chat (and the reasoning box) keeps the latest
line in view, **as long as the reader is at the bottom**. As soon as they
scroll up, the view stays where they put it, so they can read while the model
keeps writing; scrolling back to the bottom, or the "Latest" button that
appears above the composer, resumes following. `followScroll()` in
`js/app.js` implements this once and is used for both boxes.

- **Why watch the wheel, touch and keys, not only `scroll`.** A model writes
  30 to 50 tokens per second, so new content arrives between two frames. The
  `scroll` event is delivered *after* the browser has scrolled, and by then
  the next token may already have put the view back at the bottom: the user's
  gesture was undone before the code could see it (the reported bug). `wheel`,
  `touchmove` and `keydown` (↑, Page Up, Home) are delivered *before* the
  browser scrolls, so following stops at the first notch. The `scroll` event
  still catches everything else (scrollbar, find in page, screen readers).
- **One scroll per frame**: the follow-up scroll runs in
  `requestAnimationFrame`, after that frame's Markdown rendering, and checks
  again that the user did not scroll up meanwhile.
- **Nested boxes**: a wheel turn inside the reasoning box scrolls that box
  only; the chat keeps following (`innerBoxScrolls()`).
- **A click in an empty area** of the history (or on its scrollbar) pauses
  following while the button is down and resumes it on release if the view
  did not move.
- **Accessibility**: the history and the reasoning box are focusable
  (`tabindex="0"`), so they can be scrolled with the keyboard; activating
  "Latest" moves the focus to the message field (or the history while the
  model writes) instead of losing it; the smooth scroll is replaced by an
  instant jump under `prefers-reduced-motion`.

Tested in a real browser with real wheel and keyboard events
([TEST_PLAN.md](TEST_PLAN.md) §9).

### 4.15 Soft glows: clipping a little further out (v0.5.1)

A closing panel must clip its content (it slides away instead of spilling
over its neighbour), so the panels had `overflow: hidden`. That also cut the
glows of the buttons near their edges (New chat, RAG switch, composer) along
the panel's border: a straight line where the light should fade. The panels
now use `overflow: clip` with `overflow-clip-margin: var(--glow-room)` (40 px):
the clipping happens 40 px further out, where every glow has already faded.
While a panel closes, the margin returns to 0. The history drawer, which slid
in from the panel's edge, now clips itself with an animated `clip-path` that
moves exactly with it. The glows themselves were redrawn as two offset halos
(blue on the left, pink on the right, like the brand gradient) with a negative
spread, which fades from inside the shape instead of drawing an outline.

Measured on screenshots: above the RAG switch, the luminance jumped from 29 to
47 in one pixel row before the fix; it now rises smoothly. Browsers without
`overflow-clip-margin` clip at the border, as before (no regression).

### 4.16 HTTP caching of the interface (v0.5.1)

Static files were served without `Cache-Control`, so browsers reused them for a
"heuristic" period (about 10 % of the file's age) without asking the server:
after an update, a browser could combine yesterday's `app.css` with today's
`files.js` (seen during the end-to-end tests). The page and `/static` now send
`Cache-Control: no-cache`: the browser keeps its copy but revalidates it with
its ETag; an unchanged file costs a `304 Not Modified` of a few bytes
(test T-UI-03). On a local network this costs nothing noticeable, and an
update is always seen whole.

## 5. Security

| # | Risk (OWASP reference) | Mitigation in Nuage Libre | Where |
|---|---|---|---|
| 1 | Injection – SQL | SQLAlchemy expressions → always parameterised statements; no string concatenation | `repositories/`, test `T-SEC-01` |
| 2 | Injection – XSS | Markdown answers (including KaTeX output) sanitised by DOMPurify before `innerHTML`; KaTeX `trust: false`; all other text via `textContent` | `static/js/app.js` (`renderMarkdown`), T-SEC-03, T-SEC-05 |
| 2b | Untrusted parameter (model name) | Allow-list of installed chat models, 422 otherwise | `model_catalog.py`, T-SEC-02 |
| 2c | Software supply chain | Front-end libraries and fonts vendored, pinned, checksummed; no CDN (enforced by a test) | `static/vendor/`, `static/fonts/`, T-SEC-04 |
| 3 | Broken access control | Every query scoped by `user_id`; another user's data answers 404 (IDOR); composite foreign keys keep folder trees within one user, even for direct SQL. ⚠️ No authentication yet: the caller is always the Ghost User, access relies on Tailscale | `repositories/`, `auth.py`, T-USER-03/04, T-FOLD-09, T-SEC-11; login in v0.6 |
| 3b | Path traversal (file download) | Paths built only from server UUIDs; typed `uuid` route parameter; ownership filter; `CHECK` constraint on `files.file_path`; containment check after `resolve()` (symlinks included) | `file_storage.py`, T-SEC-07/08, [CLOUD_STORAGE.md](CLOUD_STORAGE.md) §4.2 |
| 3c | Stored XSS through uploaded files | Inline display only for script-free types; text shown as `text/plain`; `nosniff`; `CSP: sandbox`; HTML/SVG always downloaded | `file_policy.py`, T-SEC-06 |
| 3d | CSRF (cross-site upload) | Upload requires a custom header and `application/octet-stream` → CORS preflight, never granted; multipart refused (415); folder creation requires `application/json` (a `text/plain` body is refused, 422); `Cross-Origin-Resource-Policy: same-origin` on downloads | `routers/files.py`, `routers/folders.py`, T-FILE-07, T-SEC-13 |
| 3e | Unbounded recursion | Folder depth limited to 32; the recursive path query is bounded, even on a cycle made by hand | `folder_repository.py`, T-FOLD-07, T-SEC-12 |
| 4 | Security misconfiguration | DB port bound to localhost; restricted DB role; secrets outside Git | `compose.yaml`, `db/init/` |
| 5 | Cryptographic failures / secrets | Random passwords in `.env` (`chmod 600`, git-ignored) | `.env.example` |
| 6 | Insecure design – data integrity | FK, CHECK constraint, atomic transactions | `models.py`, `chat_service.py` |
| 7 | Information disclosure | Generic error messages to clients, details only in logs | `chat_service.py` |
| 8 | Resource exhaustion (LLM10 *Unbounded Consumption*) | Max message length, bounded history, `limit` validated, fixed context size; uploads: size limit checked before reading (413), `Content-Length` required (411), free-disk reserve (507), streaming in constant memory | `config.py`, routers, `file_service.py` |
| 8b | Header injection / spoofed file names | Names cleaned: control characters (CR/LF), bidirectional overrides (RTLO), path parts removed; NFC | `file_policy.clean_filename`, T-POL-01 |
| 9 | Prompt injection (LLM01) | Not exploitable yet (files are stored, not read by the model; no tools). Will be addressed with RAG: documents treated as untrusted data, no tool execution from document content | Roadmap v0.7 |

References: OWASP Top 10, OWASP Top 10 for LLM Applications (2025),
ANSSI recommendations for PostgreSQL deployments.
