# data-repo-template

This folder is **not meant to run from here.** It's a template: copy its entire contents (the
`.github/workflows/run-agent.yml` workflow, `scripts/run_for_user.py`, and the empty `data/`
folder) into the root of a **new, private** GitHub repo (suggested name: `jobfinder-data`). For
the exact step-by-step, see `docs/MULTI_USER_SETUP.md` in this (the app) repo.

## What it's for

JobFinder's backend runs on Render's free tier, which has no persistent disk — every redeploy
wipes the local SQLite database. To keep each tester's leads durable, the actual scrape runs in
GitHub Actions instead, inside this private repo, and commits its results back into itself as
plain JSON (`data/<user_id>/leads.json` + `data/<user_id>/seen.json`). Render triggers a run via
the GitHub API (`workflow_dispatch`), polls until it finishes, then reads the resulting JSON back
out over the GitHub API too.

## Why it's split this way

The app's actual code (`backend/`) stays in the public `jobfinder-v3` app repo, since the repo is
meant to be public and open source. This private repo holds only the durable *data* — each
tester's leads and seen-URL history — which is personal and shouldn't be public. The workflow
checks out both: the public app repo (for code, read-only) and itself (to write data back into).
It commits using the `GITHUB_TOKEN` GitHub automatically provides to every Actions run — not a
manually created secret — so this repo needs zero secrets of its own; the only configuration lives
on the Render side (see `docs/MULTI_USER_SETUP.md`).
