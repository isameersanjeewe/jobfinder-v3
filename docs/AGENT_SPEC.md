# Agent spec (v1 design, settled 2026-11-18)

Working spec for the Agent feature. Product decisions here are settled by the owner; open questions are listed at the end.
No personal data belongs in this file (public repo). Companion: `docs/OWNER_SETUP.md` (manual owner steps).
Background: `CLAUDE.md` ("Agent (true automation)"), `docs/CONTEXT.md` (scoring, sponsorship method).

## 1. Product split

| | Search tab ("you drive") | Agent tab ("works for you") |
|---|---|---|
| Trigger | Only when the user clicks | Scheduled, runs while the user is away |
| Features | Manual search, Run Full Search, filters, exports, thumbs (roadmap 1-7) | On/off switch, "on duty" line, Today brief, Activity feed, Approvals inbox, Apply kits |
| Look | Existing UI | Distinct accent color, "on duty" status |
| Leaving the page | Nothing happens | Agent keeps working |

Rules that apply everywhere:
- **Never applies or sends anything.** Drafts only. Anything that changes the agent's own plan (preferences, titles, sources, watchlist) needs explicit user approval.
- Leads found by the agent show an **"Agent" badge** in both tabs (field `found_by = "agent"`; Search-tab leads are `"search"`; a lead found by both shows Agent).
- Thumbs and status set in either tab write to the same feedback/status store (one memory).
- `/agent/*` endpoints are the Full Search pipeline and are **not renamed**. Agent-tab endpoints use `/agentmode/*`.

## 2. Capabilities (ranked)

1. **Company watchlist.** Per-user list of companies polled every few hours through the existing `free_sources` Greenhouse/Lever/Ashby fetchers (`fetch_company_board`). Seeded from: profile company tiers, companies of thumbs-up leads, Strong/Moderate companies in the shared H1B signal DB that have a known ATS board. New postings pass through the normal gate + `score_lead` (no new rules).
2. **Apply kits** for the top 3-5 new leads per day. The LLM decides which deserve a kit and writes a one-line skip reason for the others to the Activity feed. Kit = tailored resume bullets, short cover note, likely screening-question answers (including the sponsorship answer from `rules_service.sponsorship_answer`), LinkedIn contact link (`contacts.linkedin_people_url`).
3. **Pipeline follow-ups.** User sets status Applied / Interview / Rejected on a lead. Agent: drafts a follow-up note after 7 days with no status change; flags postings that disappeared from their board; builds an interview prep pack when status becomes Interview.
4. **Weekly self-review.** Proposes preference, title, source and watchlist changes from recent thumbs/statuses. Lands in the Approvals inbox; applied only on accept.
5. **Daily Today brief.** Shown at the top of the Agent tab and sent by browser Web Push (VAPID, no external account). Example: "Apply to these 2 (kits ready); follow up with 1; prep for interview."

## 3. Memory model

The LLM is stateless. Each run the app reloads and passes in:
- profile settings (`rules.json` / saved profile),
- **Preferences**: list of plain sentences ("Prefer B2B SaaS over consumer"). Changed only by an approved weekly-review item or a direct user edit. Fed to the LLM as context text.
- recent statuses + thumbs + free-text notes (last N=50 events),
- the watchlist.

Raw thumbs counts keep feeding `fit_service.feedback_bias` (deterministic, +/-10 clamp). Preference sentences and notes only go to the LLM, never into scoring math.
**Shared DB rule:** `data/_company_signals.json` receives facts only (e.g. "posting URL says no sponsorship"). Opinions ("I disliked this company") stay in the user's own store.

## 4. LLM usage policy

- Used only for: kit generation, "which leads deserve a kit" decision, interview prep, follow-up drafts, weekly review, (existing) `llm_gap_fit` on top-N.
- Never run over the full ~3000-posting pool. Deterministic gate/score first; LLM sees at most ~top 10 leads per user per day.
- Per-run call budget (env `AGENT_LLM_MAX_CALLS`, default 12) and per-call timeout; on 429/timeout, fall back to deterministic output (template kit via `outreach`-style fallback) and log it.
- Phase A: owner key via existing `LLM_PROVIDER=openai` env. Phase B: per-user OpenRouter key (OAuth PKCE), pasted Gemini AI Studio key as fallback. Puter.js rejected (browser-only; cannot serve scheduled runs).
- Privacy notice shown at connect time: free-model providers may log prompts, and prompts include resume text.
- LLM output is untrusted text: never executed, rendered escaped, JSON outputs schema-validated, prompt includes scraped JD text as data only (prompt-injection risk from postings; kits are drafts so blast radius is low).

## 5. Phases

### Phase A: Agent v1, owner only
Scope: Agent tab (switch, on-duty line, accent), watchlist, daily loop, apply kits, Today brief (in-app), Activity feed, Approvals inbox (watchlist additions + preference edits created by the agent; weekly review itself is Phase C), `found_by` badge, status field on leads (Applied/Interview/Rejected set by user; no agent follow-up logic yet). Web Push is A2.
Storage: existing `store.py` SQLite + private data repo JSON. Single user = existing session user.
LLM: owner env vars.
Out of scope: multi-user sign-in, Drive/Sheets storage, follow-up drafting, weekly review.

### Phase B: multi-user foundation
Google sign-in (ID token verified server-side with `google.oauth2.id_token.verify_oauth2_token`; `google-auth` already installed), seat cap `MAX_USERS` (default 15), waitlist, owner push on waitlist join, owner-only admin view (approve users; Actions minutes used vs 3,000), per-user Google Sheet/Docs via `drive.file`, OpenRouter PKCE connect, encrypted token storage (`ENCRYPTION_KEY`), passcode login retained for owner.

### Phase C
Pipeline follow-ups, flagged-disappeared postings, interview prep pack, weekly self-review, for all users.

### Phase D: measure and cut
Instrument: kits generated vs opened/copied, Today picks the user later marked Applied, approvals accepted/rejected, push opened. Cut any capability with <20% use after 4 weeks of data (threshold is a proposal, see open questions).

## 6. User stories and acceptance criteria

IDs are for QA test mapping. "Given/When/Then" kept short.

### A. Agent tab and switch
- **A1** As a user I can tell the Agent tab from the Search tab instantly.
  AC: Agent tab uses an accent color not used on the Search tab; header reads "Agent" with a status line; Search tab contains no agent controls.
- **A2** As a user I turn the agent on/off.
  AC: `POST /agentmode/settings {enabled:false}` stops all scheduled work for that user at the next tick (no run starts after the change; an in-flight run finishes its current step then stops). Status line shows "Off". Setting persists across logins/redeploys.
- **A3** As a user I see when it last/next ran.
  AC: status line shows "On duty: last run <relative time>, next ~<relative time>" from `last_run_at`/`next_run_at`; shows an error state with reason if the last run failed.
- **A4** Nothing happens in the Search tab when leaving it.
  AC: no polling timers or background fetches originate from the Search tab after navigation (static check in frontend test).

### B. Watchlist
- **B1** Seeding. AC: first enable produces a watchlist seeded from profile tiers + thumbs-up companies + Strong/Moderate signal-DB companies with a known ATS board; each entry records `source` (profile/thumbs/signal/user/agent). Seeds with no resolvable Greenhouse/Lever/Ashby slug are listed as `unresolved` and not polled.
- **B2** Polling. AC: for each watchlist entry with a board, the agent calls `fetch_company_board`; a failing board yields `[]`, an Activity "error" entry, and never aborts the run. New postings (not in `seen`) go through `apply_gate` + `score_lead`; only `gate == pass` become Agent leads.
- **B3** Additions need approval. AC: agent-proposed watchlist additions appear in Approvals; not polled until accepted; rejected ones are remembered and not re-proposed for 90 days. User-added entries need no approval.
- **B4** Limits. AC: watchlist capped (default 40 companies); polling is concurrent-limited and total poll wall time < 2 min for 40 boards.

### C. Apply kits
- **C1** Selection. AC: per day at most 5 kits; only for leads with score >= `hot_threshold` or user-pinned; each non-selected top-10 lead has an Activity line with a reason ("skipped: stretch title").
- **C2** Content. AC: kit has `bullets` (3-6, each traceable to resume text; the LLM is told not to invent employers, dates or skills absent from the resume), `cover_note` (<= 120 words), `screening_answers` (list incl. `sponsorship_answer` verbatim from the item-4 bank, never LLM-rewritten), `contact_link`.
- **C3** No-LLM fallback. AC: with no LLM configured, kit is generated deterministically (template cover note, resume keyword overlap bullets) with `generated_by = "template"`; no network call.
- **C4** Never sends. AC: no code path in the agent calls any email/message/apply endpoint; kit endpoints are GET/POST that only store and return text. Test greps agent module for `smtplib`, `requests.post` to non-allowlisted hosts.
- **C5** Idempotence. AC: calling `POST /agentmode/kits/{lead_id}` twice returns the same kit unless `?regenerate=1`.

### D. Today brief
- **D1** AC: `GET /agentmode/brief` returns `{date, picks:[{lead_id, reason, kit_ready}], follow_ups:[...], interviews:[...], generated_at}`; picks <= 3; empty state is an explicit "nothing new today" message, not blank.
- **D2** AC: brief is generated once per day per user (idempotent on `date`), after the daily run; shown at top of the Agent tab.
- **D3** (A2) Push: AC: a subscribed device receives one notification per day with title + count; tapping opens the Agent tab; a gone subscription (HTTP 404/410) is deleted.

### E. Activity feed
- **E1** AC: every agent action writes `{ts, kind, summary, why, lead_id?}`; kinds: `poll`, `lead_found`, `kit`, `skip`, `proposal`, `error`, `follow_up`, `review`. Feed shows newest first, paged 50, retained 90 days.
- **E2** AC: entries contain no resume text and no secrets; LLM `why` strings are length-capped (300 chars) and escaped in the UI.

### F. Approvals inbox
- **F1** AC: each item has `{id, type: preference|title|source|watchlist, change, rationale, status: pending|accepted|rejected}`. `POST /agentmode/approvals/{id}` with `accept`/`reject`.
- **F2** AC: nothing in Preferences/profile/watchlist changes until `accept`; accept applies atomically and writes an Activity entry; the same decision twice is a no-op.
- **F3** AC: pending items expire after 30 days (status `expired`), no auto-apply ever.

### G. Feedback unity
- **G1** AC: thumbs/status set in the Search tab are visible in the Agent tab for the same lead and vice versa; `feedback_signal_counts` counts both identically.

### H. Pipeline (Phase C)
- **H1** AC: lead with status Applied and `status_changed_at` older than 7 days and no follow-up draft yet gets exactly one follow-up draft (draft only), Activity entry, and appears in the brief.
- **H2** AC: a lead whose URL is no longer present on a successfully fetched board for 2 consecutive polls is flagged `posting_gone`; a failed fetch never flags.
- **H3** AC: status -> Interview produces a prep pack (company summary from public JD text, likely questions, STAR prompts drawn from resume) within the next run, or immediately on demand.

### I. Weekly review (Phase C)
- **I1** AC: runs at most once per 7 days per user, only when >= 5 feedback/status events exist since last review; produces <= 5 proposals into Approvals; zero direct writes.

### J. Multi-user (Phase B)
- **J1** AC: sign-in with a Google ID token whose `aud` != configured client ID, expired, or `email_verified` false is rejected (401).
- **J2** AC: when active users == `MAX_USERS`, a new valid sign-in is placed on the waitlist (status `waitlisted`), gets no agent access, and the owner gets a push. Owner approving a waitlisted user while full returns a clear error.
- **J3** AC: admin endpoints require the owner identity; a non-owner session gets 403 on every `/admin/*` and usage view.
- **J4** AC: no endpoint ever returns a stored OAuth token or LLM key; tokens at rest are Fernet-encrypted; logs never contain them (test with a sentinel key).
- **J5** AC: a user's Sheet is read by header name; reordered columns, added user columns, and deleted rows do not break reads; writes are batched (<= 1 Sheets write call per tab per run).
- **J6** AC: user A cannot read or modify user B's Sheet-derived data or kits through any endpoint (extends existing session-isolation tests).

## 7. Data models

### Phase A (SQLite via `store.py`, mirrored to private data repo JSON)
New tables (all keyed by `user_id`):
- `agent_settings(user_id PK, enabled INT, interval_hours INT default 6, last_run_at TEXT, next_run_at TEXT, last_error TEXT)`
- `watchlist(user_id, company, ats, slug, source, status {active|unresolved|paused}, added_at, PRIMARY KEY(user_id, company))`
- `preferences(user_id, id, sentence, created_at, origin {user|approved_review})`
- `activity(user_id, id, ts, kind, summary, why, lead_id)`
- `approvals(user_id, id, type, change_json, rationale, status, created_at, decided_at)`
- `kits(user_id, lead_id, kind {apply|prep|followup}, content_json, generated_by {llm|template}, model, created_at)`
- `briefs(user_id, date, content_json, generated_at)`
- `push_subs(user_id, endpoint, keys_json, created_at)` (A2)
Changes to `leads`: add `found_by`, `status {new|applied|interview|rejected}`, `status_changed_at`, `note`, `posting_gone INT`.
Data-repo layout: `data/<user_id>/agent.json` (settings, watchlist, preferences, approvals, briefs; kits stored separately in `kits.json` to keep diffs small). `activity` kept to 90 days.

### Phase B (per-user Google Sheet, in the user's Drive, created by the app)
Tabs and columns (read by header name, case-insensitive, extra columns preserved):
- **Leads**: `Company, Title, Location, Score, Hot, Found By, Status, Thumb, Note, Link, Posted, Kit, ID(hidden)`. Matching on `ID`; user may sort/filter/delete rows (deleted row = "dismissed", not re-added while its ID is in the owner-side seen list).
- **Preferences**: `Preference, Added, Origin`
- **Watchlist**: `Company, Source, Status`
- **Activity**: `Time, Kind, What, Why`
Kits and prep packs: Google Docs in a "JobFinder" Drive folder; Leads.Kit holds the Doc link.
Owner side keeps only: seen/dedupe list per user, shared H1B DB, profile settings, encrypted OAuth/LLM tokens, usage counters (LLM calls, Actions-triggered runs), `users` (status active|waitlisted|disabled, `is_owner`), waitlist. Where these live (data repo JSON) is open question 6.

## 8. API (Phase A proposal)

All under session auth (`Depends(current_user)`); `user_id` never from the client.

| Method + path | Purpose |
|---|---|
| `GET /agentmode/settings` / `POST /agentmode/settings` | enabled, interval_hours (6/12/24), status line fields, preferences list edit (direct user edit), watchlist add/remove |
| `GET /agentmode/brief` | today's brief (D1) |
| `GET /agentmode/activity?limit&before` | feed (E1) |
| `GET /agentmode/approvals` / `POST /agentmode/approvals/{id}` body `{decision}` | inbox (F) |
| `GET /agentmode/kits/{lead_id}` / `POST /agentmode/kits/{lead_id}` (`?regenerate=1`) | read / create kit (C) |
| `POST /agentmode/run-now` | one agent tick for this user; rate-limited (see 10); returns a job id polled via existing `/agent/jobs/{id}` pattern or `/agentmode/run-now/{id}` |
| `POST /agentmode/status` body `{lead_id, status, note}` | set pipeline status (shared with Search tab; may reuse `/feedback` shape) |
| `POST /agentmode/push/subscribe` (A2) | store Web Push subscription |

Phase B adds: `POST /auth/google`, `GET/POST /admin/waitlist`, `GET /admin/usage`, `GET /connect/openrouter/start|callback`, `POST /connect/llm-key`.
Naming reconciliation: `/agent/*` (existing) = Full Search, untouched; `/agentmode/*` = Agent tab. Add a code comment at both route groups so future sessions do not confuse them.

## 9. Execution model

Render's free web service sleeps and has no disk, so the scheduled loop runs in **GitHub Actions in the private data repo**, as in the existing design:
- Workflow `daily-scrape` (weekday cron): ONE shared scrape, saves the pool (e.g. `data/_pool/<date>.json`, descriptions dropped for terminal gates as today). Per-user steps then score against the pool, run LLM steps for that user's top leads, write results.
- Workflow `agent-tick` (cron every ~4-6h): for each enabled user whose interval elapsed, poll watchlist boards (seconds of compute), score, kits for new leads, update brief. Billed minimum is per job, so run all users inside ONE job (loop), not a matrix.
- Decrypting per-user LLM keys in Actions needs `ENCRYPTION_KEY` as a data-repo Actions secret (Phase B). Phase A uses the owner key as an Actions secret too (LLM env vars must exist on **both** Render and the data repo).
- Manual Run Full Search: re-scores today's pool on Render, instant, no Actions minutes. "Fresh scrape now": `workflow_dispatch`, limit 1 per user per day.

Minutes estimate (to be validated in Phase A): shared scrape ~15 min x 22 weekdays = ~330 min. Agent tick, 1 min x 4 per day x 30 = ~120 min. Total ~450/month, ~15% of 3,000. Each "Fresh scrape now" costs ~15 min: 15 users x 1/day x 22 days would be ~4,950 min and **exceeds the budget on its own**, so the 1/user/day cap is not safe at full seats (see open question 3).

## 10. Failure modes and handling

| Failure | Handling |
|---|---|
| LLM 429 / timeout / invalid JSON | retry once, then template fallback, Activity `error` line; never blocks the brief |
| LLM hallucinated resume facts in kit | prompt constraint + kit footer "review before use"; QA check that bullets only reuse resume tokens (heuristic) |
| ATS board slug changed / 404 | entry flagged `unresolved` after 3 consecutive failures, shown in Activity |
| Posting "disappeared" false positive (fetch partial) | require 2 consecutive successful polls missing the URL (H2) |
| GitHub scheduled run delayed or skipped | `next_run_at` is advisory; tick catches up on next run; status line shows real `last_run_at`. GitHub may also auto-disable scheduled workflows in repos with no activity for 60 days (documented for public repos; the data repo gets regular commits so unlikely, but unverified for private repos) |
| Actions minutes exhausted | admin view shows usage; scrape refuses new Fresh-scrape requests above 80% of budget; agent tick keeps priority |
| Concurrent runs for one user (Run now + tick) | per-user lock row; second returns 409; commits to data repo retried on conflict |
| Data repo commit races (several users writing) | single writer job per workflow; retry with rebase; path per user |
| `workflow_dispatch` input size limit (existing inputs carry rules_json + resume_text; GitHub caps total inputs at 65,535 chars and max 10 inputs) | for the agent, do NOT pass resume/profile as inputs; the job reads them from the data repo. Flag for dev: existing run-agent flow may already be near this with a long resume |
| Web Push subscription dead (404/410) | delete it; brief still shows in-app. iOS Safari only supports push for sites installed to the Home Screen (unverified in this doc) |
| Google refresh token revoked / consent screen left in Testing (7-day expiry) | detect `invalid_grant`, mark user "reconnect Google", pause that user's agent, show banner |
| User edits the Sheet badly (renamed header, deleted tab) | read by header; recreate missing tab/headers; never overwrite user-added columns; on unparseable state, pause and surface in Activity |
| Seat race at the cap | seat claim is a single atomic write (DB transaction); loser is waitlisted |
| Prompt injection via JD text | JD passed as quoted data; output only stored as draft; no tool access in kit calls |
| Pool poisoning / stale pool | pool carries `scraped_at`; Search tab shows "pool from <time>"; refuse pool older than 3 days for scoring without a fresh scrape |
| Sponsorship answer wrong for the user's mode | always from `sponsorship_answer()`, never LLM-authored (C2) |
| Preferences grow unbounded / contradict | cap 20 sentences; review proposals must reference which sentence they replace |

## 11. Security and privacy notes

- Resume text and Sheet content are personal data: never logged, never in the public repo, never in the shared signal DB.
- Tokens (Google refresh token, OpenRouter key, pasted Gemini key) encrypted with Fernet (`ENCRYPTION_KEY`); key rotation procedure needed (open question 8).
- OpenRouter/Gemini keys are the user's; usage counters are informational.
- `drive.file` limits the app to files it created or the user explicitly opened; app must not ask for broader Drive scopes.
- Owner passcode login is a backup path and must remain rate-limited.

## 12. Owner decisions (2026-11-19: owner agreed to all eight recommended answers)
1. Fresh scrape capped at **1 per user per week** (the shared daily scrape covers the rest).
2. The owner's LLM key goes in **both** Render env and the data repo's Actions secrets (Phase A).
3. **Owner approves every user**, below the 15-seat cap too.
4. Phase B admin = the Google account set in **`OWNER_EMAIL`**; the passcode owner maps to the same user_id.
5. Owner-side Phase B records live in the **private data repo** (tokens encrypted with `ENCRYPTION_KEY`; accepted risk).
6. Phase D: **cut any capability used by <20% of active users after 4 weeks**.
7. iPhone users: **in-app brief only**, plus a tip to add the site to the Home Screen for push.
8. The first agent-seeded watchlist is **one batch approval** (built: "Accept all" in Approvals, `POST /agentmode/approvals/accept-all`).
Also settled: the data repo, not Render, is the source of truth for the Agent tab (item 11 below), and the agent only
picks jobs in the places the user picks ("Where and what to look for" card, saved per user, used by scheduled runs too).

## 13. Open questions (remaining)

1. **Fresh-scrape budget.** 1/user/day x 15 users can exceed 3,000 min. Lower to 1/user/week, or a global daily cap (e.g. 3 per day across users)?
2. **Where does the Agent loop LLM key live in Phase A?** Needs to be both a Render env var and a data-repo Actions secret. OK to put the owner's Groq key in both?
3. **Is "5 kits/day" enough free-tier headroom?** Groq free-tier limits vary by source (see OWNER_SETUP); a kit run is ~5-8 calls/user/day. At 15 users on one shared Groq key this would hit TPM limits; Phase B is per-user keys, so only Phase A is affected.
4. **Seats: approve-by-owner or first-come?** Spec says waitlist when full and owner approves; confirm that approval is also required below the cap.
5. **Owner identity in Phase B.** Which Google account is the admin (set via `OWNER_EMAIL` env), and does the passcode owner map to the same user_id?
6. **Where do owner-side Phase B records (users, waitlist, encrypted tokens, usage) live?** Data repo JSON is simplest but means encrypted tokens sit in a git history (private repo, encrypted, but permanent). Alternative: a small free DB, which breaks the no-new-signups rule. Recommend data repo plus ENCRYPTION_KEY, accepted risk.
7. **Phase D cut threshold.** Proposed: cut anything used by <20% of active users after 4 weeks. Confirm.
8. **ENCRYPTION_KEY rotation/loss.** Losing it forces every user to reconnect. Acceptable? Back it up in a password manager.
9. **Seeding from H1B signal DB** may produce a long, generic watchlist for a user with narrow tastes. Cap at 40 and require approval for agent-seeded entries (spec says approval; confirm seeding at first enable should be one batch approval rather than 40 items).
10. **Web Push on iOS** requires installing the site as a PWA; is an in-app-only brief acceptable for those users?
11. **Kit storage in Phase A:** SQLite is wiped on Render redeploy; kits generated in Actions go to the data repo JSON. Confirm the data repo (not Render) is the source of truth for the Agent tab in Phase A (so DATA_REPO_TOKEN becomes a Phase A requirement rather than optional).
12. **Legal/ToS:** letting a third-party app write to users' Drive and call an LLM with resumes warrants a short privacy page, which Google requires for a Production consent screen (see OWNER_SETUP).
