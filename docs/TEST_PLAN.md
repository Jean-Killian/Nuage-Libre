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
| T-UI-03 | The interface is revalidated, never reused stale after an update | Page and `/static` send `Cache-Control: no-cache` and an ETag; `If-None-Match` → 304 |
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
| T-POL-08 | Folder name cleaning (7 cases) | Spaces stripped, NFD → NFC, TAB and RTLO removed; `.hidden` and 255 characters accepted |
| T-POL-09 | Folder names refused (7 cases) | Empty, blank, `.`, `..`, `Courses/2026`, `C:\Users`, 256 characters → `InvalidFolderNameError` with the reason |
| T-POL-10 | Free name on conflict | `Photos` → `Photos (2)` → `Photos (3)` (gaps filled); a 255-character name keeps its suffix and length |
| T-STO-01 / T-SEC-08 | Containment (5 cases) | `/etc/passwd`, `../../../etc/passwd`, `user/../../outside`, `""`, `"."` → `UnsafePathError` |
| T-STO-02 / T-SEC-08 | Symlink escape | `user/trap → /etc/passwd` → `UnsafePathError` |
| T-STO-03 | Normal layout | `<uuid>/<uuid>` resolves inside the root |
| T-STO-04 | Atomic, private write | 3000 small chunks (≈ 3 MiB) re-assembled exactly; file 600, folder 700; no `.part` left |
| T-STO-05 | Failed write | Stream raising half-way → folder empty |
| T-STO-06 | Free space before the first upload | Measured on the nearest existing parent, without creating anything |

### Folders (`tests/test_folders.py`, v0.5.1)

| ID | Requirement | Expected result |
|---|---|---|
| T-FOLD-01 | Create a folder at the root | 201 + `Location`; no `user_id` in the JSON; nothing written on disk |
| T-FOLD-02 | Path of a folder (breadcrumb) | `Courses / 2026 / Maths`, ids in order, root first |
| T-FOLD-03 | List sub-folders | Root lists only its children, by name as a person expects (`Apple`, `banana`, `cherry`) |
| T-FOLD-04 | Duplicate names | Same name at the root → 409 with a clear message; same name in another folder → 201 |
| T-FOLD-05 | `on_conflict = "rename"` | `Photos (2)`, then `Photos (3)` |
| T-FOLD-06 | Names cleaned or refused (8 cases) | Stripped, NFC, RTLO removed; blank, `..`, `/`, `\`, 256 characters → 422 |
| T-FOLD-07 | Depth limit | 32 levels accepted, the 33rd → 422 naming the limit |
| T-FOLD-08 | Unknown parent | 404, nothing created |
| T-FOLD-09 / T-SEC-10 | IDOR on folders | As another user: detail, listing, parent, file listing and upload into the folder → 404; nothing created, nothing on disk; the other user's own root is empty and independent |
| T-FOLD-10 | Upload into a folder | Listed in the folder, not at the root; bytes still at `<user>/<file>` on disk |
| T-FOLD-11 | Upload into an unknown folder | 404 before the body is read: no file, no row |
| T-FOLD-12 | Summary of the cloud | Counts every folder: 3 files, 2 folders, 9 bytes |
| T-FOLD-13 | Summary of an empty cloud | All zeros (no NULL from `SUM`) |
| T-SEC-11 | Composite foreign keys | Direct SQL: a folder or a file in another user's folder → `IntegrityError` |
| T-FOLD-14 | Deleting folders | Empty tree: sub-folders cascade; tree holding a file deep down → `IntegrityError` (RESTRICT) |
| T-SEC-12 | Bounded recursion | Cycle made by hand (A in B in A): the folder still answers at once, path cut at 32 |
| T-FOLD-15 | No folder is its own parent | `IntegrityError` on `ck_folders_not_own_parent` |
| T-FOLD-16 | Deleting a user deletes their folders | CASCADE (folders are metadata only) |
| T-FOLD-17 | Unique names at the root, enforced by the database | Direct SQL duplicate at the root → `IntegrityError` (`NULLS NOT DISTINCT`) |
| T-FOLD-18 | Two requests racing for the same name | The service check is bypassed (simulated race): the `UNIQUE` constraint answers, reported as 409, still one folder |
| T-SEC-13 | CSRF on folder creation | `text/plain` body (what another site can send without a preflight) → 422, nothing created |

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
159 passed in 1.87s
```

(35 tests in v0.4; v0.5 adds 80: 7 multi-user, 25 private cloud, 38 file rules,
10 storage; v0.5.1 adds 44: 28 folders, 15 folder-name rules, 1 interface cache.)

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

And for the folders of v0.5.1 (2026-11-12):

| Injected bug | Detected by |
|---|---|
| No folder check before an upload | ❌ `T-FOLD-09` (500 from the foreign key instead of a clean 404) |
| Folder lookup without the owner filter | ❌ `T-FOLD-09` (the other user could create inside the folder) |
| Depth limit off by one | ❌ `T-FOLD-07` |
| `on_conflict = "rename"` ignored | ❌ `T-FOLD-05` |
| `NULLS NOT DISTINCT` removed from the **migration** (database downgraded, mutated migration applied, then restored) | ❌ `T-FOLD-17` and `T-FOLD-18` |
| Race not mapped to 409 (`IntegrityError` re-raised) | ❌ `T-FOLD-18` (500) |
| Root condition written `folder_id = NULL` instead of `IS NULL` | ✅ survived, and rightly so: SQLAlchemy already turns `column == None` into `IS NULL`. An *equivalent mutant* (same behaviour), not a gap in the tests; the docstring of `in_folder()` was corrected accordingly |

This exercise also revealed a real gap: before T-FOLD-17 and T-FOLD-18 were
written, the service's own name check hid the database constraint, so no test
proved that the constraint existed at all.

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

## 9. End-to-end test: folders, follow scrolling and soft glows (v0.5.1)

Performed on 2026-11-12 with Firefox 160 (headless, WebDriver BiDi) against a
separate instance (port 8010) on the **test** database. For the scrolling
tests, Ollama was replaced by a small fake server that streams 260 reasoning
tokens then about 1,000 answer tokens at 33 tokens per second, so every run
is identical. Wheel, keyboard and mouse events are real input events
(`input.performActions`), not JavaScript calls; the browser cache is bypassed.

**Follow scrolling (13 checks, all ✅)**

| ID | Step | Expected |
|---|---|---|
| E2E-SCROLL-01 | Send a message; reasoning streams | The reasoning box follows its text (at the bottom) |
| E2E-SCROLL-02 | Wheel up inside the reasoning box | The box stays where it was put while the text grows; the chat keeps following |
| E2E-SCROLL-03 | Wheel down to the bottom of the box | The box follows again |
| E2E-SCROLL-04 | Answer streams | The chat stays at the bottom (6 samples, distance ≤ 1 px) |
| E2E-SCROLL-05 | **One** wheel notch up during the stream (the reported bug) | The view does not move any more (scrollTop identical 2 s later) while the content grows below |
| E2E-SCROLL-06 | — | "Latest" button shown, glowing (`is-live`) |
| E2E-SCROLL-07 | Click "Latest" | Back at the bottom, following again, button hidden |
| E2E-SCROLL-08 | Wheel up, then wheel down to the bottom by hand | Following resumes |
| E2E-SCROLL-09 | Page Up in the focused history | Following stops |
| E2E-SCROLL-10 | Click (press 150 ms) in an empty area of the history | Following continues |
| E2E-SCROLL-11 | End of the answer while scrolled up | Button visible, no longer glowing |
| E2E-SCROLL-12 | "Latest" activated with the keyboard | Focus moves to the message field, not lost |
| E2E-SCROLL-13 | Console | No JavaScript error |

**Folders (23 checks, all ✅)**

| ID | Step | Expected |
|---|---|---|
| E2E-FOLD-01 | Open the cloud | No breadcrumb at the root; empty state |
| E2E-FOLD-02 | "New folder" | Inline form, "New folder" pre-selected (typing replaces it) |
| E2E-FOLD-03 | Type "Courses", Enter | Row created; form closed; focus on the new row; summary "1 folder · 0 files" |
| E2E-FOLD-04 | Same name again | Server message in the form, `aria-invalid="true"` |
| E2E-FOLD-05 | Escape | Form closed, focus back on "New folder" |
| E2E-FOLD-06 | Name `a/b` | The server's reason shown |
| E2E-FOLD-07 | Enter on the folder row | Breadcrumb "All files › Courses", `#folder=<id>` in the address, focus on the current step, "This folder is empty", drop zone says "stored in “Courses”" |
| E2E-FOLD-08 | Upload a file with the real file picker | Listed in Courses (API: in the folder, not at the root) |
| E2E-FOLD-09 | Folder picker result (`webkitRelativePath`) with a `.DS_Store` | Tree recreated, 3 files, `.DS_Store` ignored |
| E2E-FOLD-10 | Drop a folder of the same name with 232 files, 230 of them in one sub-folder, and an empty sub-folder | Renamed "Photos (2)"; all 232 files uploaded (proves the batched `readEntries()` loop); empty sub-folder created |
| E2E-FOLD-11 | Listing | Folders first, by name, then files |
| E2E-FOLD-12 | Server side | Tree identical to the dropped folder |
| E2E-FOLD-13 | Open 3 levels down | Breadcrumb "All files › Courses › Photos (2) › 2026 › deep" |
| E2E-FOLD-14 / 15 | Browser Back, then Forward | Up one level, then down again |
| E2E-FOLD-16 | Reload | Still in the folder |
| E2E-FOLD-17 | "All files" | Root, address cleaned, focus on the panel title |
| E2E-FOLD-18 | Unknown folder id typed in the address | Notice, back to the root |
| E2E-FOLD-19 | Dropped folder with an unreadable file | The folder's row: "1 item could not be uploaded. Locked/secret.txt: This file could not be read from your computer." |
| E2E-FOLD-20 | Cancel a folder upload in the middle of a 300 MB file | Row removed; notice "0 files had already been saved"; no `.part` file left on disk |
| E2E-FOLD-21 | Header summary | Equal to the server totals over every folder |
| E2E-FOLD-22 | Split view | "New folder" / "Upload folder" become icons; summary on one line |
| E2E-FOLD-23 | Console | No JavaScript error |

Driver limitation: WebDriver BiDi cannot give a *directory* to an
`<input webkitdirectory>` in this Firefox ("Is directory"), and a real drag
from the desktop cannot be scripted. E2E-FOLD-09 therefore fires the input's
`change` event with the exact list a browser produces (File objects carrying
`webkitRelativePath`), and E2E-FOLD-10/19/20 dispatch a `drop` event carrying
`FileSystemEntry`-like objects (with a `readEntries()` that returns at most
100 entries per call, like Chromium). Everything after the browser's own
dialog or drag (reading, folder creation, uploads, errors, cancel) is the
real code against the real server.

**Soft glows**: screenshots at device pixel ratio 2 of the New chat button,
the RAG switch, the composer and the Upload button, in the day and night
themes, before and after the fix. The luminance was read pixel by pixel along
a line crossing the panel's edge: above the RAG switch (night) it jumped from
29 to 47 in one pixel row before, and now rises gradually from 29 to 40;
above "New chat", from 25 to 35 before, gradual after.

**Regression**: the v0.5 layout script (E2E-CLOUD-04 to 11: views, keyboard,
`inert`, drawer, mobile) was run again with identical results; the history
drawer and the closing panel were captured in the middle of their animation:
nothing shows beyond the edge of the chat panel.

Defects found and fixed during this test:

- **Stale interface after an update**: the test browser kept the previous
  `app.css` (no `Cache-Control` header, heuristic caching) while loading the
  new scripts. Fixed with `Cache-Control: no-cache` (T-UI-03, ARCHITECTURE §4.16).
- **Hard line at the bottom of the history** when scrolled up: the top edge
  already faded, the bottom did not; both edges now fade.
- **Summary on two lines in split view** with three header buttons: the
  secondary buttons become icons below 860 px of panel width.

## 10. Not covered yet

- Automated browser tests in CI (Playwright) — planned with the CI pipeline (v0.9).
- Load testing (several concurrent generations, several concurrent uploads).
- Automated colour-contrast audit of the three themes (planned with the RGAA audit, v0.8).
- Authentication tests (login, token expiry) — with v0.6; the authorisation
  logic (isolation between users) is already tested (T-USER-03/04).
- Inline audio/video in desktop browsers (manual check), and the UI in Chrome
  and Safari.
- The native folder dialog and a real drag from the desktop (see the driver
  limitation in §9), and touch scrolling on a real phone.
