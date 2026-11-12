# Test plan

## 1. Strategy

| Level | What is tested | Tools | Real or fake dependencies |
|---|---|---|---|
| Unit | `OllamaClient`: parsing of Ollama's NDJSON protocol, error handling | pytest, `httpx.MockTransport` | Network replaced by hand-written responses |
| Unit | File rules (`file_policy.py`: names, MIME types, inline allow-list, sandbox) and `FileStorage` (path containment, atomic writes) | pytest, `tmp_path` | No database, no HTTP; a fresh temporary folder per test |
| Integration | HTTP routes → service → repository → **real PostgreSQL** | pytest, pytest-asyncio, `httpx.ASGITransport` | Real test database; Ollama replaced by `FakeOllama` |
| Security | SQL injection, input validation, id format, path traversal, stored XSS, IDOR between users, CSRF by design | same as integration | A second user is impersonated through `get_current_user_id` (`act_as` fixture) |
| End-to-end (manual + scripted) | Full UI in a real browser with the real model on the GPU | Firefox (WebDriver BiDi script), curl | Everything real |
| Static analysis | Code style, likely bugs, security smells (Bandit rules) | Ruff | — |

**Test environment**

- Dedicated database `nuage_libre_test`, created by `db/init/01-create-roles.sh`
  with the same roles and privileges as production.
- The schema is created by running the **real Alembic migrations**
  (`tests/conftest.py::migrated_database`), so the migrations are tested too.
- Every test starts with empty tables (`clean_tables` fixture): tests are
  independent and can run in any order.
- Safety net: `conftest.py` refuses to run if the database name does not end in
  `_test`, so the real data can never be wiped by the test suite.
- `FakeOllama` returns a fixed answer (`"Hello Killian!"` with some reasoning and
  statistics): results are deterministic and the suite runs in < 1 s without a GPU.
- Uploaded files go to a temporary folder created for the session
  (`NUAGE_STORAGE_DIR` is overridden before the app is imported), emptied before
  each test and deleted at the end: the real storage on the Lexar disk is never
  touched.
- `act_as(user_id)` replaces the identity dependency exactly as JWT
  authentication will in v0.6, to test the isolation between users.

**Run**

```bash
venv/bin/pytest -v
venv/bin/ruff check . && venv/bin/ruff format --check .
```

## 2. Test cases

### Chat (`tests/test_chat.py`)

| ID | Requirement | Input | Expected result |
|---|---|---|---|
| T-CHAT-01 | A first message creates a conversation and streams the answer | `{"message": "Hi, I am Killian"}` | 200, `application/x-ndjson`; events `conversation, thinking, content, content, done`; `is_new=true` |
| T-CHAT-02 | Question and answer are persisted with reasoning and stats | One message, then `GET` the conversation | 2 messages `user`, `assistant`; thinking and token count stored; answer dated after question |
| T-CHAT-03 | The model receives the conversation history | Two messages in the same conversation | Prompt = [user, assistant, user]; no `thinking` field re-sent |
| T-CHAT-04 | History is bounded | `max_history_messages=3`, third message | Prompt has exactly 3 messages (the most recent) |
| T-CHAT-05 | Generation failure saves nothing (atomicity) | Fake Ollama raises "out of memory" | Events `conversation, error`; 0 conversation, 0 message in DB |
| T-CHAT-06 | Unknown conversation is rejected | Random UUID | 404 `Conversation not found.` |
| T-CHAT-07 | Blank message is rejected before reaching the model | `"   \n  "` | 422; model never called |
| T-CHAT-08 | Maximum message length enforced | limit 10, 11 chars | 413; model never called |
| T-CHAT-09 | Malformed id is rejected | `"1 OR 1=1"` as conversation_id | 422 |
| T-CHAT-10 | Title derived from the first line, truncated | 100 chars + 2nd line | Title of 60 chars ending with `…` |
| T-SEC-01 | SQL injection payload is stored as data | `'); DROP TABLE messages; --` | Stored verbatim; `messages` table still exists |

### Conversations & health (`tests/test_conversations.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-CONV-01 | Empty list on a fresh database | 200, `[]` |
| T-CONV-02 | Most recently updated conversation first | Writing in an older conversation moves it to the top |
| T-CONV-03 | `limit` parameter validated | 0 → 422, 201 → 422, 1 → 200 |
| T-CONV-04 | Unknown conversation | 404 |
| T-CONV-05 | Delete removes conversation and messages (cascade) | 204, then GET → 404, second DELETE → 404 |
| T-HEALTH-01 | Health reports database and AI engine | `database=up`, `ollama=up`, version |
| T-UI-01 | Web UI served at `/` | 200, page contains "Nuage Libre" |
| T-UI-02 | Every CSS / JS / font file referenced by the page is served | Each `/static/...` URL found in the HTML → 200 with the right content type |
| T-SEC-04 | Sovereignty: the UI loads nothing from the internet | No `http(s)://` or `//` URL in `src`/`href` of the page nor in `url(...)` of our stylesheets |

### Model selection (`tests/test_models.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-MOD-01 | Menu lists chat models, smallest first, default flagged | `["qwen3:4b", <default>]`, `is_default` only on the default |
| T-MOD-02 | Embedding-only models are hidden | `nomic-embed-text` absent |
| T-MOD-03 | Ollama down | `GET /api/models` → 503 |
| T-MOD-04 | Requested model is used and stored | Ollama called with `qwen3:4b`; conversation and answer record it; user message `model = null` |
| T-MOD-05 | No model sent on a new chat | Default model used |
| T-MOD-06 | Switching model mid-conversation | Calls `[default, qwen3:4b, qwen3:4b]`; each answer stores its own model; next message without model continues with the latest |
| T-MOD-07 / T-SEC-02 | Model not installed (allow-list) | 422 "This model is not installed."; Ollama never called |
| T-MOD-08 | Installed but embedding-only model | 422; Ollama never called |

### Multi-user foundation (`tests/test_users.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-USER-01 | The Ghost User is seeded by the migration with the hard-coded id | Row `ghost` exists with `GHOST_USER_ID`; the UUID frozen in the migration equals the one in `app/auth.py` |
| T-USER-02 | Without authentication, new data belongs to the Ghost User | `conversations.user_id` and `files.user_id` = `GHOST_USER_ID` |
| T-USER-03 / T-SEC-09 | IDOR: another user's conversation is unreachable | As a second user: list empty, GET 404, continue 404, DELETE 404; the Ghost User's conversation untouched |
| T-USER-04 / T-SEC-09 | IDOR: another user's file is unreachable | List empty; download 404, content not leaked |
| T-USER-05 | "Fail closed": no data without owner | `INSERT INTO conversations` without `user_id` → `IntegrityError` (NOT NULL) |
| T-USER-06 | Deleting a user: conversations cascade, files protected | Conversations and messages deleted; user owning a file → `IntegrityError` on `fk_files_user_id_users` (RESTRICT) |
| T-USER-07 | Usernames are unique | Second `ghost` → `IntegrityError` on `uq_users_username` |

### Private cloud (`tests/test_files.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-FILE-01 | Upload stores the bytes and indexes them | 201 + `Location`; bytes at `<root>/<user>/<id>`, mode 600; row with a *relative* path; `is_vectorized=false`; no `file_path`/`user_id` in the JSON; no `.part` left |
| T-FILE-02 | Empty file accepted | 201, size 0 |
| T-FILE-03 | MIME type decided by the server | `photo.PNG` → `image/png`, `Makefile` → `application/octet-stream`, `backup.tar.gz` → `application/gzip` |
| T-FILE-04 | Name cleaned | `../../etc/pass\r\nwd<U+202E>txt.exe` → `passwdtxt.exe`; NFD → NFC |
| T-FILE-05 | Invalid requests leave no trace | `%FF%FE`, `..%2F`, `%2E%2E` → 422; `text/plain` body → 415; 0 file, 0 row |
| T-FILE-06 | `X-File-Name` required | 422 |
| T-FILE-07 | Multipart refused (CSRF by design) | 415, 0 row |
| T-FILE-08 | `Content-Length` required | Chunked body → 411 |
| T-FILE-09 | Size limit before reading | limit 10: 11 bytes → 413 "the limit is 10 B", 10 bytes → 201 |
| T-FILE-10 | Free-disk reserve | Reserve of 10¹⁸ bytes → 507, nothing written |
| T-FILE-11 | Compensation when the INSERT fails | Unknown owner (FK fails) → 500 generic message, bytes deleted, details in the logs |
| T-FILE-12 | Interrupted upload | Stream raising mid-way → no file, no row |
| T-FILE-13 | Limit enforced while streaming | No declared size, 10 bytes for a limit of 8 → `UploadTooLargeError`, nothing written |
| T-FILE-14 | Truncated body | Declared 1000, received 4 → `IncompleteUploadError`, nothing kept |
| T-FILE-15 | List | Newest first; no `file_path`; `limit` 0 / 1001 → 422 |
| T-FILE-16 | Download | 3 MiB of random bytes identical; `attachment; filename*=utf-8''donn%C3%A9es%20%C3%A9t%C3%A9.bin`; `nosniff`, CORP, `no-referrer`, `no-store`, CSP `sandbox` |
| T-FILE-17 / T-SEC-06 | Stored XSS: inline only for script-free types | PNG inline `image/png`; SVG → attachment; HTML inline as `text/plain`; all sandboxed + `nosniff` |
| T-FILE-18 | PDF viewable | Inline `application/pdf`, no CSP (viewer), `nosniff` kept |
| T-FILE-19 | Malformed ids | Unknown → 404; `1 OR 1=1` → 422; `..%2F..%2Fetc%2Fpasswd` → 404 |
| T-FILE-20 | Bytes deleted behind the app's back | 404 + "missing on disk" in the logs |
| T-SEC-07 | Path traversal, database layer | `UPDATE files SET file_path` to `/etc/passwd`, `../../../etc/passwd`, `<uuid>/../../../etc/passwd` → `IntegrityError` (`ck_files_path_relative`) |
| T-SEC-08 | Path traversal, storage layer | Bytes replaced by a symlink to `/etc/passwd` → 404, no `root:` in the body, error logged |

### File rules and storage (`tests/test_file_policy.py`, `tests/test_file_storage.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-POL-01 | Name cleaning (8 cases) | Path parts, CR/LF, NUL, TAB, RTLO removed; spaces stripped; emoji and accents kept |
| T-POL-02 | macOS decomposed accents | `e + U+0301` (NFD, 9 code points) → `é` (NFC, 7) |
| T-POL-03 | Empty names refused (8 cases) | `""`, `"."`, `".."`, `"../"`, `"a/.."`, CR/LF only, RTLO only → `InvalidFileNameError` |
| T-POL-04 | Long names | 400 + `.pdf` → 255 characters, extension kept |
| T-POL-05 | MIME types (8 cases) | From the extension, case-insensitive; compression → real type; unknown → `application/octet-stream` |
| T-POL-06 / T-SEC-06 | Inline allow-list (11 cases) | Images, PDF, media as-is; text/HTML/JSON → `text/plain`; SVG, XHTML, ZIP, binary → never inline |
| T-POL-07 | Sandbox | Everything sandboxed except PDF, audio, video (built-in viewers) |
| T-STO-01 / T-SEC-08 | Containment (5 cases) | `/etc/passwd`, `../../../etc/passwd`, `user/../../outside`, `""`, `"."` → `UnsafePathError` |
| T-STO-02 / T-SEC-08 | Symlink escape | `user/trap → /etc/passwd` → `UnsafePathError` |
| T-STO-03 | Normal layout | `<uuid>/<uuid>` resolves inside the root |
| T-STO-04 | Atomic, private write | 3000 small chunks (≈ 3 MiB) re-assembled exactly; file 600, folder 700; no `.part` left |
| T-STO-05 | Failed write | Stream raising half-way → folder empty |
| T-STO-06 | Free space before the first upload | Measured on the nearest existing parent, without creating anything |

### Ollama client (`tests/test_ollama_client.py`)

| ID | Requirement | Expected result |
|---|---|---|
| T-OLL-01 | Parse reasoning, content and final statistics | Chunks split correctly; 2 500 000 000 ns → 2500 ms |
| T-OLL-02 | Request carries model and context size | `options.num_ctx` sent, `stream=true` |
| T-OLL-03 | HTTP error (unknown model) | `OllamaError` with status and message |
| T-OLL-04 | Error in the middle of the stream (GPU OOM) | `OllamaError("out of memory")` |
| T-OLL-05 | Stream cut before completion | `OllamaError`, not a silently truncated answer |
| T-OLL-06 | Ollama not running | `OllamaError` "Is the container running?" |
| T-OLL-07 | Corrupted line in the stream | Line skipped, rest of the answer kept |

## 3. Results

Run on 2026-11-12 (Python 3.15.0, PostgreSQL 18, pytest 9.2):

```
115 passed in 0.93s
```

(35 tests in v0.4; v0.5 adds 80: 7 multi-user, 25 private cloud, 38 file rules,
10 storage.)

### Checking that the tests can actually fail

A test suite that always passes proves nothing. Two bugs were deliberately
introduced to check that the tests detect them (mutation testing, done by hand):

| Injected bug | Detected by |
|---|---|
| History limit off by one (`max_history_messages` instead of `- 1`) | ❌ `T-CHAT-04` failed (prompt had 4 messages) |
| Exchange saved even when the model fails (removed the `return` after the error) | ❌ `T-CHAT-05` failed (rows written to DB) |

Both bugs were reverted afterwards and the suite went back to green.

The same exercise was repeated for the security properties of v0.5 (2026-11-12),
each bug injected alone, then reverted:

| Injected bug | Detected by |
|---|---|
| Ownership filter removed from the file download query (`WHERE id = ?` only) | ❌ `T-USER-04` (the second user could download the file) |
| Containment check removed from `FileStorage.resolve()` | ❌ 7 tests, including `T-SEC-08`, which then really received `/etc/passwd` through the planted symlink |
| Inline allow-list bypassed (any type displayed inline) | ❌ `T-FILE-17` (SVG served inline) |
| Compensation removed (bytes kept when the INSERT fails) | ❌ `T-FILE-11` (orphan file left on disk) |

The same check was done for `T-SEC-04`: adding a Google Fonts `<link>` to the
page made the test fail (`assert ['https://fon...amily=Nunito'] == []`).

## 4. End-to-end test with the real model

Performed on 2026-11-09 with Firefox (headless, driven by a WebDriver BiDi script)
on the real stack (Ollama + Qwen3.8 27B Q4_K_M on the RX 7900 XTX, PostgreSQL).

| Step | Expected | Observed |
|---|---|---|
| Open the UI | Model name in the header, green status dot | ✅ `Qwen3.8-27B-…:Q4_K_M`, "AI engine up, database up" |
| Type a question and press Enter | Reasoning appears live, then the answer replaces it | ✅ Reasoning panel open during thinking, folded when the answer started |
| End of answer | Stats under the answer, conversation in the sidebar | ✅ `111 tokens · 3.7 s · 30.1 tok/s`; sidebar count 1 → 2 |
| Reload the page and click the conversation | Messages restored from the database | ✅ 2 messages restored |
| Mobile width (390 px), open the menu | Off-canvas sidebar | ✅ |
| Two-turn conversation via the API | The model remembers the first turn | ✅ "Comment s'appelle mon projet ?" → "Nuage Libre" |

A bug was found and fixed during this test: a literal closing script tag written
inside a JavaScript comment ended the `<script>` element early, so the whole
script was displayed as text. Unit tests could not catch this; only rendering
the page in a real browser did.

## 5. End-to-end test: model menu and Markdown (v0.3)

Same tooling (headless Firefox driven by WebDriver BiDi), real stack, 2026-11-09.

| ID | Step | Expected | Observed |
|---|---|---|---|
| E2E-MOD-01 | Open the UI | Menu lists installed chat models, default pre-selected | ✅ 7 models from `llama3.2` (2.0 GB) to Q6_K (22.7 GB); 27B Q4_K_M selected |
| E2E-MOD-02 | Pick `qwen3:4b` and send a message | Answer by that model; choice remembered | ✅ footer `qwen3:4b · 1927 tokens · 12.8 s · 150.3 tok/s`; saved in localStorage |
| E2E-MD-01 | Ask for heading, bold, inline code, list, table, Python block | Rendered as HTML, no raw Markdown left | ✅ 1 heading, 1 bold, 1 inline code, 2 list items, 1 table (4 cells), 1 highlighted Python block with *Copy* button |
| E2E-MD-02 | Click *Copy* with a real (trusted) mouse click | Code copied | ✅ "Copied!" |
| E2E-MOD-03 | Reload with another model remembered, open the conversation | Menu switches to the conversation's model; Markdown re-rendered | ✅ `qwen3:4b` re-selected; same rendering |
| E2E-MD-03 | Mobile width (390 px) | Table and code fit, no horizontal page scroll | ✅ |
| T-SEC-03 | Render `<img src=x onerror=…>`, `[x](javascript:…)`, `<script>`, `<iframe>` through `renderMarkdown()` | Nothing executes; formatting kept | ✅ `onerror` removed, `href` removed, script and iframe removed, `**ok**` still bold; no payload executed |

Note: the first *Copy* attempt reported "Failed" because a click simulated from
JavaScript is not a real user gesture, so the browser refuses clipboard access.
This led to a more robust implementation (fallback to `execCommand('copy')` when
the Clipboard API is refused), then the real-click test above passed.

## 6. End-to-end test: visual identity and themes (v0.4)

Same tooling, real stack, 2026-11-09. Screenshots taken for each theme at
1280 × 820 (desktop) and 390 × 844 (mobile).

| ID | Step | Expected | Observed |
|---|---|---|---|
| E2E-TH-01 | Open the UI with each saved theme (day / night / pro) | Right sky, colours and bubbles; no JavaScript error | ✅ 3 × 2 screenshots; console empty; Nunito loaded (`document.fonts.check`) |
| E2E-TH-02 | Real mouse click on the night option | Theme switches with a circular reveal, choice saved, `aria-checked` updated | ✅ `data-theme=night`, `localStorage=night`, `aria-checked` = `false,true,false` |
| E2E-TH-03 | Keyboard: focus the switcher, press → | Next theme selected, focus follows | ✅ `pro`, focus on the *pro* option |
| E2E-TH-04 | Send a message with `qwen3:4b` in the night theme | Typing indicator, mascot bounces, answer rendered (code + table) | ✅ `is-generating` set during generation; 1 code block with *Copy*, 1 table; `151.9 tok/s` |
| E2E-TH-05 | Mobile: open the menu, click the backdrop | Sidebar covers the content, closes on backdrop click | ✅ after fix (see below) |

Two defects found and fixed during this test:

- **Mobile menu see-through**: the sidebar used a translucent "frosted glass"
  background; behind it the large welcome title stayed readable, even at 96 %
  opacity, because the blur is not rendered everywhere. The mobile menu now uses
  an opaque background.
- **Table border**: tables are `display: block` (to scroll on small screens),
  so their rounded border spanned the whole bubble while the cells did not.
  Fixed with `width: max-content; max-width: 100%`.

## 7. End-to-end test: LaTeX formulas (v0.4.1)

Same tooling. A test answer exercising every case was passed through the exact
pipeline of `renderMarkdown()` (`DOMPurify.sanitize(marked.parse(md))`) inside a
real AI bubble, in the day and night themes and at 390 px; then a real question
was asked to `qwen3:4b`.

| ID | Case | Expected | Observed |
|---|---|---|---|
| E2E-MATH-01 | `$e^{i\pi}+1=0$`, `\(a_1 * b_1\)`, `$$\int…$$`, multi-line `\[ pmatrix … \]` | All typeset; no Markdown italics inside formulas | ✅ 11 formulas, 2 displayed; 0 `<em>` |
| E2E-MATH-02 | Formula in a list item and in a table cell | Typeset | ✅ |
| E2E-MATH-03 | `It costs $5 and $10`, escaped `\$x\$`, `` `$x$` ``, `$` in a code block | Left as text | ✅ |
| E2E-MATH-04 | Invalid formula, unknown command | Shown in the error colour with its source, rest of the answer intact | ✅ (after fix: KaTeX's dark red was unreadable on the night sky → `errorColor` set to the theme variable) |
| E2E-MATH-05 | Macro bomb `\def\a{\a\a}\a` | Error message, browser not frozen | ✅ "Too many expansions"; whole document rendered in 15 ms |
| T-SEC-05 | `\href{javascript:…}`, `\url{javascript:…}`, `\htmlClass`, `\text{<img onerror>}`, `$x$<img onerror>` | Nothing executes, no `javascript:` link, no event handler | ✅ `window.__xss` undefined; 0 `onerror`; 0 `href` |
| E2E-MATH-06 | MathML for screen readers | MathML present, raw TeX not duplicated | ✅ 11 `<math>`, 0 `<annotation>` |
| E2E-MATH-07 | Real model, streaming (quadratic formula) | Formulas typeset, no raw `$` left, no JS error | ✅ after fix (below): 6 formulas; console empty |

Defects found and fixed:

- **Spaced inline formulas**: `qwen3:4b` wrote `$ ax^2 + bx + c = 0 $`, which the
  strict Pandoc rule rejected (raw text shown). A second rule now accepts a space
  on *both* sides; `from $ 5 to $10` is still not a formula (re-tested).
- **Unreadable code while streaming** (from the v0.4 redesign): before
  highlight.js runs at the end of the answer, code inherited the dark text colour
  of the light themes on the dark code block. Code blocks now set a light text colour.

## 8. End-to-end test: private cloud and split-screen (v0.5)

Performed on 2026-11-12 with Firefox 160 (headless, driven by a WebDriver BiDi
script) against a separate instance of the application (port 8010) connected to
the **test** database and a scratch storage folder, so no real data was touched.
The chat used the real model (`qwen3:4b` on the GPU). Screenshots at 1440 × 900
and 390 × 844, in the three themes.

| ID | Step | Expected | Observed |
|---|---|---|---|
| E2E-CLOUD-01 | Open the UI (first visit) | Split view 50/50, empty cloud with mascot, no JavaScript error | ✅ panels 689 px / 689 px; console empty |
| E2E-CLOUD-02 | Drop 5 files on the panel (PNG with emoji and accents, PDF, Markdown, 2.5 MB ZIP, HTML) | Drag-over overlay, sequential upload, rows in the list | ✅ overlay visible on `dragenter`; 5 rows, badges PNG/PDF/MD/ZIP/HTML; "5 files · 2.5 MB" |
| E2E-CLOUD-03 | View actions | "View" only for previewable types | ✅ shown for PNG, PDF, MD, HTML; absent for ZIP |
| E2E-CLOUD-04 | Real mouse click on "Chat" | Files panel glides shut, becomes `inert`; history becomes a column | ✅ mid-animation 169 px / 1224 px, then 0 / 1396; history `position: relative` |
| E2E-CLOUD-05 | Keyboard: ← twice from the focused switcher | Split, then Files; focus follows; choice saved | ✅ `aria-checked` and `localStorage` updated |
| E2E-CLOUD-06 | Focus the chat textarea while the chat panel is hidden | Impossible | ✅ focus stays on the switcher (`inert`) |
| E2E-CLOUD-07 | Reload | Saved view applied before first paint | ✅ `data-view="files"` |
| E2E-CLOUD-08 | RAG switch | `aria-checked`, remembered, honest status line | ✅ "RAG on · no document in the AI memory yet…"; `localStorage = on` |
| E2E-CLOUD-09 | History drawer in split view, then Escape | Drawer visible over the chat, closed by Escape, focus back on its button | ✅ |
| E2E-CLOUD-10 | "Add to AI memory" | Inactive, explains itself | ✅ notice "arrives with RAG (v0.7)…" |
| E2E-CLOUD-11 | Mobile 390 px | "Split" not offered, falls back to chat; no horizontal scroll; preference kept | ✅ split button `display: none`; back to 1440 px restores the saved view |
| E2E-CLOUD-12 | View a real PNG (240 × 140) | Rendered under the CSP sandbox | ✅ `naturalWidth = 240` |
| E2E-CLOUD-13 | View a real PDF | Rendered by pdf.js | ✅ 1 page, PDF.js 6.5 |
| E2E-CLOUD-14 / T-SEC-06 | View `page.html` containing `<script>alert(1)</script>` | Source shown as text, nothing executed | ✅ `text/plain`, 0 script |
| E2E-CLOUD-15 | Real chat with `qwen3:4b` in split view | Answer streamed and saved for the Ghost User | ✅ "qwen3:4b · 278 tokens · 5.3 s · 52.4 tok/s"; 1 conversation listed |
| E2E-CLOUD-16 | Cancel a 400 MB upload at 10 % | Row removed, nothing listed, no partial file on disk, no server error | ✅ cancelled at "10% · 41.7 MB of 400 MB"; storage folder without any large or `.part` file; no stack trace |
| E2E-CLOUD-17 | Upload over the limit (server started with a 1 MB limit) | The browser receives the 413 message although it is still sending; the next file uploads | ✅ "File too large: the limit is 976.6 KiB." (`role="alert"`), then `small.txt` uploaded |

Defects found and fixed during this test:

- **History buttons visible next to the history column** (chat in full screen):
  the container-query rule hiding them came *before* `.icon-btn { display: grid }`
  in the stylesheet, with the same specificity, so it lost. Fixed with a more
  specific selector (`.topbar .sidebar-toggle`).
- **Uneven file rows in split view**: the text labels of "View/Download" pushed
  the details onto a second line on some rows. They are now icon-only below 820 px
  of *panel* width (accessible names unchanged); "Add to AI memory" keeps its words.
- **Phone wording**: "Drop your files here" on a touchscreen became "Tap to add
  files" (`pointer: coarse`); the default "Cloud only" badge is hidden in very
  narrow panels.
- **Audio/video under the CSP sandbox**: could not be verified because this
  headless Firefox does not open audio documents at all, even without any
  security header (checked with a separate test server serving the same WAV with
  and without the policy). Rather than gamble, audio and video were excluded from
  the sandbox like PDF (they are decoded by the browser's own player and cannot
  run scripts; `nosniff` still applies).

Also found by reading the code during this work: the closed mobile history
drawer was only moved off-screen, so the Tab key still walked through its
invisible links. It is now `visibility: hidden` when closed.

## 9. Not covered yet

- Automated browser tests in CI (Playwright) — planned with the CI pipeline (v0.9).
- Load testing (several concurrent generations, several concurrent uploads).
- Automated colour-contrast audit of the three themes (planned with the RGAA audit, v0.8).
- Authentication tests (login, token expiry) — with v0.6; the authorisation
  logic (isolation between users) is already tested (T-USER-03/04).
- Inline audio/video in desktop browsers (manual check), and the UI in Chrome
  and Safari.
