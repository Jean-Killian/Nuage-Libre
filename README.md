# ☁️ Nuage Libre

**Nuage Libre** ("Free Cloud") is a self-hosted personal cloud and private AI assistant.
It turns a gaming PC (AMD RX 7900 XTX, 24 GB VRAM) into a 24/7 server that I can
reach from any of my devices (MacBook, Android phone) through a private
Tailscale network. Large language models run **locally on the GPU**: no prompt,
document or answer ever leaves the machine.

> School project — Bachelor *Concepteur Développeur d'Applications* (RNCP 37873), 2026‑2027.

---

## Table of contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Tech stack](#tech-stack)
4. [Getting started](#getting-started)
5. [Configuration](#configuration)
6. [API reference](#api-reference)
7. [Tests](#tests)
8. [Project structure](#project-structure)
9. [Security](#security)
10. [Roadmap](#roadmap)
11. [Detailed documentation](#detailed-documentation)

---

## Features

**Available (v0.4)**

- 💬 **Chat with a local LLM** (default: Qwen 3.8 27B, Q4_K_M quantization) with the
  answer streamed token by token.
- 🔀 **Model menu**: switch between every installed model (e.g. `qwen3:4b` for fast
  answers, 27B for quality), even in the middle of a conversation. The choice is
  remembered, and each answer records which model wrote it.
- 📝 **Markdown rendering**: headings, lists, tables, links, inline code and code
  blocks with syntax highlighting and a *Copy* button. Sanitised against XSS.
- 🧠 **Reasoning display**: the model's "thinking" is shown live in a collapsible
  panel, then folded away when the final answer starts.
- 💾 **Persistent conversations** stored in PostgreSQL: list, reopen, continue and
  delete past chats from a sidebar.
- 🔁 **Conversation memory**: the recent history is sent back to the model at
  each turn (bounded, to stay inside the context window).
- 📊 **Generation metrics** saved per answer (tokens, duration, tokens/s).
- 🩺 **Health check** of the AI engine and the database (status dot in the UI).
- 📱 **Responsive UI** (desktop sidebar / mobile off-canvas menu).
- ☁️ **Own visual identity**: a kawaii cloud mascot, cloud-shaped chat bubbles and
  **three themes**: *Ciel de Jour* (living dawn sky with drifting clouds),
  *Ciel de Nuit* (twinkling star field with parallax and shooting stars) and
  *Mode Pro* (clean, minimal). Switch with a circular reveal animation; the
  choice is remembered. Respects the OS "reduce motion" setting.
- 🔒 **Zero third-party request**: libraries and fonts are served locally.

**Planned**: authentication, document upload and RAG (the model answers using my
own files), deployment pipeline. See the [Roadmap](#roadmap).

---

## Architecture

The backend follows a **layered architecture**: each layer has one
responsibility and only talks to the layer directly below it.

```
 Browser (MacBook / phone, via Tailscale)
   │  HTTP + JSON / NDJSON stream
   ▼
┌──────────────────────────────────────────────────────────────┐
│ Presentation layer   app/routers/        HTTP, validation     │
│                      app/schemas.py      (Pydantic DTOs)      │
├──────────────────────────────────────────────────────────────┤
│ Business layer       app/services/       chat logic,          │
│                                          Ollama client        │
├──────────────────────────────────────────────────────────────┤
│ Data access layer    app/repositories/   all SQL queries      │
│                      app/models.py       (SQLAlchemy ORM)     │
└──────────────────────────────────────────────────────────────┘
   │ asyncpg (pool)                         │ HTTP (NDJSON)
   ▼                                        ▼
 PostgreSQL 18 + pgvector               Ollama (ROCm, GPU)
 container "nuage-postgres"             container "ollama"
```

How one chat message flows through the system:

```mermaid
sequenceDiagram
    participant B as Browser
    participant R as Router (chat.py)
    participant S as ChatService
    participant DB as PostgreSQL
    participant O as Ollama (GPU)

    B->>R: POST /api/chat {conversation_id?, message}
    R->>R: validate (Pydantic, length)
    R->>S: prepare()
    S->>DB: SELECT conversation + last N messages
    S-->>R: ChatContext (prompt = history + new message)
    R-->>B: 200, start NDJSON stream
    S->>O: POST /api/chat (stream)
    loop each generated token
        O-->>S: {thinking|content}
        S-->>B: {"type":"thinking"|"content", "text":...}
    end
    S->>DB: BEGIN; INSERT user msg + answer; COMMIT
    S-->>B: {"type":"done", token_count, generation_ms}
```

Design decisions (why NDJSON streaming, why one transaction at the end, why
UUIDs...) are explained in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| Web framework | **FastAPI** 0.162 (Python 3.15) | Async, automatic validation and OpenAPI docs |
| ASGI server | Uvicorn | Runs the async app |
| ORM / SQL | **SQLAlchemy 2** (async) + asyncpg | Parameterised queries, Python models |
| Migrations | **Alembic** | Versioned, reversible database schema changes |
| Database | **PostgreSQL 18** + pgvector | Relational integrity; vector search for future RAG |
| AI engine | **Ollama** (ROCm container) | Runs GGUF models on the AMD GPU |
| Front-end | HTML + vanilla JS + hand-written CSS (design tokens, 3 themes), Canvas 2D | No build step, no framework, no CDN |
| Font | Nunito (variable, vendored in `static/fonts/`) | Rounded and friendly, matches the cloud identity |
| Markdown | marked 15 + DOMPurify 3 + highlight.js 11 (vendored in `static/vendor/`) | Formatted answers, XSS-safe, no CDN |
| Containers | Podman + podman-compose | Rootless containers on an immutable OS |
| Tests | pytest, pytest-asyncio, httpx | Integration tests against a real test database |
| Quality | Ruff | Linter (incl. security rules) and formatter |
| Host | Bazzite (Fedora Atomic), Tailscale VPN | Stable 24/7 server, private remote access |

---

## Getting started

### Prerequisites

- Python ≥ 3.12, Podman
- Ollama running on `127.0.0.1:11434` with the model pulled:
  ```bash
  podman exec ollama ollama pull hf.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF:Q4_K_M
  ```

### 1. Install the Python dependencies

```bash
cd 05_Source_Code/NuageLibre
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
```

### 2. Create the secrets file

```bash
cp .env.example .env
# Replace every "change-me" with a strong random password:
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
chmod 600 .env
```

### 3. Start PostgreSQL

```bash
venv/bin/podman-compose up -d          # first start runs db/init/01-create-roles.sh
podman logs nuage-postgres | tail      # should end with "ready to accept connections"
```

### 4. Create the tables

```bash
venv/bin/alembic upgrade head
```

### 5. Run the application

```bash
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- Web UI: <http://127.0.0.1:8000>
- Interactive API docs (Swagger): <http://127.0.0.1:8000/docs>

To reach it from another device over Tailscale, listen on the Tailscale
interface only (not `0.0.0.0`, which would also expose it to the local network):

```bash
venv/bin/uvicorn app.main:app --host 100.67.50.109 --port 8000
```

---

## Configuration

All settings are environment variables (or lines in `.env`), read and
validated by [`app/config.py`](app/config.py).

| Variable | Default | Description |
|---|---|---|
| `NUAGE_DATABASE_URL` | – | App connection string (restricted `nuage_app` role) |
| `NUAGE_MIGRATION_DATABASE_URL` | – | Alembic connection string (`nuage_owner` role) |
| `NUAGE_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server |
| `NUAGE_MODEL` | Qwen3.8 27B Q4_K_M | Default model (pre-selected in the menu, used when none is sent) |
| `NUAGE_NUM_CTX` | `16384` | Context window in tokens (drives VRAM usage) |
| `NUAGE_MAX_HISTORY_MESSAGES` | `50` | Max messages re-sent to the model |
| `NUAGE_MAX_MESSAGE_LENGTH` | `100000` | Max characters per user message |
| `NUAGE_OLLAMA_READ_TIMEOUT` | `300` | Seconds to wait between two tokens |
| `NUAGE_SQL_ECHO` | `false` | Log every SQL statement (learning/debugging) |

### Why the Q4_K_M model?

The previous Q6_K version of the model (22.7 GB of weights) left almost no room for
the KV cache on a 24 GB GPU and crashed with *out of memory* errors.
Measurements on the RX 7900 XTX with the Q4_K_M version:

| Context (`num_ctx`) | VRAM used by the model | Placement | Speed |
|---|---|---|---|
| 8 192 | 17.1 GB | 100 % GPU | ~34 tok/s |
| 16 384 (default) | ~18 GB | 100 % GPU | ~30–34 tok/s |
| 32 768 | 18.6 GB | 100 % GPU | — |

Other installed models appear in the menu, smallest first, with their size so the
user can judge whether a model fits in VRAM (e.g. `qwen3:4b`: 2.5 GB, ~150 tok/s).
Models whose weights alone approach 24 GB (e.g. the Q6_K variant) will be partly
offloaded to the CPU and become much slower.

---

## API reference

Full interactive documentation is generated by FastAPI at `/docs`.

| Method | Route | Description | Success |
|---|---|---|---|
| `GET` | `/api/health` | Status of Ollama and PostgreSQL | 200 |
| `GET` | `/api/models` | Installed chat models (smallest first, default flagged) | 200 / 503 |
| `POST` | `/api/chat` | Send a message, receive an NDJSON stream | 200 / 404 / 422 / 503 |
| `GET` | `/api/conversations?limit=50` | List conversations, most recent first | 200 |
| `GET` | `/api/conversations/{id}` | One conversation with its messages | 200 / 404 |
| `DELETE` | `/api/conversations/{id}` | Delete a conversation and its messages | 204 / 404 |

`POST /api/chat` body:

```json
{ "conversation_id": null, "message": "Hello!", "model": "qwen3:4b" }
```

`model` is optional: without it, an existing conversation continues with the model
of its latest answer, and a new one uses `NUAGE_MODEL`. A model that is not
installed is rejected with **422** (allow-list).

```
```

Response (`application/x-ndjson`, one event per line):

```json
{"type": "conversation", "id": "0b6c…", "title": "Hello!", "is_new": true, "model": "qwen3:4b"}
{"type": "thinking", "text": "The user greets me…"}
{"type": "content", "text": "Hello"}
{"type": "content", "text": "! How can I help?"}
{"type": "done", "message_id": "9f1e…", "model": "qwen3:4b", "token_count": 42, "generation_ms": 1530}
```

If something fails, an `{"type": "error", "text": "..."}` event ends the stream
and nothing is saved.

---

## Tests

```bash
venv/bin/pytest -v           # 35 tests, < 1 second, no GPU needed
venv/bin/ruff check .        # lint (incl. security rules)
venv/bin/ruff format --check .
```

The tests run against a dedicated `nuage_libre_test` database (never the real
one) and replace Ollama with a fake, deterministic model. The full test plan,
with the requirement covered by each test, is in [docs/TEST_PLAN.md](docs/TEST_PLAN.md).

---

## Project structure

```
NuageLibre/
├── app/
│   ├── main.py                 # FastAPI app: routers + static files
│   ├── config.py               # typed settings from env / .env
│   ├── database.py             # async engine, connection pool, sessions
│   ├── dependencies.py         # dependency injection wiring
│   ├── models.py               # ORM tables (Conversation, Message)
│   ├── schemas.py              # API input/output DTOs (Pydantic)
│   ├── routers/                # presentation layer (HTTP routes)
│   │   ├── chat.py
│   │   ├── conversations.py
│   │   ├── health.py
│   │   └── models.py           # model menu
│   ├── services/               # business layer
│   │   ├── chat_service.py     # chat orchestration + persistence
│   │   ├── model_catalog.py    # installed models, allow-list validation
│   │   └── ollama_client.py    # Ollama HTTP protocol
│   └── repositories/           # data access layer
│       └── conversation_repository.py
├── static/
│   ├── index.html              # web UI structure (single page)
│   ├── css/                    # themes.css (tokens + 3 themes), app.css, markdown.css
│   ├── js/                     # app.js (chat), theme.js (switcher), sky.js (animated skies)
│   ├── fonts/                  # Nunito woff2, served locally (+ licence, SHA256SUMS)
│   └── vendor/                 # pinned third-party JS/CSS (+ README, SHA256SUMS)
├── migrations/                 # Alembic: versioned schema changes
├── db/init/01-create-roles.sh  # PostgreSQL roles & privileges bootstrap
├── tests/                      # pytest suite
├── docs/                       # architecture, database, test plan, skills map
├── compose.yaml                # PostgreSQL container
├── alembic.ini
├── pyproject.toml              # pytest & ruff configuration
├── requirements.txt            # runtime dependencies
├── requirements-dev.txt        # + test & dev tools
└── .env.example                # configuration template (real .env is git-ignored)
```

---

## Security

Measures implemented so far (details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security)):

| Threat | Mitigation |
|---|---|
| SQL injection | Only parameterised queries (SQLAlchemy); tested by `T-SEC-01` |
| Compromised app destroying data | App DB role `nuage_app` limited to SELECT/INSERT/UPDATE/DELETE: no DROP, no ALTER, no CREATE |
| Database exposed on the network | PostgreSQL port bound to `127.0.0.1` only |
| Secrets leaked in Git | Passwords only in `.env` (git-ignored, `chmod 600`); `.env.example` template |
| XSS via model output | Markdown converted by marked then sanitised by DOMPurify (scripts, event handlers, `javascript:` links removed); every other text inserted with `textContent` |
| Arbitrary model name sent by the client | Allow-list: the model must be installed and able to chat, else 422 |
| Compromised CDN / supply chain | Front-end libraries and fonts vendored and pinned, with SHA-256 checksums |
| Tracking by third-party servers | The UI loads nothing from the internet (no CDN, no Google Fonts); enforced by `T-SEC-04` |
| Reverse tabnabbing via links in answers | Links forced to `target="_blank" rel="noopener noreferrer"` |
| Malformed / oversized input | Pydantic validation (types, UUID format, lengths), 413 on oversize |
| Id enumeration | Random UUIDv4 primary keys instead of sequential integers |
| Internal details leaked in errors | Generic messages to the client, full stack traces only in server logs |
| Data inconsistency | Foreign key + `ON DELETE CASCADE`, `CHECK` constraint, atomic transactions |
| GPU out-of-memory | Q4_K_M model + explicit `num_ctx` + bounded history |

Known gaps, next on the roadmap: **no user authentication yet** (access is
currently protected only by the Tailscale network).

---

## Roadmap

- [x] v0.1 — Minimal chat with Ollama, streaming
- [x] v0.2 — Layered architecture, PostgreSQL persistence, migrations, tests
- [x] v0.3 — Model selector, Markdown rendering with syntax highlighting
- [x] v0.4 — Visual identity: mascot, cloud bubbles, 3 themes (day / night / pro), no CDN
- [ ] v0.5 — Authentication (users table, Argon2 password hashing, sessions/JWT)
- [ ] v0.6 — Document upload + RAG (text extraction, embeddings in pgvector, sources cited)
- [ ] v0.7 — Accessibility audit (RGAA), LaTeX math rendering
- [ ] v0.8 — Containerised app, CI pipeline (GitHub Actions: lint, tests, image build)

---

## Detailed documentation

| Document | Content |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, request lifecycle, design decisions, security |
| [docs/DATABASE.md](docs/DATABASE.md) | Conceptual / logical / physical data models, roles & privileges, migrations |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | Test strategy, test cases and results |
| [docs/SKILLS_MAPPING.md](docs/SKILLS_MAPPING.md) | Mapping of this work to the CDA professional skills (CP 1–11) |
| [AGENTS.md](AGENTS.md) | Instructions for AI coding assistants working on this repository |
