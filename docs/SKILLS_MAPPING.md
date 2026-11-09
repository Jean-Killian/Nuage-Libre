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
| **CP2** Développer des interfaces utilisateur | `static/index.html`: responsive (mobile off-canvas sidebar), streaming display, model menu, Markdown rendering with syntax highlighting, ARIA labels, `aria-live`, keyboard support (Enter / Shift+Enter), XSS-safe rendering (DOMPurify) | 🟡 | Figma mock-ups, RGAA audit, local Tailwind build |
| **CP3** Développer des composants métier | `app/services/chat_service.py` (orchestration, transaction), `ollama_client.py` (protocol, typed errors), Pydantic validation, defensive error handling | ✅ | Authentication module (v0.3) |
| **CP4** Contribuer à la gestion d'un projet informatique | VS Code Kanban board (`.vscode/vscode-kanban.json`), Git history, roadmap in README | 🟡 | Regular small commits, sprint/iteration notes, retrospective |

## AT2 — Concevoir et développer une application sécurisée organisée en couches

| CP | Evidence | Status | Missing |
|---|---|---|---|
| **CP5** Analyser les besoins et maquetter une application | Persona & empathy map, functional tree, user flow (`01_Project_Management/`), HTML prototype (`04_UI_Mockup/`) | 🟡 | Use-case diagram, Figma screens, user stories with acceptance criteria |
| **CP6** Définir l'architecture logicielle | `docs/ARCHITECTURE.md`: layers, dependency injection, sequence diagram, design decisions, eco-design (bounded context, metrics) | ✅ | Deployment diagram (v0.6) |
| **CP7** Concevoir et mettre en place une base de données relationnelle | `docs/DATABASE.md`: business rules, MCD, MLD, MPD; constraints; roles with least privilege; test database; verified rights | ✅ | Users & documents tables (v0.3 / v0.4), test data set (seed script) |
| **CP8** Développer des composants d'accès aux données SQL et NoSQL | `app/repositories/conversation_repository.py`: parameterised queries, eager loading, transactions; Alembic migrations including a data migration (`UPDATE … FROM` backfill) | 🟡 | NoSQL/vector part: pgvector embeddings for RAG (v0.4) |

## AT3 — Préparer le déploiement d'une application sécurisée

| CP | Evidence | Status | Missing |
|---|---|---|---|
| **CP9** Préparer et exécuter les plans de tests | `docs/TEST_PLAN.md`: 25 automated tests (unit, integration, security), isolated test DB, manual mutation check, scripted end-to-end browser test | ✅ | Automated browser tests, load test |
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
  Planned for v0.3 (Argon2 hashing, sessions).
