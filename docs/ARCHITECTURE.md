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
 │                                └── Samba file share    (2 TB NVMe)         │
 └───────────────────────────────────────────────────────────────────────────┘
```

The FastAPI application is the single entry point for the browser. It is the
only component allowed to talk to the database and to the AI engine; neither
PostgreSQL nor Ollama is meant to be reached directly by client devices.

## 2. Layered architecture

| Layer | Folder | Responsibility | Must NOT |
|---|---|---|---|
| Presentation | `app/routers/`, `app/schemas.py`, `static/` | Parse and validate HTTP requests, map results/errors to HTTP status codes, serve the UI | Contain SQL or business rules |
| Business | `app/services/` | Chat orchestration, prompt building, transaction boundaries, talking to Ollama | Know about HTTP status codes or write SQL |
| Data access | `app/repositories/`, `app/models.py` | Every database query, ORM mapping | Commit transactions or contain business rules |
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
`app/dependencies.py`): settings → Ollama client → chat service, and a database
session per request. Nothing is instantiated inside route functions, which lets
the tests replace any dependency with `app.dependency_overrides`.

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

Ids appear in URLs (`/api/conversations/{id}`). Sequential integers would reveal
how many conversations exist and let anyone try `1, 2, 3…`. Random UUIDv4s are
not guessable. This will matter once multiple users exist (v0.5).

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
│   ├── app.css         layout, cloud bubbles, sky layers, animations
│   └── markdown.css    typography of the model's answers
├── js/
│   ├── theme.js        theme switcher, circular reveal, "themechange" event
│   ├── sky.js          drifting clouds (day), star canvas (night)
│   └── app.js          chat logic (API, streaming, conversations, models)
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

## 5. Security

| # | Risk (OWASP reference) | Mitigation in Nuage Libre | Where |
|---|---|---|---|
| 1 | Injection – SQL | SQLAlchemy expressions → always parameterised statements; no string concatenation | `repositories/`, test `T-SEC-01` |
| 2 | Injection – XSS | Markdown answers sanitised by DOMPurify before `innerHTML`; all other text via `textContent` | `static/js/app.js` (`renderMarkdown`), T-SEC-03 |
| 2b | Untrusted parameter (model name) | Allow-list of installed chat models, 422 otherwise | `model_catalog.py`, T-SEC-02 |
| 2c | Software supply chain | Front-end libraries and fonts vendored, pinned, checksummed; no CDN (enforced by a test) | `static/vendor/`, `static/fonts/`, T-SEC-04 |
| 3 | Broken access control | ⚠️ Not yet: no authentication. Mitigated by Tailscale network-level access only | Roadmap v0.5 |
| 4 | Security misconfiguration | DB port bound to localhost; restricted DB role; secrets outside Git | `compose.yaml`, `db/init/` |
| 5 | Cryptographic failures / secrets | Random passwords in `.env` (`chmod 600`, git-ignored) | `.env.example` |
| 6 | Insecure design – data integrity | FK, CHECK constraint, atomic transactions | `models.py`, `chat_service.py` |
| 7 | Information disclosure | Generic error messages to clients, details only in logs | `chat_service.py` |
| 8 | Resource exhaustion (LLM10 *Unbounded Consumption*) | Max message length, bounded history, `limit` validated (1–200), fixed context size | `config.py`, routers |
| 9 | Prompt injection (LLM01) | Not exploitable yet (no documents, no tools). Will be addressed with RAG: documents treated as untrusted data, no tool execution from document content | Roadmap v0.6 |

References: OWASP Top 10, OWASP Top 10 for LLM Applications (2025),
ANSSI recommendations for PostgreSQL deployments.
