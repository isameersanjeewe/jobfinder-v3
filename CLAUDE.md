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
| `main.py` | FastAPI app. `/agent/start` + `/agent/jobs/{id}` (background run, used by the UI), `/agent/run` (sync, CLI/tests; `hours` = 24 daily / 168 weekly; optional `filters` = per-run overrides, see In-app filters), `/profile/filters` (profile defaults for the UI), `/search` (classic pipeline), `/leads?format=json\|csv\|xlsx`, `/export/sheets`, `/export`, `/feedback`, `/parse-resume`, `/usage`, `/health`. Loads `backend/.env`. Security middleware (see Security) |
| `agent_service.py` | Agent loop. Cross-site dedupe (`dedupe_key`: company + title + city; text of duplicate copies is merged so any disclaimer still blocks). Plan researches unverified companies by best potential score, routes staffing jobs to the C2C track. h1bdata research is per year (last + current) and cached with expiry. Tools: `search_jobs`, `research_company_h1b`, `set_company_signal`, `score_jobs`, `save_leads`, `finish`. Providers: `LLM_PROVIDER=openai` (Ollama/Groq/Gemini, OpenAI-compatible), `anthropic`, or `none` (deterministic `_plan()` with the same tools). System prompt is filled from the profile. Small models: args are coerced and the plan runs as fallback if the LLM stops without saving |
| `rules.example.json` | Committed profile template. Users copy it to `rules.json` (gitignored). Load order: env `RULES_JSON` -> env `RULES_PATH` -> `rules.json` -> `rules.example.json` |
| `rules_service.py` | Gate: title -> geo -> future date -> recency -> (if `visa.enabled`) posting visa language (`No`/`Yes`/`Transfer`/`Unknown`; `Transfer` passes only with `visa.mode = h1b_transfer`) -> staffing firm (`staffing_c2c_only`) -> company H1B tier. Sets `job["gate"]` = `pass` or a reason. `load_rules()` merges per-user `rules.local.json` (persisted research; never overrides manual tiers) |
| `free_sources.py` | JobSpy (each title x profile locations + remote US, deduped by URL, per-call timeout) + Greenhouse/Lever/Ashby public JSON + Workday (`slug: "tenant.wdN/site"`; serialized + 429 backoff) |
| `scraper_service.py` | Legacy: JSearch, Adzuna (keys), Remotive. Owns `normalize()` (shared job shape) |
| `fit_service.py` | `score_lead()`: 0-100 lead score (weights in profile `scoring`), drives ranking + `hot_lead`. `keyword_fit()`: separate resume keyword coverage; `fit=None` when no JD text (unknown, not zero) |
| `store.py` | SQLite (`DB_PATH`): `leads` + `seen` tables keyed by `user_id`; drives "only new leads" |
| `export_service.py` | CSV, XLSX, Google Sheets append (service account, append-only, maps to the tab's existing headers, dedupe by job link) |
| `run_agent.py` | CLI for cron. `.github/workflows/daily-agent.yml` runs it on weekdays (set `RULES_JSON` as a repo secret) |
| `smoke_test.py` | Live check of every real source |
| `filter_service.py`, `ai_service.py` | v3 classic pipeline (keyword H1B, Claude match). Kept for `/search` |

Frontend: `frontend/index.html` (vanilla JS). Buttons: Search, Run Agent (+ Daily/Weekly window), Leads CSV/XLSX, Google Sheet.

## In-app filters (any user, no file editing)
The UI's **Search filters** panel prefills from `GET /profile/filters` and, once saved, is stored in that browser only
and sent with each run as `filters`. `rules_service.apply_filters()` overlays it on a fresh copy of the profile:
- `titles`: replaces the role family (`core_title_terms`/`fallback_titles` cleared; exclusion words that appear in a title dropped). ATS/Workday search phrases come from `search_queries()` (shortest distinct titles).
- `locations` ("City, ST") + `remote_ok`: geography terms are derived (city, full state name, ", ST"). No locations = anywhere.
- `visa`: `none` (no visa checks) | `new_sponsorship` | `h1b_transfer`. `sponsors_only` = company must be a confirmed Strong/Moderate H-1B sponsor (else unknown companies pass with fewer H1B points).
- `industries` (highest first -> score tiers 15, 13, 11...), `exclude_companies`.
Nothing role-specific is hardcoded: the default role family lives in the profile's `core_title_terms`.

## Security
- `API_TOKEN` env: when set, every endpoint except `/` and `/health` requires `Authorization: Bearer <token>` (the UI has a token field under Connection settings). **Any internet-facing deployment must set it.** Without it, anyone can start runs, read leads and append to your Sheet.
- `ALLOWED_ORIGINS` env (CORS, default `*`). Input limits on every request model; one agent run at a time (HTTP 429 otherwise); resume uploads capped at 5 MB / 20 pages and must be a PDF; server errors are logged, clients get a generic message.
- Untrusted scraped text: the UI escapes everything and only opens `http(s)` links (plus a CSP). CSV/XLSX cells starting with `= + - @` are prefixed with `'`. Sheets appends use `RAW` (never parsed as formulas).
- Dependencies: `pip-audit` runs in `run_live_test.sh`; requirement minimums are the first versions with no known CVEs. Keep them current.
- Single-tenant for now: one token = full access. Per-user auth is on the roadmap.

## Run / test
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r tests/requirements-dev.txt
cp backend/rules.example.json backend/rules.json   # then edit your profile
cp backend/.env.example backend/.env               # optional: Sheets + LLM settings
.venv/bin/python tests/test_agent.py          # offline, mocked sources; must print ALL OK
bash run_live_test.sh                          # audit + offline + live sources -> logs/
cd backend && ../.venv/bin/python smoke_test.py --quick   # live sources + H1B filter checks, ~2 min
cd backend && ../.venv/bin/uvicorn main:app --reload   # API on :8000
```
Env: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON or a path to the key file), `SHEET_ID`, `DB_PATH`, `RULES_PATH`, `RULES_JSON`, `RULES_LOCAL_PATH`, `API_TOKEN`, `ALLOWED_ORIGINS`, `JOBSPY_PROXIES`, `JOBSPY_TIMEOUT`, `AGENT_MAX_STEPS`.

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
- LinkedIn is now queried only for an explicit search location + the remote-US sweep, not every profile location (`free_sources.fetch_jobspy`) -- cuts LinkedIn call volume roughly in proportion to profile location count. The other JobSpy sites still get the full location fan-out. Actual post-change 429 rate not yet confirmed live; re-check with `JOBSPY_PROXIES` unset before relying on it.
- 2026-11-16: hosted on Render's free tier, a full `/agent/start` run was observed dying mid-run (its `_jobs` entry vanished without ever reaching "done", and a concurrent request failed with a bare connection error) -- consistent with an OOM kill, since `self.pool` was holding ~3000+ fetched postings' full description text in memory for the whole run even though ~95%+ terminally fail title/geo/recency/sponsorship-language gating in the first pass. Fixed in `agent_service.search_jobs`: description is dropped immediately for any gate that can never change again, kept only for `pass`/`company_unverified`/`company_risk`/`staffing_c2c_only`. Not yet confirmed this fully eliminates the crash on the free tier under a real full run (all JobSpy sites + ATS) -- re-check live if it recurs; the other lever is upgrading the Render plan, which conflicts with the "completely free" goal above and needs the user's call.
- Workday rate-limits per IP across tenants. Its list API gives only "Posted N Days Ago"; exact dates/locations/deadline come from the detail call (fetched for jobs <= 7 days old).
- Full agent run with all JobSpy sites ~10-20 min. Small local LLMs (llama3.1 8B) don't drive the tools reliably; the deterministic plan does the work.

## Roadmap (priority order)
1. Dice (or another free contract board) for the C2C track. Staffing-firm jobs found by the FT search are routed to C2C already.
   Investigated 2026-11: Dice has no working public API (`job-search-api.svc.dhigroupinc.com` 403s on the stale public
   key found in its JS bundle); only option is scraping its public search-results page (no login, but fragile to
   frontend changes and likely against ToS). Deliberately skipped for now -- revisit only with a clear free API, or if
   the user decides the scrape-and-accept-risk trade-off is worth it.
2. Contact finding (contact points in the score are always 0 today). Free + safe options are limited; avoid logged-in LinkedIn scraping.
3. Multi-user: per-user auth (replace the single API_TOKEN), Postgres (Supabase free), per-user profiles/resume, job queue, shared fetch cache.
4. Optional LLM JD-vs-resume gap analysis for top leads only (token budget).
5. If LinkedIn 429s are still high after the location-limiting above, add optional proxies (`JOBSPY_PROXIES`) or drop LinkedIn's `results_wanted`.
