# Mapping to the CDA professional skills (RNCP 37873)

This table links the work done in the repository to the 11 professional skills
(*Compétences Professionnelles*, CP) of the *Concepteur Développeur d'Applications*
certification. It is a working index to build the *Dossier Professionnel* and the
project report: for each skill, where the evidence is and what is still missing.

Status: ✅ evidence exists · 🟡 partial · ❌ not started

## AT1 — Développer une application sécurisée

| CP | Evidence in this repository | Status | Missing |
|---|---|---|---|
| **CP1** Installer et configurer son environnement de travail | Python venv, `requirements*.txt`, Podman + `compose.yaml`, `.env` config, Ruff, VS Code Remote-SSH over Tailscale, Bazzite server setup | ✅ | Screenshot/description of the dev setup in the report |
| **CP2** Développer des interfaces utilisateur | `static/` (HTML + 4 CSS + 5 JS files): own visual identity (mascot, cloud bubbles), 3 themes built on design tokens, Canvas animation, **split-screen workspace** (file manager + chat, 3 view modes animated with CSS grid, **container queries**, `inert` hidden panel), **drag & drop upload** with progress bars, cancel and keyboard alternative, responsive (phone fallback), streaming display, model menu, Markdown rendering, ARIA (radio group, `role="switch"`, `progressbar`, `aria-disabled`, `aria-live`), keyboard support, `prefers-reduced-motion`, contrast-checked colours, XSS-safe rendering, no CDN (`docs/ARCHITECTURE.md` §4.10, `docs/CLOUD_STORAGE.md` §6) | ✅ | Figma mock-ups of the 3 themes, full RGAA audit |
| **CP3** Développer des composants métier | `app/services/chat_service.py` (orchestration, transaction), `ollama_client.py` (protocol, typed errors), `file_service.py` (upload rules, disk-then-database consistency with compensation, download policy), `file_policy.py` (pure, unit-tested rules: names, MIME types, safe preview), `auth.py` (identity seam), Pydantic validation, defensive error handling | ✅ | Authentication module (v0.6) |
| **CP4** Contribuer à la gestion d'un projet informatique | VS Code Kanban board (`.vscode/vscode-kanban.json`), Git history, roadmap in README | 🟡 | Regular small commits, sprint/iteration notes, retrospective |

## AT2 — Concevoir et développer une application sécurisée organisée en couches

| CP | Evidence | Status | Missing |
|---|---|---|---|
| **CP5** Analyser les besoins et maquetter une application | Persona & empathy map, functional tree, user flow (`01_Project_Management/`), HTML prototype (`04_UI_Mockup/`) | 🟡 | Use-case diagram, Figma screens, user stories with acceptance criteria |
| **CP6** Définir l'architecture logicielle | `docs/ARCHITECTURE.md`: layers, dependency injection, sequence diagrams, design decisions; `docs/CLOUD_STORAGE.md`: storage architecture (bytes on disk / index in PostgreSQL), streaming upload justified by a measurement (tmpfs), "seam" pattern for future authentication, eco-design (constant memory, no double write) | ✅ | Deployment diagram (v0.9) |
| **CP7** Concevoir et mettre en place une base de données relationnelle | `docs/DATABASE.md`: business rules R1–R14, MCD, MLD, MPD with `users` and `files`; FK with deliberate `CASCADE` vs `RESTRICT`, `UNIQUE`, `CHECK` constraints (path traversal stopped in the database), composite indexes matching the queries; roles with least privilege; verified rights (as `nuage_app`) | ✅ | Test data set (seed script) |
| **CP8** Développer des composants d'accès aux données SQL et NoSQL | `app/repositories/`: parameterised queries scoped by owner, eager loading, transactions; `file_storage.py`: file-system data access (atomic write, `fsync`, containment check); Alembic migrations including two data migrations (`UPDATE … FROM` backfill; Ghost User seed + nullable → backfill → NOT NULL), tested on a non-empty database and for reversibility | 🟡 | NoSQL/vector part: pgvector embeddings for RAG (v0.7) |

## AT3 — Préparer le déploiement d'une application sécurisée

| CP | Evidence | Status | Missing |
|---|---|---|---|
| **CP9** Préparer et exécuter les plans de tests | `docs/TEST_PLAN.md`: 115 automated tests (unit, integration, security: path traversal, stored XSS, IDOR, CSRF), isolated test DB and storage folder, impersonation of a second user, manual mutation checks (6 injected bugs, all caught), scripted end-to-end browser tests (17 steps for v0.5) | ✅ | Automated browser tests in CI, load test |
| **CP10** Préparer et documenter le déploiement | README "Getting started", `compose.yaml`, `db/init/` bootstrap, backup/restore procedure | 🟡 | Containerfile for the app, systemd/Quadlet service, production checklist |
| **CP11** Contribuer à la mise en production dans une démarche DevOps | Ruff + pytest commands ready to automate | ❌ | CI pipeline (GitHub Actions: lint, tests with a PostgreSQL service, image build) |

## Talking points for the jury

- **Why a transaction at the end of generation?** Atomicity: a failed generation
  leaves no half-saved exchange (`T-CHAT-05` proves it).
- **Why two database roles?** Least privilege: even a compromised app cannot drop tables
  (verified: `ERROR: must be owner of table messages`).
- **Why Q4_K_M?** Q6_K weights (22.7 GB) + KV cache exceeded 24 GB of VRAM → OOM.
  Q4_K_M = 17 GB, 100 % on GPU, ~34 tok/s; `num_ctx` set explicitly.
- **How do you know your tests are useful?** Two bugs were injected on purpose; both
  were caught.
- **What did a real browser test find that unit tests did not?** A closing script
  tag inside a JS comment broke the whole page.
- **Why not just put the model's answer in `innerHTML`?** It is untrusted output:
  `<img onerror=…>` would run JavaScript. DOMPurify strips it (test T-SEC-03).
- **Why validate the model name if Ollama would refuse it anyway?** Defence in
  depth: never forward an unchecked client value to an internal service (allow-list).
- **What is not secure yet?** No authentication; access relies on Tailscale only.
  Planned for v0.6 (Argon2 hashing, JWT). The *authorisation* part (each user only
  reaches their own data) already exists and is tested.
- **Why three themes?** Design tokens: components only use CSS variables, a
  theme is one block of values. The *Mode Pro* exists for a technical audience;
  the creative themes show the product's identity.
- **Your brand pink and blue are pastel: is the text readable?** They fail the
  4.5:1 contrast ratio on white (~1.9:1), so they are only used for surfaces and
  decoration; text uses darker "ink" variants of the same hue (5.6:1).
- **How do you display maths safely?** KaTeX typesets the LaTeX, then its HTML
  goes through DOMPurify like any other answer; KaTeX itself runs with
  `trust: false` and expansion limits (a macro bomb is stopped). Test T-SEC-05.
- **Is the animated sky wasteful?** One canvas, animated only while the night
  theme is visible and the tab is active; disabled with "reduce motion".
- **You have no login: why a `user_id` everywhere?** The "Ghost User" strategy: the
  data model and every query are already multi-user; only `get_current_user_id()`
  will change for JWT (the *seam*). Tests impersonate a second user and prove the
  isolation (T-USER-03/04).
- **Why not FastAPI's `UploadFile`?** Measured: `/tmp` is a tmpfs (RAM) on this
  server, and Starlette spools every upload there first. Streaming the raw body
  writes once, straight to the data disk, in constant memory; and it makes uploads
  CSRF-proof by construction (custom header → CORS preflight).
- **How can't someone download `/etc/passwd`?** Five layers: paths built only from
  UUIDs, typed route parameter, ownership filter, `CHECK` constraint in the database,
  containment check after `resolve()`. Removing the last one in a mutation test
  really leaked `/etc/passwd` through a symlink: it is not redundant.
- **Why `RESTRICT` for files but `CASCADE` for conversations?** A file row points to
  bytes PostgreSQL cannot delete; a cascade would silently lose track of them.
- **What if the database fails after the file is written?** The bytes are deleted
  again (compensating action); disk first, row last, so no row ever points to
  missing bytes (T-FILE-11).
- **Why can't I view an SVG or an HTML file?** Stored XSS: rendered from our origin,
  its JavaScript would act as the app. Only script-free types are shown inline;
  text is shown as plain text, and `nosniff` + CSP `sandbox` protect the rest.
- **Why container queries?** In split-screen, the chat panel's width, not the
  screen's, decides its layout (history as a column or a drawer).
