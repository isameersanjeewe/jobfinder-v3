# Multi-user setup (optional) — ~10 minutes

**This whole section is optional.** Until you do this, the app keeps working exactly as it does
today: agent runs happen in-process on Render, and leads live in Render's local SQLite file —
which gets wiped every time Render redeploys. Doing this setup makes leads durable across
redeploys, and moves the actual scraping work off Render's free web service onto free GitHub
Actions minutes instead.

## 1. Create the private data repo

1. Go to **https://github.com/new**.
2. Owner: `isameersanjeewe`. Repository name: `jobfinder-data` (or similar).
3. Visibility: **Private**.
4. Leave "Add a README" unchecked (we're pushing files in step 2).
5. Click **Create repository**. Make sure its default branch is `main`.

## 2. Copy the template into it

1. From this repo, copy the entire contents of the `data-repo-template/` folder — that's
   `.github/workflows/run-agent.yml`, `scripts/run_for_user.py`, and the empty `data/` folder —
   into the root of the new `jobfinder-data` repo.
2. Commit and push those files to `jobfinder-data`'s `main` branch (via `git push`, or by
   dragging the files into the GitHub web UI's "Add file" -> "Upload files" screen).
3. Nothing else needs to be added to this new repo. It does **not** need any secrets of its own —
   the workflow only uses the token GitHub automatically provides to every workflow run.

## 3. Generate a fine-grained GitHub token

1. Go to **https://github.com/settings/personal-access-tokens/new**.
2. Token name: anything, e.g. `jobfinder-data-render`.
3. Expiration: **1 year** (set a calendar reminder to come back and rotate it — free
   fine-grained tokens expire and this one will stop working silently).
4. Repository access: **Only select repositories** -> select `jobfinder-data`. Do not give it
   access to any other repo.
5. Under **Repository permissions**, set:
   - **Contents: Read and write**
   - **Actions: Read and write**
   Leave everything else as "No access".
6. Click **Generate token**, then **copy the token value immediately** — GitHub shows it only
   once.

## 4. Set the Render environment variables

Go to the Render dashboard -> your `jobfinder-backend` service -> **Environment** tab -> use
**Add Environment Variable** for each of these:

- `DATA_REPO_TOKEN` — paste the token you copied in step 3.
- `DATA_REPO` — `isameersanjeewe/jobfinder-data` (or whatever you named the repo in step 1).
- `SESSION_SECRET` — any long random string. Generate one by running this locally:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- *(Optional — defaults are already correct, only set these if you renamed things)*
  `DATA_REPO_WORKFLOW=run-agent.yml`, `DATA_REPO_BRANCH=main`.

After saving, either click **Manual Deploy** -> **Deploy latest commit** to redeploy right away,
or just wait — Render may pick up the new environment variables automatically on its next deploy,
depending on your service's auto-deploy settings.

That's it — the app is now set up to run scrapes via GitHub Actions and store results durably in
the private `jobfinder-data` repo.

---

## Adding a new tester

This part is **not** a one-time GitHub setup step — it's a backend command you run any time you
want to add or reset a tester, and it works whether or not you've done the data-repo setup above.

```bash
cd backend && python add_user.py <user_id> <passcode>
```

Then give that tester three things: the app's URL, their `<user_id>`, and their `<passcode>`.
They type both the user ID and passcode into the app's login screen.
