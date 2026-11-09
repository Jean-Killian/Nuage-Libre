# Test plan

## 1. Strategy

| Level | What is tested | Tools | Real or fake dependencies |
|---|---|---|---|
| Unit | `OllamaClient`: parsing of Ollama's NDJSON protocol, error handling | pytest, `httpx.MockTransport` | Network replaced by hand-written responses |
| Integration | HTTP routes → service → repository → **real PostgreSQL** | pytest, pytest-asyncio, `httpx.ASGITransport` | Real test database; Ollama replaced by `FakeOllama` |
| Security | SQL injection, input validation, id format | same as integration | — |
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

Run on 2026-11-09 (Python 3.15.0, PostgreSQL 18, pytest 9.2):

```
33 passed in 0.34s
```

### Checking that the tests can actually fail

A test suite that always passes proves nothing. Two bugs were deliberately
introduced to check that the tests detect them (mutation testing, done by hand):

| Injected bug | Detected by |
|---|---|
| History limit off by one (`max_history_messages` instead of `- 1`) | ❌ `T-CHAT-04` failed (prompt had 4 messages) |
| Exchange saved even when the model fails (removed the `return` after the error) | ❌ `T-CHAT-05` failed (rows written to DB) |

Both bugs were reverted afterwards and the suite went back to green.

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

## 6. Not covered yet

- Automated browser tests in CI (Playwright) — planned with the CI pipeline (v0.6).
- Load testing (several concurrent generations).
- Authentication and authorisation tests — with v0.3.
