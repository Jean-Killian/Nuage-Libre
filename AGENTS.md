# Instructions for AI coding agents

These rules apply to any AI assistant (Claude, Roo Code, Cline, Aider...) working on this repository.

## Rules

1. **Complete code (no lazy coding):** never use ellipses (`...`) or placeholder
   comments to shorten code. Always write complete, working changes.
2. **Respect the layered architecture** (see `docs/ARCHITECTURE.md`):
   - `app/routers/`: HTTP only (validation, status codes). No SQL, no business logic.
   - `app/services/`: business logic. No SQL strings, no HTTP response objects.
   - `app/repositories/`: the only place that queries the database. Never commits:
     transactions are decided by the service layer.
3. **Local only:** never add a library or API that sends data to the internet.
   All AI and file processing stays on the Bazzite machine.
4. **Database changes go through Alembic:** edit `app/models.py`, then
   `venv/bin/alembic revision --autogenerate -m "..."`, review the generated
   file, then `venv/bin/alembic upgrade head`. Never edit the schema by hand.
5. **Security:** parameterised queries only; insert untrusted text in the UI with
   `textContent`. The only `innerHTML` allowed is `renderMarkdown()` in
   `static/js/app.js`, which sanitises with DOMPurify. Front-end libraries and
   fonts are vendored in `static/vendor/` and `static/fonts/` (no CDN, checked by
   test T-SEC-04). Never commit `.env`.
8. **Front-end styling:** use the design tokens of `static/css/themes.css`
   (`var(--...)`), never raw colours in components, so the three themes keep
   working. Respect `prefers-reduced-motion` for any new animation.
6. **Document in English:** detailed code comments and docstrings explaining the
   *why*, so the project owner can understand and present every line.
7. **Tests must pass before finishing:**
   ```bash
   venv/bin/pytest -q && venv/bin/ruff check . && venv/bin/ruff format --check .
   ```

## Commands

```bash
venv/bin/podman-compose up -d                                          # start PostgreSQL
venv/bin/alembic upgrade head                                          # apply migrations
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload    # run the app
```
