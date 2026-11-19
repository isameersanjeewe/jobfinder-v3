---
name: jobfinder-release
description: Commit, push, deploy and check JobFinder in production (Render backend, Vercel frontend, private data repo on GitHub Actions). Covers the pre-commit gate (verify, code review, security review), splitting commits, the owner's manual env steps, and live checks (auth on, the right profile loaded, the Agent tab working). Use this whenever the owner says commit, push, ship, deploy, release, "is it live" or "check production", or after merging an agent branch they want to use.
---

# JobFinder release

Production serves a real job search. Render's free tier wipes its disk, and the repo is public. A careless push can expose an open API, leak personal data, or erase state. Go in order and stop at each owner checkpoint.

## 1. Gate (before any commit)
1. Run **jobfinder-verify** (full). It must end `VERIFY: OK`.
2. Run **`/code-review`** on the uncommitted diff, usually at `high` for features (use max/ultra only if the owner asks). Fix confirmed findings, then verify again.
3. Run **`/security-review`** when auth, endpoints, workflows, secrets handling or LLM code changed. Fix, then verify again.
4. Make sure `git status` shows no personal files and that `.claude/worktrees/` is ignored.

## 2. Commit (only on the owner's explicit OK)
- Branch: work on `agentic-v4` (not the default branch). Split commits by concern, e.g. "security fixes + tests" and then "feature + docs".
- Stage files by name, not `git add -A`. Look at `git diff --cached --stat` before committing.
- End every message with the attribution trailer from the system reminder.
- Approval for one commit isn't approval to push.

## 3. Owner's manual steps (never ask for secret values in chat)
Point to `docs/OWNER_SETUP.md` and list only the steps this release needs:
- **Render env:** `API_TOKEN`, `SESSION_SECRET`, `ADMIN_TOKEN`, three different random values from `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Do this before the first push of any auth change. LLM vars come later (`LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
- **Private data repo:** copy the changed `data-repo-template/` files (workflows, scripts). Set Actions secrets (LLM vars, `RULES_JSON` if used) and the repo variable `APP_REF` = the reviewed commit SHA, so scheduled jobs don't run whatever is newest on `main`.

## 4. Push (only on the owner's explicit OK)
**Render deploys from `main`** (the service header shows the branch and live commit). Pushing `agentic-v4` alone deploys nothing. To ship: push `agentic-v4`, then fast-forward `main` (`git push origin agentic-v4:main`), which needs the owner's explicit OK to deploy, because it's the default branch. Render redeploys automatically and the first request after deploy can take ~1 minute (cold start).

## 5. Live checks
1. `curl -s <backend>/` must show `"auth": true`. If it's false, stop and tell the owner the API is open.
2. With the claude-in-chrome skill (start with `tabs_context_mcp`, use a new tab, no dialogs), open the frontend, log in with the owner's own session, and check:
   - Search tab loads leads
   - Agent tab loads state, and Run now completes
   - the browser console shows no errors
3. **Confirm the right profile:** a run's titles and locations match the owner's private profile (`RULES_JSON`), not `rules.example.json`. Compare by eye in the browser; never paste the values into chat or files.
4. Report what passed, what didn't, and what's left for the owner.

## Lessons
Add one dated line after each release.
- 2026-11-18: Production was found with `"auth": false` and a public backend URL, so admin was open to anyone. Always run check 5.1 after a deploy, and before telling the owner something is safe.
- 2026-11-18: Render deploys from `main`, not `agentic-v4`. The first push "succeeded" but production kept running the old commit. Check the deploy branch on the Render service page before claiming something is live.
- 2026-11-18: The owner believed A-0 was done, but Render had only `DATA_REPO`, `DATA_REPO_TOKEN` and `SESSION_SECRET`. Check env key names (never values) in the Render Environment tab instead of relying on memory.
- 2026-11-18: The browser reported 503 while Render logged 401. Auth rejections lacked CORS headers (fixed in 8998d6a). Read Render's app logs before trusting the browser's status code. Note that once API_TOKEN is set, every route returns 401 without a token, so to detect a deploy, check for a header only the new code sends, not the status code.
- 2026-11-18: A live check that worked: compare the UI's Search-filter titles and locations with backend/rules.json locally, reporting only match true/false, never the values.
- 2026-11-19: Pushed before the background `/code-review` finished. It then found 4 real bugs (trusted ATS remote roles dropped, "Austin TX" leak, "no places" rejecting everything, saved leads swept for users without Agent targets), which needed a second deploy. Wait for the review result before `git push ... :main`.
- 2026-11-19: Each Render deploy wipes SQLite. A write endpoint hit before `/agentmode/state` saw empty state and could sync it over the data repo. Agent write endpoints now call `_agentmode_load()` first. Any new per-user write endpoint must do the same.
- 2026-11-19: Requiring ADMIN_TOKEN on `/admin/*` silently broke `frontend/admin.html`, which still sent only API_TOKEN, so the owner couldn't add users. When an endpoint's auth changes, grep every client (`frontend/*.html`, CLI scripts, workflows) for that path and update them in the same commit.
