# JobFinder

A free, open-source, **agentic** job-search tool built for one specific, hard problem: finding roles that
will actually sponsor an H-1B (new or transfer), not just roles that happen to mention "visa" somewhere in
the posting.

## About

### Why "agentic"

Most of what people call a "job search agent" is really a keyword filter with a chat UI bolted on. JobFinder
is built differently: the search runs as a loop over a small set of tools —

- `search_jobs` — pull postings from your configured sources (LinkedIn/Indeed/Glassdoor, Greenhouse, Lever,
  Ashby, Workday boards)
- `research_company_h1b` — look up a company's actual H-1B sponsorship signal when it isn't already known
- `set_company_signal` — remember that signal so the same company is never re-researched
- `score_jobs` — score the survivors 0–100 on fit
- `save_leads` — persist only the eligible, scored leads
- `finish` — end the run

With an LLM configured (`LLM_PROVIDER=anthropic` or an OpenAI-compatible provider), a real model is handed
those tools and decides **which to call, in what order, and when to stop** — e.g. noticing a cluster of
postings from an unfamiliar company and researching it before scoring, rather than following a fixed script.
Without an LLM configured (the default, zero-cost path), a deterministic planner calls the exact same tools
in a fixed, sensible order. Same architecture either way — the difference is whether a model or a script is
driving it. That design is deliberate: the whole tool stays fully functional and completely free without
ever requiring an LLM API key.

### How this is different from a job board

- **A job board shows you postings. This investigates them first.** Before a lead ever reaches you, it's
  passed through a gate — title → geography → recency → the posting's own visa language → staffing-firm
  routing → the *company's* historical H-1B sponsorship tier — and unknown companies get actively
  researched, not just keyword-matched.
- **It remembers what you've already seen.** Cross-site deduping plus a persistent "seen" record means a
  daily run surfaces only *new* eligible leads, instead of showing you the same reposted listing on three
  different boards every day.
- **The gate is tuned to your actual situation**, not a generic filter: new sponsorship vs. H-1B transfer,
  your specific title family, your geography, your industry priorities — all config, no code changes.
- **It's free and self-hosted**, not a SaaS product holding your search data. Every data source used has a
  free tier; the optional LLM step is the only thing that would ever cost money, and it's off by default.
- **Your data stays exportable.** Leads export to CSV, XLSX, or append straight into your own Google Sheet —
  nothing is locked into a platform.

For the full technical architecture (gate pipeline, scoring rubric, multi-user design, source quirks), see
[`CLAUDE.md`](CLAUDE.md). For setting this up for more than one person, see
[`docs/MULTI_USER_SETUP.md`](docs/MULTI_USER_SETUP.md).

## Structure
```
backend/
  main.py             ← FastAPI endpoints, auth, security
  agent_service.py     ← the agent loop + tools described above
  rules_service.py     ← the gate pipeline (title/geo/recency/visa/staffing/company tier)
  free_sources.py       ← JobSpy (LinkedIn/Indeed/Glassdoor) + ATS boards (Greenhouse/Lever/Ashby/Workday)
  scraper_service.py    ← legacy sources: JSearch, Adzuna, Remotive
  fit_service.py        ← lead scoring + resume keyword fit
  store.py               ← SQLite: leads, seen, per-user accounts and profiles
  data_repo.py            ← optional durable multi-user storage (see MULTI_USER_SETUP.md)
  auth.py                  ← passcode auth for multi-user mode
  export_service.py         ← CSV / XLSX / Google Sheets export
  run_agent.py                ← CLI entry point for the daily cron
  add_user.py                  ← owner CLI to create/reset a tester's passcode
  rules.example.json            ← profile template (copy to rules.json; never commit your real one)
frontend/
  index.html             ← the app
  admin.html              ← tester management (not linked from the app; bookmark it)
```

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r tests/requirements-dev.txt
cp backend/rules.example.json backend/rules.json   # then edit your profile
cp backend/.env.example backend/.env               # optional: Sheets + LLM settings
.venv/bin/python tests/test_agent.py                # offline, mocked sources; must print ALL OK
cd backend && ../.venv/bin/uvicorn main:app --reload   # API on :8000
```
Open `frontend/index.html` directly in a browser, or point `frontend/index.html`'s Connection settings at
`http://localhost:8000`.

## Deploying for real
The live version runs the backend on Render (free tier) and the frontend as a static site on Vercel (free
tier) — see `render.yaml` and `vercel.json`. Set secrets in each platform's own dashboard, never in code:

| Variable | Purpose |
|---|---|
| `API_TOKEN` | Required for any public deployment — gates the whole API. |
| `SESSION_SECRET` | Signs per-user login sessions (multi-user mode). |
| `RULES_JSON` | Your real profile, as JSON (never commit `rules.json` itself). |
| `ALLOWED_ORIGINS` | Your frontend's origin, for CORS. |
| `ANTHROPIC_API_KEY` (optional) | Makes the agent genuinely LLM-driven instead of the free deterministic planner. |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID` (optional) | Google Sheets export. |
| `DATA_REPO_TOKEN`, `DATA_REPO` (optional) | Durable multi-user storage — see `docs/MULTI_USER_SETUP.md`. |

The full environment variable list is in `CLAUDE.md`.
