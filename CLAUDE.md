# CLAUDE.md — JobFinder v4 (agentic)

Read this first. Domain detail (search rules, sponsorship method, scoring, source quirks) is in `docs/CONTEXT.md`.
Personal working notes, if any, are in `CLAUDE.local.md` and `docs/*.local.md` (gitignored, never committed).

## What this is
A user-agnostic job-search agent. Each user has a profile (`backend/rules.json`, copied from `rules.example.json`):
target titles, locations, recency, optional visa-sponsorship filtering, company tiers, industry priorities and job boards.
Goal: every day, surface **all fresh, eligible, high-fit leads and nothing else**, and export them to CSV/XLSX/Google Sheets.
Constraint: **completely free and open source** (free data sources, free/local LLM optional, free hosting/cron).

## Privacy rule (hard)
The repo is public. Never commit personal data: names, locations, visa status, employers, interviews, resumes,
sheet IDs, company research tied to a person. Personal values go in gitignored files (`backend/rules.json`,
`backend/rules.local.json`, `backend/.env`, `CLAUDE.local.md`, `docs/*.local.md`). Tests use fictional companies and
the example profile only. No hardcoded locations, titles or industries in code: read them from the profile.

## Architecture (backend/)
| File | Role |
|---|---|
| `main.py` | FastAPI app. `/search` (classic pipeline), `/agent/run` (`hours` = 24 daily / 168 weekly), `/leads?format=json\|csv\|xlsx`, `/export/sheets`, `/export`, `/feedback`, `/parse-resume`, `/usage`, `/health`. Loads `backend/.env` via python-dotenv |
| `agent_service.py` | Agent loop. Tools: `search_jobs`, `research_company_h1b`, `set_company_signal`, `score_jobs`, `save_leads`, `finish`. Providers: `LLM_PROVIDER=openai` (Ollama/Groq/Gemini, OpenAI-compatible), `anthropic`, or `none` (deterministic `_plan()` with the same tools). System prompt is filled from the profile. Small models: args are coerced and the plan runs as fallback if the LLM stops without saving |
| `rules.example.json` | Committed profile template. Users copy it to `rules.json` (gitignored). Load order: env `RULES_JSON` -> env `RULES_PATH` -> `rules.json` -> `rules.example.json` |
| `rules_service.py` | Gate: title -> geo -> recency -> (if `visa.enabled`) posting visa language -> staffing firm (`staffing_c2c_only`) -> company H1B tier. Sets `job["gate"]` = `pass` or a reason. `load_rules()` merges per-user `rules.local.json` (persisted research; never overrides manual tiers) |
| `free_sources.py` | JobSpy (each title x profile locations + remote US, deduped by URL, per-call timeout) + Greenhouse/Lever/Ashby public JSON + Workday (`slug: "tenant.wdN/site"`; serialized + 429 backoff) |
| `scraper_service.py` | Legacy: JSearch, Adzuna (keys), Remotive. Owns `normalize()` (shared job shape) |
| `fit_service.py` | `score_lead()`: 0-100 lead score (weights in profile `scoring`), drives ranking + `hot_lead`. `keyword_fit()`: separate resume keyword coverage; `fit=None` when no JD text (unknown, not zero) |
| `store.py` | SQLite (`DB_PATH`): `leads` + `seen` tables keyed by `user_id`; drives "only new leads" |
| `export_service.py` | CSV, XLSX, Google Sheets append (service account, append-only, maps to the tab's existing headers, dedupe by job link) |
| `run_agent.py` | CLI for cron. `.github/workflows/daily-agent.yml` runs it on weekdays (set `RULES_JSON` as a repo secret) |
| `smoke_test.py` | Live check of every real source |
| `filter_service.py`, `ai_service.py` | v3 classic pipeline (keyword H1B, Claude match). Kept for `/search` |

Frontend: `frontend/index.html` (vanilla JS). Buttons: Search, Run Agent (+ Daily/Weekly window), Leads CSV/XLSX, Google Sheet.

## Run / test
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r tests/requirements-dev.txt
cp backend/rules.example.json backend/rules.json   # then edit your profile
cp backend/.env.example backend/.env               # optional: Sheets + LLM settings
.venv/bin/python tests/test_agent.py          # offline, mocked sources; must print ALL OK
bash run_live_test.sh                          # live sources -> logs/
cd backend && ../.venv/bin/uvicorn main:app --reload   # API on :8000
```
Env: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON or a path to the key file), `SHEET_ID`, `DB_PATH`, `RULES_PATH`, `RULES_JSON`, `RULES_LOCAL_PATH`, `JOBSPY_PROXIES`, `JOBSPY_TIMEOUT`, `AGENT_MAX_STEPS`.

## How to work in this repo
- **Diff-only edits scoped to the named function.** Never regenerate whole files.
- **Fix scraper volume/quality before adding features.**
- **Company-level LCA data beats keyword scanning** for sponsorship. Keyword scan is only for explicit "no sponsorship" language on a posting.
- Keep the `normalize()` + try/except pattern for every new source. A failing source returns `[]` and is reported, never crashes the run.
- Be cautious and incremental; explain trade-offs (e.g., account-ban risk) before adopting session/login-based scraping.
- Prompt style: PM -> Dev -> QA. State the business reason, write scoped code, list failure modes before finishing.
- Never submit applications or send messages for the user. Drafts only.
- Run `tests/test_agent.py` after every change. Add a test case for every new gate rule.
- Don't commit secrets or personal data (see Privacy rule).

## Source behavior (live, 2026-11)
- Employer boards incl. Workday: ~3,400 jobs/run; Workday is serialized + throttled (~55s total), the rest ~2s.
- JobSpy per title x location: Indeed ~25, LinkedIn ~90 (expect ~15 x HTTP 429 per run without proxies), Glassdoor ~40. Google empty, ZipRecruiter 403 (upstream). Calls can hang on a socket: guarded by `JOBSPY_TIMEOUT` (120s).
- Workday rate-limits per IP across tenants. Its list API gives only "Posted N Days Ago"; exact dates/locations/deadline come from the detail call (fetched for jobs <= 7 days old).
- Full agent run with all JobSpy sites ~10-20 min. Small local LLMs (llama3.1 8B) don't drive the tools reliably; the deterministic plan does the work.

## Roadmap (priority order)
1. Cross-source dedupe: the same job arrives from Indeed/Glassdoor/LinkedIn with different URLs (~half of live leads). Key on normalized company + title + city.
2. Agent runtime: too slow for a synchronous `/agent/run` on a free host. Background job, or fewer LinkedIn calls.
3. Dice for the C2C track; the deterministic plan only runs FT, so staffing-firm jobs are excluded rather than routed.
4. Research prioritization: the plan researches the first 5 unverified companies alphabetically; rank by passing-job score instead.
5. Contact finding (contact points in the score are always 0 today).
6. Recency hardening: reject future/aggregator dates.
7. Multi-user: auth + Postgres (Supabase free), per-user profiles/resume, job queue, rate-limited shared fetch cache.
8. Optional LLM JD-vs-resume gap analysis for top leads only (token budget).
