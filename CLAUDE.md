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
| `main.py` | FastAPI app. `POST /auth/login` (passcode -> signed session token, see Multi-user). `/agent/start` + `/agent/jobs/{id}` (background run, used by the UI), `/agent/run` (sync, CLI/tests; `hours` = 24 daily / 168 weekly; optional `filters` = per-run overrides, see In-app filters), `/profile/filters` (this user's profile defaults for the UI), `/search` (classic pipeline), `/leads?format=json\|csv\|xlsx`, `/export/sheets`, `/export`, `/feedback`, `/parse-resume`, `/usage`, `/health`. Loads `backend/.env`. Security middleware (see Security). Every user-scoped endpoint takes `user_id` from `Depends(current_user)` (the verified session token), never from a client-supplied field |
| `auth.py` | Passcode hashing (stdlib `hashlib.pbkdf2_hmac`, no new dependency) + signed session tokens (`SESSION_SECRET`-HMAC'd `user_id:expiry`, not a JWT -- avoids adding PyJWT). `make_session_token()` / `verify_session_token()` |
| `add_user.py` | Owner-run CLI, no self-serve signup: `python add_user.py <user_id> <passcode>` creates/resets a tester's passcode in the `users` table (see Multi-user) |
| `data_repo.py` | Optional Render-side GitHub API client for the private data repo (see Multi-user): reads `data/<user_id>/leads.json` via the Contents API, triggers/polls the agent run via `workflow_dispatch`. Inert (`enabled()` is `False`) unless `DATA_REPO_TOKEN`/`DATA_REPO` are set -- until then everything runs exactly as before, in-process against local SQLite |
| `agent_service.py` | Agent loop. Cross-site dedupe (`dedupe_key`: company + title + city; text of duplicate copies is merged so any disclaimer still blocks). Plan researches unverified companies by best potential score, routes staffing jobs to the C2C track. h1bdata research is per year (last + current) and cached with expiry. Tools: `search_jobs`, `research_company_h1b`, `set_company_signal`, `score_jobs`, `save_leads`, `finish`. Providers: `LLM_PROVIDER=openai` (Ollama/Groq/Gemini, OpenAI-compatible), `anthropic`, or `none` (deterministic `_plan()` with the same tools). System prompt is filled from the profile. Small models: args are coerced and the plan runs as fallback if the LLM stops without saving |
| `rules.example.json` | Committed profile template. Users copy it to `rules.json` (gitignored), or (multi-user) get it as their default until they have a saved profile row. Load order: per-user saved profile (if `user_id` given and found) -> env `RULES_JSON` -> env `RULES_PATH` -> `rules.json` -> `rules.example.json` |
| `rules_service.py` | Gate: title -> geo -> future date -> recency -> (if `visa.enabled`) posting visa language (`No`/`Yes`/`Transfer`/`Unknown`; `Transfer` passes only with `visa.mode = h1b_transfer`) -> staffing firm (`staffing_c2c_only`) -> company H1B tier. Sets `job["gate"]` = `pass` or a reason. `load_rules(user_id=...)` prefers that user's saved profile (`store.profiles`) over the file-based one, and still merges per-user `rules.local.json` (persisted research; never overrides manual tiers) |
| `free_sources.py` | JobSpy (each title x profile locations + remote US, deduped by URL, per-call timeout) + Greenhouse/Lever/Ashby public JSON + Workday (`slug: "tenant.wdN/site"`; serialized + 429 backoff) |
| `scraper_service.py` | Legacy: JSearch, Adzuna (keys), Remotive. Owns `normalize()` (shared job shape) |
| `fit_service.py` | `score_lead()`: 0-100 lead score (weights in profile `scoring`), drives ranking + `hot_lead`. `keyword_fit()`: separate resume keyword coverage; `fit=None` when no JD text (unknown, not zero) |
| `store.py` | SQLite (`DB_PATH`): `leads` + `seen` tables keyed by `user_id` (drives "only new leads"); `users` (passcode auth) + `profiles` (per-user rules.json override) tables added for multi-user, see below. Local cache/mirror, not the durable store once the private data repo is set up (see Multi-user) |
| `export_service.py` | CSV, XLSX, Google Sheets append (service account, append-only, maps to the tab's existing headers, dedupe by job link) |
| `run_agent.py` | CLI for cron. `.github/workflows/daily-agent.yml` runs it on weekdays (set `RULES_JSON` as a repo secret) |
| `smoke_test.py` | Live check of every real source |
| `filter_service.py`, `ai_service.py` | v3 classic pipeline (keyword H1B, Claude match). Kept for `/search` |

Frontend: `frontend/index.html` (vanilla JS). Login gate (passcode, session token in `localStorage`, sent as `X-Session-Token` on every request) before the existing UI. Buttons: Search, Run Agent (+ Daily/Weekly window), Leads CSV/XLSX, Google Sheet, Send feedback (opens a pre-filled GitHub issue on the public repo; unrelated to the per-lead thumbs up/down `/feedback` endpoint).

## Multi-user
Added 2026-11-17, replacing the single shared `API_TOKEN` = one global user model noted as a gap in Security
below and in the old Roadmap. Two independent layers, deliberately not merged: `API_TOKEN` (unchanged) gates the
whole API from the public internet; a new passcode layer (`auth.py`, `POST /auth/login`) identifies *which*
authenticated user is calling, so `user_id` is never trusted from a client-supplied field anymore (it used to be --
`AgentParams.user_id` / `SheetExport.user_id` / `/leads?user_id=` -- meaning anyone who could reach the API at all
could read or write *any* other user's leads/profile just by naming their id). No self-serve signup: the owner
creates passcodes for testers with `backend/add_user.py <user_id> <passcode>`; there is no public registration path.

**Why a private GitHub repo, not Postgres/Supabase/Turso:** the owner's hard constraint is free forever, no new
third-party signups. Render's free web service has no persistent disk (confirmed: no `disk:` block in `render.yaml`,
and that tier doesn't support one) -- this is the same root cause as the 2026-11-16 OOM entry below, a different
symptom of "Render's free tier is not a place to keep state." A private GitHub repo the owner already has (GitHub
Pro/Student Pack, no new account) gives free, durable, versioned storage with a free API (Contents API for
reads, Actions API for writes-via-commit) and, as a bonus, moves the actual multi-minute scrape off Render's
single free web dyno entirely, onto GitHub Actions' free minutes (unlimited on a public repo, 2,000 min/mo on a
private one -- see the existing `daily-agent.yml` comment). JSON files, not the sqlite file itself, are the
source of truth committed there, so Render's read path is a plain Contents API GET + `json.loads`, not sqlite over
an API.

**Design:** a second, *private* repo (suggested name `jobfinder-data`, same owner) holds `data/<user_id>/leads.json`
+ `seen.json` per user -- kept out of this *public* repo per the Privacy rule above. `data-repo-template/` in *this*
repo has everything to copy into that new private repo: a `workflow_dispatch` GitHub Actions workflow
(`run-agent.yml`) that checks out this public repo for the scraper code, runs `scripts/run_for_user.py` (which
reuses `agent_service.AgentRun` + `store.py` completely unmodified, pointed at a throwaway sqlite file seeded from
the committed JSON so the existing dedupe logic needs zero changes), then commits the result back using the
workflow's own automatic `GITHUB_TOKEN` (`permissions: contents: write` -- no new secret needed in that repo).
Render's `backend/data_repo.py` is the client side: `GET /leads` reads from there via the Contents API,
`POST /agent/start` fires `workflow_dispatch` there (needs one fine-grained PAT, `DATA_REPO_TOKEN`, scoped to
*only* that repo, Contents + Actions read/write) and `GET /agent/jobs/{id}` polls the run instead of a local
thread. **This whole path is optional and inert by construction** (`data_repo.enabled()` is `False` until
`DATA_REPO_TOKEN`/`DATA_REPO` are set) -- unconfigured, everything behaves exactly as it does today, which is also
what local dev and the test suite exercise (no GitHub credentials available or needed there). The repo creation
and the one PAT are manual, owner-only steps (no `gh` CLI / token available to any agent in this environment to do
it for them) -- see `docs/MULTI_USER_SETUP.md`.

**Per-user profile:** `rules_service.load_rules(user_id=...)` checks `store.profiles` first; a user with no row
yet falls back to the same file-based resolution as before, so a brand-new tester needs zero setup. The in-app
filters overlay (`apply_filters()`, see above) is unchanged -- it's still a per-run, browser-only overlay on top
of whichever base profile `load_rules()` returns.

**Free-tier risk, flagged rather than silently built around:** GitHub Actions free minutes on a *private* repo are
2,000/month (public repos are unlimited). A full agent run today takes ~10-20 minutes (see Source behavior below).
At a handful of testers running this occasionally, that's nowhere close to the cap; it would become a real
constraint only with roughly 100+ runs/month, or if a single run's duration grows a lot. No runtime "about to hit
the cap" detector was built (there's no reliable way to check Actions minutes usage mid-design without added
complexity, and it's unnecessary at the discussed scale) -- if usage grows a lot, revisit by checking the private
repo's Settings -> Billing/Actions usage, or make that repo public (job-search leads are not secret, only the fact
of who is running this and their personal profile would be -- worth a deliberate decision, not a default).

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
- Multi-user (2026-11-17): `API_TOKEN` still gates the whole API; a second layer (`auth.py`, `POST /auth/login`)
  identifies which user is calling, so `user_id` is derived from a verified session token, never a client-supplied
  field. See "Multi-user" section above.

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
Env: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON or a path to the key file), `SHEET_ID`, `DB_PATH`, `RULES_PATH`, `RULES_JSON`, `RULES_LOCAL_PATH`, `API_TOKEN`, `ALLOWED_ORIGINS`, `JOBSPY_PROXIES`, `JOBSPY_TIMEOUT`, `AGENT_MAX_STEPS`, `SESSION_SECRET`, `SESSION_DAYS` (multi-user session tokens, see Multi-user), `DATA_REPO_TOKEN`, `DATA_REPO`, `DATA_REPO_WORKFLOW`, `DATA_REPO_BRANCH` (optional private-repo persistence, see Multi-user and `docs/MULTI_USER_SETUP.md`).

Add a tester: `cd backend && python add_user.py <user_id> <passcode>`, then give them the app's URL + their user_id + passcode (no self-serve signup). See `docs/MULTI_USER_SETUP.md`.

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
- 2026-11-16: hosted on Render's free tier, a full `/agent/start` run was observed dying mid-run (its `_jobs` entry vanished without ever reaching "done", and a concurrent request failed with a bare connection error) -- consistent with an OOM kill, since `self.pool` was holding ~3000+ fetched postings' full description text in memory for the whole run even though ~95%+ terminally fail title/geo/recency/sponsorship-language gating in the first pass. Fixed in `agent_service.search_jobs`: description is dropped immediately for any gate that can never change again, kept only for `pass`/`company_unverified`/`company_risk`/`staffing_c2c_only`.
- 2026-11-17: confirmed fixed live. Ran a full `/agent/start` on the production Render backend end-to-end via Claude in Chrome (network + console watched throughout): completed in 811s, scanned 2956 postings, 18 leads saved (3 new, 18 hot), zero dropped requests, zero console errors. Previously this crashed consistently at ~21s. No further action needed on this; if it recurs, re-check live before assuming the same root cause.
- Workday rate-limits per IP across tenants. Its list API gives only "Posted N Days Ago"; exact dates/locations/deadline come from the detail call (fetched for jobs <= 7 days old).
- Full agent run with all JobSpy sites ~10-20 min. Small local LLMs (llama3.1 8B) don't drive the tools reliably; the deterministic plan does the work.

## Roadmap (priority order)
1. Dice (or another free contract board) for the C2C track. Staffing-firm jobs found by the FT search are routed to C2C already.
   Investigated 2026-11: Dice has no working public API (`job-search-api.svc.dhigroupinc.com` 403s on the stale public
   key found in its JS bundle); only option is scraping its public search-results page (no login, but fragile to
   frontend changes and likely against ToS). Deliberately skipped for now -- revisit only with a clear free API, or if
   the user decides the scrape-and-accept-risk trade-off is worth it.
2. Contact finding (contact points in the score are always 0 today). Free + safe options are limited; avoid logged-in LinkedIn scraping.
3. ~~Multi-user~~ done 2026-11-17 -- see "Multi-user" section above. Still open under that umbrella: a real profile-editing UI
   (today a user's persisted profile can only be set via `store.save_profile()`/a future `POST /profile`, not the frontend --
   the in-app filters panel still only does its existing per-run overlay), and watching whether the private data repo's
   free Actions minutes (2,000/mo, private) ever becomes a real constraint (flagged, not yet a problem at current scale).
4. Optional LLM JD-vs-resume gap analysis for top leads only (token budget).
5. If LinkedIn 429s are still high after the location-limiting above, add optional proxies (`JOBSPY_PROXIES`) or drop LinkedIn's `results_wanted`.
