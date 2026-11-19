# JobFinder progress log

What has been built, deployed and decided, and what's next. Read this after CLAUDE.md when resuming work.
No personal values here (public repo): the owner's private notes are in `CLAUDE.local.md`.

## Current state (2026-11-19)
- **Live:** backend on Render deploys from `main` (live commit `62259f0`; later commits are docs/skills only).
  Frontend on Vercel (`frontend/index.html`, `frontend/admin.html`). Branch `agentic-v4` = `main` + docs commits.
- **Production is locked:** `API_TOKEN`, `SESSION_SECRET`, `ADMIN_TOKEN` and `RULES_JSON` are set on Render, and `GET /` shows `"auth": true`.
- **AI is on:** Groq via `LLM_PROVIDER=openai`, `LLM_BASE_URL=https://api.groq.com/openai/v1`,
  `LLM_MODEL=openai/gpt-oss-120b`, `LLM_API_KEY` (owner's key), on Render and as Actions secrets in the private
  data repo. The AI picks the leads that get kits and writes skip reasons. Kits stay templates until the user saves a resume.
- **Agent:** owner's Agent is on duty (daily), with targets set to the owner's profile places + remote. The data repo
  runs `scheduled-agent.yml` (weekdays 12:20 and 21:20 UTC).
- **Tests:** 5 offline suites, all passing (`bash .claude/skills/jobfinder-verify/scripts/verify.sh`).

## Commits this session (oldest first)
| Commit | What |
|---|---|
| `9a1f8e1` | Security hardening (ADMIN_TOKEN, session secret fallback, removed-user tokens rejected, login lockout), auto-reject fix, Agent v1 (Search vs Agent tabs) |
| `cf59fe5` | Agent spec, owner setup checklist, QA plan, 4 project skills |
| `8998d6a` | CORS headers on 401s (the browser showed 503 instead of "wrong token") |
| `9ff01f3` | Strict location picks, Agent targets card, accept-all approvals, UI rework |
| `cc7a0b9` | Owner decisions recorded, stale v3 summary files removed |
| `6b252aa` | Code-review fixes: trusted employer-board remote flags, "Austin TX" without a comma, no places = anywhere, Agent writes load data-repo state first |
| `24b7b2a` | LLM env values trimmed; Activity shows the provider's error code (e.g. `model_not_found`) |
| `6f5a392` | Groq free plan: 15 s between Agent LLM calls; gpt-oss gets low reasoning effort + reply headroom |
| `2e6d2ca` | Agent tab resume upload (auto-saves) + "Add your resume" reminder; a Search-tab upload also saves for the Agent |
| `62259f0` | Admin page sends `X-Admin-Token` (adding users had been broken since ADMIN_TOKEN was required) |
| `d17aea9` | Release skill lesson |

Private data repo `jobfinder-data`: commit `9e01e91` added `scheduled-agent.yml` and `scripts/run_agent_cycle.py`, and gave
`run-agent.yml` the shared `data-repo-writes` concurrency group. The `APP_REF` variable is not set (no `gh` CLI), so runs use `main`.

## What was built (by area)
**Locations (the "Texas leak").** Root causes: (1) Full Search leads saved before `RULES_JSON` was set on Render used the
example profile (Austin, TX); (2) JobSpy flags some postings remote even when they name a state ("TX, USA").
Fix in `rules_service.geo_ok()`:
- A posting must match a picked place, or be truly remote when `remote_ok` is on.
- Remote flags from employer boards (`ATS/*`, Remotive) are trusted even with an HQ city.
- Aggregator flags are not trusted when the location names another state.
- `named_states()` reads "TX, USA", "Austin TX" and "austin, tx".
- No places set = anywhere.

**Agent targets.** Agent tab card "Where and what to look for": locations as chips, include remote, titles. Saved per user
in `agent_settings.filters`, used by Run now and by scheduled runs (snapshot). When targets are set, each cycle also
re-checks saved leads and clears kits outside the targets that have no status.

**Approvals.** `POST /agentmode/approvals/accept-all` accepts watchlist additions only. Plan changes stay one-by-one.

**UI.**
- Layout: sticky header + tabs, 40 px controls, Full Search card with a 24 h / 7 day toggle, compact ✓/✕ approvals, kit
  detail card, Activity shows the latest 6.
- Loading: retries while Render's free instance wakes up; the Search tab opens on saved leads.
- Kits: honest location answers (no false "fits"), no filler gap words.
- Checked at phone width.

**Reliability.** Render wipes SQLite on every deploy. All `/agentmode/*` write endpoints now load the user's state from
the data repo first (`_agentmode_load`), so a write can't sync an empty state over the durable copy.

**Admin.** `frontend/admin.html` takes API_TOKEN (remembered) and ADMIN_TOKEN (kept for the tab session only).

## How-tos
- **Add a tester:**
  1. Open `/admin.html` and enter both tokens (values from the Render Environment tab).
  2. Enter a user ID, click 🎲 for a passcode, then **+ Add tester**.
  3. Send them the app URL, their user ID + passcode, and the API_TOKEN (they paste it under Search → Connection settings).
  Users persist in the data repo (`data/_users.json`, passcodes hashed).
  On a new device, the tester opens "First time on this device?" on the login screen and pastes the app access token
  (API_TOKEN) once (`5d78831`; before that, new testers could not sign in at all).
  To reset a passcode, use "Add tester" again with the same user ID. Passcodes can be checked offline against
  `data/_users.json` with `auth.verify_passcode()`, without logging in.
- **Where tester data lives today:** private repo `jobfinder-data`:
  - `data/<user>/leads.json` and `seen.json`
  - `data/<user>/agent.json` (settings, targets, resume text, notes, kits, thumbs, statuses, watchlist, activity)
  - shared `data/_company_signals.json`

  The owner can read all of it, and testers use the owner's Groq quota and Actions minutes.
- **Change the AI model:** set `LLM_MODEL` on Render (auto-redeploys) and in the data repo's Actions secrets.
  Check https://console.groq.com/docs/models: `llama-3.3-70b-versatile` became Enterprise-only (404 `model_not_found`).
  Free plan for gpt-oss-120b: 30 requests/min, 1K requests/day, 8K tokens/min, 200K tokens/day.
- **Deploy:** push `agentic-v4`, then `git push origin agentic-v4:main` (Render auto-deploys `main`). Browsers may
  cache the old frontend: hard refresh (Cmd+Shift+R).

## Owner decisions (2026-11-19, all eight accepted; details in docs/AGENT_SPEC.md §12)
1. Fresh scrape: 1 per user per week.
2. LLM key in both Render and Actions secrets.
3. Owner approves every user.
4. `OWNER_EMAIL` sets the Phase B admin.
5. Owner records go in the private data repo.
6. Cut any feature used by <20% of users after 4 weeks.
7. iPhone: in-app brief only, plus a Home Screen tip.
8. First watchlist approved as one batch.

## Known issues / open items
1. **Watchlist suggestions** name companies whose boards the watchlist can't read (Amazon, Google, Bloomberg: not on
   Greenhouse/Lever/Ashby). Only suggest companies with a readable board (`agent_mode.board_from_url`), or add Workday
   to watchlist polling. Until then, don't accept those approvals.
2. **New users start from the example profile (Austin, TX)** until they set filters/targets. Needs a neutral
   `rules.example.json` (tests depend on it; change them together).
3. `leads.json` may exceed the GitHub Contents API's 1 MB read limit as it grows.
4. Actions runs don't persist company signals; there is no shared `llm.py` (three copies of LLM call code).
5. Web Push (A2) not built.
6. Scheduled Agent: the 12:20 UTC cron on 2026-11-19 never fired (GitHub cron is best-effort). A manual dispatch
   worked (run 44551360104: "Anyone due?" -> nobody due, exited). Re-check after the 21:20 UTC slot.
   Fixed 2026-11-19: Full Search runs in the data repo failed at the save step whenever Render wrote meanwhile (5 of
   10 runs, leads lost). run-agent.yml now retries from the latest remote (app `data-repo-template`, data repo `fcecf2a`).
   Runs/logs can be read via the GitHub API with the stored git credential (`gh` is not installed).
7. Browser checks of github.com aren't allowed in the Chrome extension (Actions secrets can't be viewed there).
8. Optional: an automatic AI fallback (Groq → Gemini) via `LLM_FALLBACK_*` vars. Not built.

## Next: Phase B (Google sign-in, per-user Drive + AI key). Owner said "okay" on 2026-11-19; nothing built yet
Plan (design in CLAUDE.md "Agent" section and docs/AGENT_SPEC.md):
1. Google sign-in (ID token verified server-side) + owner approval, seat cap 15 + waitlist, passcode stays as owner backup.
2. Per-user Google Sheet in their own Drive (`drive.file` scope) as the source of truth.
3. Per-user AI key (OpenRouter OAuth PKCE, or a pasted Gemini key), stored encrypted (`ENCRYPTION_KEY`).

Facts checked before writing the owner's Google setup guide (verify against current Google docs):
- `drive.file` is a **non-sensitive** scope (only basic app verification).
- Refresh tokens **expire after 7 days while the consent screen is in "Testing"**. Scheduled runs need offline
  access, so the app must be **published to Production**; users then re-consent once.

Sources: [Choose Google Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[Refresh token expires after 7 days (Testing status)](https://groups.google.com/g/adwords-api/c/nmIY_WDNyTU/m/uc7Bk0PmBgAJ).

Owner's part (guide still to be written as `docs/GOOGLE_SETUP.md`):
1. Create a Google Cloud project and enable the Drive + Sheets APIs.
2. Set up the OAuth consent screen (External; app home page; a privacy page `frontend/privacy.html`, not yet written)
   and publish it to Production.
3. Create a Web OAuth client (origin = the Vercel app, redirect = the Render backend callback). Put
   `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` on Render.
4. Set `ENCRYPTION_KEY` and `OWNER_EMAIL` on Render.

## Lessons (also in .claude/skills/*/SKILL.md)
- Wait for `/code-review` to finish before pushing to `main`; it found 4 real bugs after a push.
- When endpoint auth changes, update every client (admin.html broke).
- Render wipes SQLite on deploy: any per-user write endpoint must load the data-repo state first.
- Never trust a remembered model name: provider catalogs change (check the provider's models page).
