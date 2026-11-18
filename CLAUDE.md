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

Frontend: `frontend/index.html` (vanilla JS). Login gate (passcode, session token in `localStorage`, sent as `X-Session-Token` on every request) before the existing UI. Buttons: Manual search, Run Full Search (+ Daily/Weekly window -- labeled "Full Search" in the UI as of 2026-11-18, not "Agent": it's a synchronous, deterministic multi-step pipeline by default, see "Is this actually agentic?" below), Leads CSV/XLSX, Google Sheet, Send feedback (opens a pre-filled GitHub issue on the public repo; unrelated to the per-lead thumbs up/down `/feedback` endpoint).

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

## Is "Run Full Search" actually agentic? (2026-11-18)
Honest answer: **not as deployed.** No `LLM_PROVIDER` is set on Render, so every click runs `agent_service.py`'s
deterministic `_plan()` fallback -- a fixed-order pipeline (search -> research unverified companies -> score ->
save -> finish), not a model choosing anything. Even with an LLM configured it would only be mildly agentic: one
bounded tool-call loop per click, no initiative, no operation over time. Renamed the user-facing button/copy from
"Run Agent" to "Run Full Search" to stop overclaiming (frontend/index.html, this file) -- `agent_service.py`,
`AgentRun`, and the `/agent/*` endpoints were deliberately left as-is (internal naming, real rename risk for zero
user-facing benefit, touches the private-repo workflow contract). The real agentic piece belongs in a genuinely
separate component -- see "Agent (true automation)" below, not yet built.

## Agent (true automation) -- design, not yet built (2026-11-18)
What actually makes something "agentic" in the sense the user meant: operating with initiative over time without
being asked each time, and proactively telling the user something worth their attention. That's a different
shape of problem from "Run Full Search," and should be a new, separate, opt-in component, not a rename of the
existing button.

**Design:**
- **Opt-in per user**, configured in-app: enable/disable, a check interval (e.g. every 6h/12h/24h), and
  optionally their own LLM provider + API key (BYO -- see below).
- **Scheduling infra**: reuse what's already built, don't invent new infra. Render's free plan can't run a
  reliable background cron (sleeps when idle). The private data repo (`docs/MULTI_USER_SETUP.md`) already has a
  GitHub Actions workflow per user-triggered run; add ONE additional scheduled workflow (fixed cron, e.g. hourly)
  that iterates over every user with Agent enabled, checks whether their chosen interval has elapsed, and if so
  runs `scripts/run_for_user.py` for them (same script Full Search already uses) -- zero new infra, one new
  workflow file.
- **BYO LLM key, not a shared one**: each user who wants their scheduled runs to be genuinely LLM-driven (real
  tool-selection, not the deterministic fallback) supplies their own key from a free-tier provider (Groq +
  an open-source model recommended -- see below). This is the only design that keeps the shared deployment's
  cost at exactly $0 regardless of how many users enable Agent mode, since each person's LLM usage is billed
  (or free-tier-limited) on their own account, not the owner's.
  - **New responsibility this introduces**: unlike every other secret in this project (always typed by a human
    into a dashboard, never stored by the app), a per-user LLM key has to persist server-side to run unattended
    on a schedule. Store it with the same care as a passcode hash -- never log it, never return it from any read
    endpoint, and prefer encrypting it at rest (not just relying on DB access control) given it's a live,
    usable credential sitting in storage, unlike a passcode hash which is one-way.
- **Alerts, browser first**: Web Push (service worker + push subscription per device, self-generated VAPID
  keypair -- no external account needed, genuinely free) sent from the scheduled workflow when a run finds a
  new hot lead. Email is real extra surface (a transactional email provider account, another credential) --
  deliberately deferred; browser push covers the core "tell me when something good shows up" need without it.
- **What decides "worth alerting"**: a simple hot-lead-count threshold is a fine v1 (no LLM needed for this
  part even in LLM-driven mode) -- don't over-build judgment logic here before the basic loop is proven.

**Why this design** (validated, not just proposed): it correctly separates "I search when I ask" (Full Search,
free, synchronous, already built) from "it watches and tells me" (Agent, opt-in, autonomous, BYO-cost) -- which
is the actual distinction users mean by "agent" vs. "tool." It reuses 100% of existing infra (private data repo,
run_for_user.py, the data_repo.py pattern) rather than inventing a new automation substrate. And it sequences
correctly: push notifications before email, because push is free-by-construction and email isn't.

**Recommended LLM for BYO**: Groq, serving an open-source model (Llama 3.3 70B or newer) -- free tier, no card,
OpenAI-compatible (zero new code, `LLM_PROVIDER=openai` + `LLM_BASE_URL` already supports it), and plenty
accurate for the scoped tasks below (structured extraction + short-form writing, not frontier reasoning).

## Roadmap (priority order, updated 2026-11-18)
1. **Wire feedback into scoring.** Thumbs up/down (`POST /feedback`) currently only affects the legacy
   `/search` pipeline (`ai_service.apply_feedback_boost`) -- `fit_service.score_lead()`, what actually scores
   Full Search leads, never looks at it. Today it's functionally decorative for the main workflow. Fix: downweight/
   upweight future leads by company and title pattern based on accumulated feedback signals.
2. **Fix resume fit scoring.** `fit_service.keyword_fit()` does percentage-of-matched-terms against a small fixed
   120-term SKILLS list -- when a JD only mentions 2-3 of those terms, one match/miss swings the score 33-50
   points, which reads as "random" even though it's deterministic. Fix with an LLM JD-vs-resume gap analysis for
   top leads only (token/rate-limit budget), via the BYO Groq key above -- falls back to the current keyword
   method when no key is configured.
3. **Contact finding.** `contact_points` in the score is always 0 today. Free + safe option: a deep-link
   generator to a company's LinkedIn People-tab search (pre-filled query), not logged-in scraping.
4. **Sponsorship-question answer bank**, keyed to the user's visa mode (new sponsorship vs. transfer), surfaced
   per lead -- a consistent, pre-written answer to the near-universal ATS screening question, not improvised
   per application.
5. **Auto-reject-on-sponsorship company flag**, separate from the existing LCA-tier `company_signal`. Source
   from accumulated `posting_visa == "No"` detections per company (the regex detector already exists) rather
   than one posting's language -- a company flag is different signal from a single posting's language.
6. **Growing H1B company-signal database.** Today `research_company_h1b` researches a company fresh (or from
   the user's own `rules.local.json`) every time it's unknown. Make the result durable and shared: write every
   research result into the same persistent store the multi-user leads/users data already uses (the private
   data repo), so the database only ever grows, and a new lookup checks that accumulated store *before*
   re-researching -- "suggest from what we already know" instead of starting cold each time. This is the
   natural next step on top of the per-user data_repo.py pattern already built, not a new subsystem.
7. **Draft outreach notes in-app**, via the BYO LLM key -- never auto-send, consistent with the existing rule
   that this project never submits applications or sends messages on the user's behalf.
8. **Agent (true automation)** -- see full design above. The biggest item here; everything above (1-7) should
   land first since the Agent's scheduled runs just reuse the same scoring/matching/contact/outreach pipeline.
9. Dice (or another free contract board) for the C2C track. Investigated 2026-11: no working public API
   (`job-search-api.svc.dhigroupinc.com` 403s on a stale key) -- only option is scraping its results page, fragile
   and likely against ToS. Deliberately skipped; revisit only with a real free API or if the scrape-risk trade-off
   is accepted.
10. A real profile-editing UI (today a saved profile can only be set via `store.save_profile()`, not the
    frontend -- the in-app filters panel still only does its per-run overlay). Watch whether the private data
    repo's free Actions minutes (2,000/mo, private) ever become a real constraint once Agent (item 8) adds
    scheduled runs on top of on-demand ones -- not yet a problem at current scale.
11. If LinkedIn 429s climb again, add optional proxies (`JOBSPY_PROXIES`) or drop LinkedIn's `results_wanted`.
