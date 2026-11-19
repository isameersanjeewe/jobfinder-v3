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
| `agent_service.py` | Agent loop. Cross-site dedupe (`dedupe_key`: company + title + city; text of duplicate copies is merged so any disclaimer still blocks). Plan researches unverified companies by best potential score, routes staffing jobs to the C2C track. h1bdata research is per year (last + current) and cached with expiry. Tools: `search_jobs`, `research_company_h1b`, `set_company_signal`, `score_jobs`, `save_leads`, `finish`. Providers: `LLM_PROVIDER=openai` (Ollama/Groq/Gemini, OpenAI-compatible), `anthropic`, or `none` (deterministic `_plan()` with the same tools). System prompt is filled from the profile. Small models: args are coerced and the plan runs as fallback if the LLM stops without saving. `AgentRun` loads this user's accumulated `/feedback` signals once per run (item 1) and accumulates this run's posting-level no-sponsorship detections, flushed to the shared company DB at run end (item 5, applies starting next run). `score_jobs()`/`save_leads()` attach `contact_link` (item 3) and `sponsorship_answer` (item 4) to every scored/saved lead, and refine `fit`/`fit_source` for the top `LLM_FIT_TOP_N` passing leads via `fit_service.llm_gap_fit()` (item 2) |
| `agent_mode.py` | **Agent tab (roadmap item 8, Phase A, owner-first).** One cycle: load memory (profile, feedback + lead statuses, watchlist, preferences) -> gather saved Full Search leads + watchlist postings via the existing Greenhouse/Lever/Ashby fetchers -> same gate/score as Full Search -> pick 3-4 leads (one batched LLM call, or deterministic ranking) -> apply kits (LLM or template) -> watchlist proposals into Approvals -> Activity log + Today brief. Never calls `agent_service` tools or `set_company_signal` (test-enforced). Job text is untrusted: LLM only reorders already-gated candidates (ids validated), output is stripped of links and payment/identity asks, kit bullets are fact-checked against the resume (heuristic), visa/sponsorship text comes ONLY from `sponsorship_answer()`. First LLM error stops LLM use for the cycle; kits are saved one by one and pending ones carry to the next cycle. CLI: `python agent_mode.py --user <id>` / `--due`. Tables in `store.py`: `agent_settings`, `lead_status` (`applied_at`), `watchlist`, `approvals`, `agent_activity`, `apply_kits`. **Agent targets** (2026-11-19): `agent_settings.filters` (same shape as Search filters: titles, locations, remote_ok) set in the Agent tab's "Where and what to look for" card; `rules_for()` overlays them with `apply_filters()`, and `/agentmode/settings` snapshots the result for the scheduler. Each cycle re-checks saved Full Search leads against title + geo (`_still_eligible`) and clears kits outside the targets that have no status. Endpoints `/agentmode/state`, `/agentmode/settings`, `/agentmode/approvals/accept-all` (owner decision 8), `/agentmode/run`, `/agentmode/status`, `/agentmode/approvals/{id}`, `/agentmode/watchlist` (all `Depends(current_user)`). Scheduler: `data-repo-template/.github/workflows/scheduled-agent.yml` + `scripts/run_agent_cycle.py` |
| `rules.example.json` | Committed profile template. Users copy it to `rules.json` (gitignored), or (multi-user) get it as their default until they have a saved profile row. Load order: per-user saved profile (if `user_id` given and found) -> env `RULES_JSON` -> env `RULES_PATH` -> `rules.json` -> `rules.example.json` |
| `rules_service.py` | Gate: title -> geo -> future date -> recency -> (if `visa.enabled`) **auto-reject company flag** (item 5) -> posting visa language (`No`/`Yes`/`Transfer`/`Unknown`; `Transfer` passes only with `visa.mode = h1b_transfer`) -> staffing firm (`staffing_c2c_only`) -> company H1B tier. Sets `job["gate"]` = `pass` or a reason. `load_rules(user_id=...)` prefers that user's saved profile (`store.profiles`) over the file-based one, and still merges per-user `rules.local.json` (persisted research; never overrides manual tiers). `save_company_signal()`/`record_no_sponsorship()` write through to the shared, durable `data/_company_signals.json` in the private data repo when configured (item 6); `sync_company_signals_from_repo()` pulls it back on startup. `sponsorship_answer()` is the item-4 answer bank |
| `free_sources.py` | JobSpy (each title x profile locations + remote US, deduped by URL, per-call timeout) + Greenhouse/Lever/Ashby public JSON + Workday (`slug: "tenant.wdN/site"`; serialized + 429 backoff) |
| `scraper_service.py` | Legacy: JSearch, Adzuna (keys), Remotive. Owns `normalize()` (shared job shape) |
| `fit_service.py` | `score_lead()`: 0-100 lead score (weights in profile `scoring`) + feedback bias (see Roadmap item 1), drives ranking + `hot_lead`. `keyword_fit()`: free resume keyword coverage; `fit=None` when no JD text (unknown, not zero). `llm_gap_fit()`: optional BYO-key LLM JD-vs-resume gap read for top leads only (item 2), falls back to `keyword_fit()`. `llm_configured()` is the one check every LLM-touching feature (this file, `outreach.py`) makes before calling out |
| `contacts.py` | `linkedin_people_url()`: deep link to a company's LinkedIn People-tab search, pre-filled (item 3). No scraping, no login, no stored contact data |
| `outreach.py` | `draft_outreach()`: drafts a short outreach note (item 7) via the BYO LLM key, or a deterministic template with zero network calls when none is configured. Generates only -- never sends; the user copies and sends it themselves |
| `store.py` | SQLite (`DB_PATH`): `leads` + `seen` tables keyed by `user_id` (drives "only new leads"); `users` (passcode auth) + `profiles` (per-user rules.json override) + `feedback` (thumbs up/down, item 1) tables added for multi-user, see below. Local cache/mirror, not the durable store once the private data repo is set up (see Multi-user) |
| `export_service.py` | CSV, XLSX, Google Sheets append (service account, append-only, maps to the tab's existing headers, dedupe by job link) |
| `run_agent.py` | CLI for cron. `.github/workflows/daily-agent.yml` runs it on weekdays (set `RULES_JSON` as a repo secret) |
| `smoke_test.py` | Live check of every real source |
| `filter_service.py`, `ai_service.py` | v3 classic pipeline (keyword H1B, Claude match). Kept for `/search` |

Frontend: `frontend/index.html` (vanilla JS). Login gate (passcode, session token in `localStorage`, sent as `X-Session-Token` on every request) before the existing UI. Buttons: Manual search, Run Full Search (+ Daily/Weekly window -- labeled "Full Search" in the UI as of 2026-11-18, not "Agent": it's a synchronous, deterministic multi-step pipeline by default, see "Is this actually agentic?" below), Leads CSV/XLSX, Google Sheet, Send feedback (opens a pre-filled GitHub issue on the public repo; unrelated to the per-lead thumbs up/down `/feedback` endpoint). Each Full Search row has a "⋯" expander (items 3/4/7): a LinkedIn contact deep link, the sponsorship-answer-bank text with Copy, and a "Draft outreach" button that calls `/outreach/draft` and shows the draft in a read-only textarea with its own Copy button -- never auto-sends anything. A "✦" by the Fit column marks an LLM-refined fit (item 2, only when a BYO key is configured).

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
  Strict since 2026-11-19 (`rules_service.geo_ok`): a board's remote flag no longer lets in a posting whose location names
  another state ("TX, USA" flagged remote by an aggregator was the leak); postings whose text says "remote" still pass when
  `remote_ok`. "ST, USA" (state code without a leading comma) matches the user's own states (`named_states()`).
- `visa`: `none` (no visa checks) | `new_sponsorship` | `h1b_transfer`. `sponsors_only` = company must be a confirmed Strong/Moderate H-1B sponsor (else unknown companies pass with fewer H1B points).
- `industries` (highest first -> score tiers 15, 13, 11...), `exclude_companies`.
Nothing role-specific is hardcoded: the default role family lives in the profile's `core_title_terms`.

## Security
- `API_TOKEN` env: when set, every endpoint except `/` and `/health` requires `Authorization: Bearer <token>` (the UI has a token field under Connection settings). **Any internet-facing deployment must set it.** Without it, anyone can start runs, read leads and append to your Sheet.
- `ALLOWED_ORIGINS` env (CORS, default `*`). Input limits on every request model; one agent run at a time (HTTP 429 otherwise); resume uploads capped at 5 MB / 20 pages and must be a PDF; server errors are logged, clients get a generic message.
- Untrusted scraped text: the UI escapes everything and only opens `http(s)` links (plus a CSP). CSV/XLSX cells starting with `= + - @` are prefixed with `'`. Sheets appends use `RAW` (never parsed as formulas).
- Dependencies: `pip-audit` runs in `run_live_test.sh`; requirement minimums are the first versions with no known CVEs. Keep them current.
- `ADMIN_TOKEN` env (2026-11-18): `/admin/*` needs header `X-Admin-Token` (plus the bearer when `API_TOKEN` is set). Unset = `/admin/*` disabled (403). Never reuse `API_TOKEN` for it -- testers may hold `API_TOKEN`.
- Session hardening (2026-11-18): empty `SESSION_SECRET` no longer signs with an empty key (random per-process fallback, so forged tokens fail; sessions reset on restart). `current_user` rejects tokens of removed users. `/auth/login` locks a user_id for 15 min after 5 wrong passcodes. Signed-in users' thumbs no longer write the legacy global `feedback_store`. User ids can't contain `..` or start with `.`.
- Auto-reject (2026-11-18 fix): only postings that passed title/geo/recency count (gate `posting_no_sponsorship`), each posting URL counts once across runs (hashes kept per company, capped at 50), and the run-end flush is one batched write (`record_no_sponsorship_hits`), not one data-repo commit per company.
- Multi-user (2026-11-17): `API_TOKEN` still gates the whole API; a second layer (`auth.py`, `POST /auth/login`)
  identifies which user is calling, so `user_id` is derived from a verified session token, never a client-supplied
  field. See "Multi-user" section above.

## Run / test
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r tests/requirements-dev.txt
cp backend/rules.example.json backend/rules.json   # then edit your profile
cp backend/.env.example backend/.env               # optional: Sheets + LLM settings
.venv/bin/python tests/test_agent.py          # offline, mocked sources; must print ALL OK
.venv/bin/python tests/test_core_regression.py # offline: auth/isolation/injection/auto-reject regressions; ALL OK
.venv/bin/python tests/test_agent_mode.py     # offline: Agent tab cycle, kits, fact-check, endpoints; ALL OK
.venv/bin/python tests/test_agent_mode_qa.py  # offline: hostile-JD injection, isolation, LLM-error paths; ALL OK (1 known gap: lowercase invented tool names)
.venv/bin/python tests/test_agent_targets.py  # offline: strict location picks, Agent targets, saved-lead re-check, accept-all; ALL OK
bash run_live_test.sh                          # audit + offline + live sources -> logs/
cd backend && ../.venv/bin/python smoke_test.py --quick   # live sources + H1B filter checks, ~2 min
cd backend && ../.venv/bin/uvicorn main:app --reload   # API on :8000
```
Env: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON or a path to the key file), `SHEET_ID`, `DB_PATH`, `RULES_PATH`, `RULES_JSON`, `RULES_LOCAL_PATH`, `API_TOKEN`, `ALLOWED_ORIGINS`, `JOBSPY_PROXIES`, `JOBSPY_TIMEOUT`, `AGENT_MAX_STEPS`, `SESSION_SECRET`, `SESSION_DAYS` (multi-user session tokens, see Multi-user), `ADMIN_TOKEN` (owner-only `/admin/*`, see Security), `DATA_REPO_TOKEN`, `DATA_REPO`, `DATA_REPO_WORKFLOW`, `DATA_REPO_BRANCH` (optional private-repo persistence, see Multi-user and `docs/MULTI_USER_SETUP.md`), `LLM_FIT_TOP_N` (default 5, roadmap item 2), `LLM_FIT_TIMEOUT`, `LLM_OUTREACH_TIMEOUT` (item 7), `AUTO_REJECT_THRESHOLD` (default 2, item 5). Agent mode: `AGENT_KITS_PER_CYCLE` (4), `AGENT_LLM_MAX_CALLS_PER_CYCLE` (6), `AGENT_CYCLE_TOKEN_BUDGET` (18000), `AGENT_LLM_MIN_GAP_SECONDS` (4), `AGENT_LLM_TIMEOUT` (30), `AGENT_SHORTLIST` (10), `AGENT_POOL_DAYS` (7), `AGENT_WATCH_HOURS` (72), `AGENT_WATCHLIST_MAX` (25), `AGENT_FOLLOW_UP_DAYS` (7).

Add a tester: `cd backend && python add_user.py <user_id> <passcode>`, then give them the app's URL + their user_id + passcode (no self-serve signup). See `docs/MULTI_USER_SETUP.md`.

## How to work in this repo
- **Project skills** (`.claude/skills/`, improve them after each use via their `## Lessons` section): `jobfinder-verify` (all offline suites + privacy scan, run before any commit/"done"), `jobfinder-release` (gate -> commit -> push -> live checks), `jobfinder-agent-team` (PM/Dev/QA/Architect/Adversarial/Ops lanes, relay, merge), `jobfinder-llm-safety` (untrusted JDs, visa text only from `sponsorship_answer()`, fact-check, budgets).
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

## Agent (true automation) -- design settled 2026-11-18; Phase A v1 built 2026-11-18 (uncommitted, see agent_mode.py)
Known v1 gaps: Render and the scheduled workflow can both write `data/<user>/agent.json` (a UI change during a cycle can be lost); watchlist leads reach `leads.json` only from scheduled runs when the data repo is on; fact-check is heuristic; both data-repo workflows share the `data-repo-writes` concurrency group (GitHub keeps only ONE pending run per group: a newer pending run cancels an older pending one -- fine at owner scale, revisit for 15 users); scheduled commits are whole-file (never hunk-merged JSON); set data-repo variable `APP_REF` to a reviewed commit SHA to pin the app code; Web Push not built (A2). Open review items (adversarial/architect, see `docs/*_REVIEW.local.md`): generic `rules.example.json`, owner profile as default for other users, `leads.json` 1MB Contents-API limit, Actions runs not persisting company signals, shared `llm.py` refactor.
Full spec, acceptance criteria, data models and endpoints: `docs/AGENT_SPEC.md`. Owner-only manual steps: `docs/OWNER_SETUP.md`.

**Product split (the user must tell them apart at a glance):**
- **Search tab = "you drive."** Manual search, Run Full Search, filters, exports, thumbs. Runs only when clicked (roadmap 1-7 live here).
- **Agent tab = "works for you."** On/off switch + "on duty" line (last/next run), its own accent color, **Today brief**,
  **Activity feed** ("what I did and why"), **Approvals inbox** (proposed preference/watchlist changes), **Apply kits**
  per top lead (tailored bullets, short cover note, screening answers incl. sponsorship answer, LinkedIn contact link).
  Agent-found leads carry an "Agent" badge everywhere. Thumbs/status from either tab feed the same memory.
- **Hard rule:** the agent never applies or sends anything. Drafts only; a human approves anything that changes its plan.

**Capabilities (ranked):** (1) per-user company watchlist polled every few hours via the existing Greenhouse/Lever/Ashby
fetchers in `free_sources.py`, seeded from profile + thumbs-up history + H1B signal DB; (2) apply kits for the top 3-5
new leads (LLM picks, logs why it skipped others); (3) pipeline follow-ups (status Applied/Interview/Rejected: draft
follow-up after 7 days unchanged, flag vanished postings, interview prep pack); (4) weekly self-review proposing
preference/title/source/watchlist changes, applied only on approval; (5) daily Today brief, shown at top of the Agent tab and sent by browser Web Push (VAPID, no external account).

**Memory:** the LLM remembers nothing between runs. Each run reloads profile settings, a plain-sentence **Preferences**
list (changed only by approved weekly review or direct edit), recent statuses + thumbs, and the watchlist. Raw counts
feed `fit_service.feedback_bias`; preference sentences + notes go to the LLM as context. Personal opinions never go in the
shared company-signal DB -- only facts (e.g. a posting says no sponsorship).

**Storage:** Phase A (owner only) = existing `store.py`/SQLite + private data repo JSON. Phase B (multi-user) = each user's
Google Sheet in their own Drive is the source of truth (tabs Leads, Preferences, Watchlist, Activity), kits/prep packs as
Google Docs in a "JobFinder" folder, OAuth scope `drive.file`, consent screen in Production. Owner side keeps only
seen/dedupe, shared H1B DB, profile settings, encrypted tokens, usage counters.

**LLM:** Phase A = owner's key via existing `LLM_PROVIDER=openai` (Groq/Gemini). Phase B = per-user OpenRouter OAuth PKCE
(user-owned key, encrypted, usable by scheduled runs); pasted Gemini key fallback. Puter.js rejected (browser-only).
LLM only on top leads, kits, weekly review -- never the full ~3000-posting pool. Warn users free providers may log prompts.

**Infra/limits:** one SHARED scrape per weekday in the private data repo's scheduled workflow saves the pool; per-user
scoring against it is cheap. Manual Run Full Search re-scores today's pool; "Fresh scrape now" 1/user/day. Budget: GitHub
Pro 3,000 Actions min/mo on the private repo (a full run ~10-20 min; 811s observed). Phase B adds Google sign-in (ID token
verified server-side), `MAX_USERS` seat cap (15) + waitlist + owner push on join, owner-only admin view (approvals, minutes
used vs 3,000). Passcode login stays as owner backup.

**Naming:** existing `/agent/*` endpoints are the Full Search pipeline and are NOT renamed; new Agent-tab endpoints live under `/agentmode/*`.

**Phases:** A = owner-only Agent v1 (tab, daily loop, watchlist, kits, brief, activity, approvals; push may be A2).
B = multi-user foundation (Google sign-in, seats/waitlist/admin, Drive sheet, OpenRouter PKCE, encrypted tokens).
C = follow-ups/interview prep + weekly self-review for all users. D = measure (kits used, Today picks applied to), cut what is unused.

## Roadmap (priority order, updated 2026-11-18; items 1-7 landed 2026-11-18)
1. **DONE. Wire feedback into scoring.** Thumbs up/down (`POST /feedback`) used to only affect the legacy
   `/search` pipeline (`ai_service.apply_feedback_boost`) -- `fit_service.score_lead()`, what actually scores
   Full Search leads, never looked at it. Fixed: `store.py` has a `feedback` table (`save_feedback()` /
   `feedback_signal_counts()`, per-user, local SQLite -- same durability tier leads/seen have before the
   private-repo setup); `fit_service.feedback_bias()` turns accumulated company + title-pattern (seniority-
   stripped) up/down counts into a +/-10-clamped adjustment folded into `score_lead()`'s 0-100 total (new
   `score_parts.feedback`). `AgentRun` loads the calling user's signals once per run. `/feedback` persists
   per-user when the caller has a session (every Full Search UI call does); the legacy global `feedback_store`
   is untouched, so `/search` behaves exactly as before.
2. **DONE. Fix resume fit scoring.** `fit_service.keyword_fit()`'s small fixed 120-term SKILLS list made one
   match/miss swing the score 33-50 points. Fixed: `fit_service.llm_gap_fit()` is an optional BYO-key LLM
   JD-vs-resume gap read (same `LLM_PROVIDER`/`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` pattern as
   `agent_service.py`'s header -- no new provider abstraction), applied in `AgentRun.score_jobs()` to only the
   top `LLM_FIT_TOP_N` (default 5) passing leads per run. No key configured (this deployment's current state)
   -> `llm_configured()` is False -> `keyword_fit()`'s result is used untouched, zero network calls. Each
   scored lead now carries `fit_source` ("llm" or "keyword"); the UI marks LLM-refined fits with "✦".
3. **DONE. Contact finding.** `contacts.linkedin_people_url()` builds a deep link to a company's LinkedIn
   People-tab search, pre-filled with the company name plus a hiring-relevant role hint from the job title.
   No login, no scraping, no stored contact data. Attached to every scored/saved lead as `contact_link`;
   does **not** change scoring -- `contact_points` stays 0 until there's a way to confirm an actual named
   contact, which this deliberately isn't.
4. **DONE. Sponsorship-question answer bank.** `rules_service.sponsorship_answer()`, keyed to `visa.mode`
   (`new_sponsorship` vs. `h1b_transfer`; `None` when `visa.enabled=false`), with an optional per-profile
   override (`visa.sponsorship_answer` in `rules.json`). Attached to every scored/saved lead as
   `sponsorship_answer`; the UI shows it with a Copy button.
5. **DONE. Auto-reject-on-sponsorship company flag.** Separate from the existing LCA-tier `company_signal`.
   `AgentRun` accumulates this run's `posting_visa == "No"` detections per company (deduped by URL) and
   flushes them into the shared company-signal store at run end via `rules_service.record_no_sponsorship()`,
   which sets `auto_reject=true` once a company crosses `AUTO_REJECT_THRESHOLD` (default 2) -- *starting next
   run*, not retroactively within the same run. `apply_gate()` checks `company_auto_reject()` first in the FT
   visa branch, producing a new `company_auto_reject` gate reason distinct from `posting_no_sponsorship`
   (single posting) and `company_risk`/`company_excluded` (LCA tier) -- a company can auto-reject regardless
   of its LCA signal or this specific posting's own wording.
6. **DONE. Growing H1B company-signal database.** This was the single most important item to get right.
   The per-company record (`rules.local.json`'s `company_signals` map -- already a single global file, so
   already shared across users in one deployment) now also durably write-throughs to the private data repo at
   `data/_company_signals.json` (same pattern as `data/_users.json`) on every `save_company_signal()` /
   `record_no_sponsorship()` call, and `sync_company_signals_from_repo()` pulls it back at backend startup
   (alongside `store.sync_users_from_repo()`) -- so a freshly redeployed Render instance (local disk wiped)
   doesn't start research/auto-reject cold. LCA tier (`signal`/`basis`/`date`) and posting-level accumulation
   (`no_sponsorship_count`/`auto_reject`) live in the *same* per-company record (one source of truth per
   company) -- `save_company_signal()` merges rather than replaces so neither clobbers the other. This is
   optional/inert exactly like the rest of Multi-user: unconfigured, it's the same single local file as
   before, just now also fed by `record_no_sponsorship()`.
7. **DONE. Draft outreach notes in-app.** `outreach.draft_outreach()`, BYO LLM key (same pattern as item 2)
   or a deterministic template with zero network calls when none is configured -- drafting must never
   require payment. **Generates only, never sends** -- `POST /outreach/draft` returns the text; the UI shows
   it in a read-only textarea with a Copy button. Consistent with the existing hard rule that this project
   never submits applications or sends messages for the user.
8. **Agent (true automation)** -- design settled, phases A-D, see "Agent (true automation)" above and
   `docs/AGENT_SPEC.md`. Reuses items 1-7 (scoring, contact link, sponsorship answer, outreach drafting, feedback).
9. Dice (or another free contract board) for the C2C track. Investigated 2026-11: no working public API
   (`job-search-api.svc.dhigroupinc.com` 403s on a stale key) -- only option is scraping its results page, fragile
   and likely against ToS. Deliberately skipped; revisit only with a real free API or if the scrape-risk trade-off
   is accepted.
10. A real profile-editing UI (today a saved profile can only be set via `store.save_profile()`, not the
    frontend -- the in-app filters panel still only does its per-run overlay). Watch whether the private data
    repo's free Actions minutes (3,000/mo on GitHub Pro, private) ever become a real constraint once Agent (item 8) adds
    scheduled runs on top of on-demand ones -- not yet a problem at current scale.
11. If LinkedIn 429s climb again, add optional proxies (`JOBSPY_PROXIES`) or drop LinkedIn's `results_wanted`.
