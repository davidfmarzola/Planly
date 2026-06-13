# AGENTS.md — Planly

## Project

Single-file Flask backend (`app.py`) + static frontend (`index.html` + `assets/`).
Generates study-plan PDFs for Brazilian public exams ("concursos públicos") by
parsing uploaded exam-edict PDFs with AI (DeepSeek) and scheduling blocks
deterministically (50 min study / 10 min break, WFQ discipline selection).

## Commands

| Action | Command |
|---|---|
| Install deps | `pip install -r requirements.txt` |
| Run dev server | `python app.py` (Flask on port 5000, debug=True) |
| Deploy | `web: gunicorn app:app` (Procfile) |

No tests, linter, typecheck, or CI exist in this repo.

## Architecture

```
app.py          ← Flask backend (only source of truth for logic)
app_last_version.py  ← Previous version backup; do NOT edit
index.html      ← Static frontend (plain HTML/CSS/JS, no framework)
assets/         ← CSS, JS, images, icons
docs/           ← GitHub Pages site assets
```

### Endpoints (app.py)

| Method | Path | Purpose |
|---|---|---|
| GET | `/teste` | Health-check |
| POST | `/extrair_cargos` | Extract cargo list from uploaded PDF |
| POST | `/gerar` | Generate study-plan PDF (multipart: edital PDF + cargo + rotina) |
| POST | `/informar` | Interactive Q&A flow (JSON, session-based via in-memory dict) |

### Key flow in `/gerar`

1. Read PDF text with PyMuPDF (fitz)
2. DeepSeek extracts disciplines/topics as JSON
3. Second AI pass ("reflection") validates/corrects JSON
4. Deterministic scheduler builds weekly blocks (WFQ algorithm)
5. ReportLab renders PDF for download

## Important conventions / quirks

- **API key is hardcoded** in `app.py` line 46 (`cliente_ia = OpenAI(...)`). Do not move it to `.env` unless asked.
- **`load_dotenv()`** is called but the AI key bypasses it.
- **Sessions for `/informar`** are stored in a plain `dict` in memory (`sessoes_ativas`) — they do not survive restarts.
- Portuguese day names are normalized to unaccented keys internally (`terca`, `sabado`).
- All AI prompts and responses are in Portuguese.
- The frontend form sends study hours as a simple radio value ("1"–"5"), not a full JSON routine — the backend has its own mapping logic.

## Env / .gitignore

`.env` and `.env.*` are gitignored. Only `load_dotenv()` is used; no other env vars are read.
