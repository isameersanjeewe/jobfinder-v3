# ── DATA REPO CLIENT ──────────────────────────────────────────────────────────
# Render-side client for the private "jobfinder-data" GitHub repo that holds durable per-user data
# (leads/seen JSON) so it survives Render redeploys (Render's free plan has no persistent disk), and
# that triggers + polls the agent run which now happens there via GitHub Actions instead of in-process.
# See docs/MULTI_USER_SETUP.md for the one-time owner setup and data-repo-template/ for what goes in
# that repo. CLAUDE.md "Multi-user" section has the why.
#
# Fully optional and inert until configured: every function below is a no-op / returns None unless
# DATA_REPO_TOKEN + DATA_REPO are set. Without them, main.py keeps running the agent in-process and
# reading local SQLite exactly as it does today -- so local dev and the test suite need no GitHub
# credentials, and flipping this on later (once the owner finishes the manual repo-creation step) is
# just two Render env vars, no code change.

import base64
import calendar
import json
import logging
import os
import time

import requests

log = logging.getLogger("jobfinder")

DATA_REPO_TOKEN = os.getenv("DATA_REPO_TOKEN", "")   # fine-grained PAT: contents:write + actions:write on DATA_REPO only
DATA_REPO = os.getenv("DATA_REPO", "")               # "owner/jobfinder-data"
DATA_REPO_WORKFLOW = os.getenv("DATA_REPO_WORKFLOW", "run-agent.yml")
DATA_REPO_BRANCH = os.getenv("DATA_REPO_BRANCH", "main")
API = "https://api.github.com"
_TIMEOUT = 15


def enabled() -> bool:
    return bool(DATA_REPO_TOKEN and DATA_REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {DATA_REPO_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_user_json(user_id: str, filename: str):
    """Reads data/<user_id>/<filename> from the private repo via the Contents API.
    Returns None if disabled, missing, or on any error (callers treat None as 'nothing yet')."""
    if not enabled():
        return None
    url = f"{API}/repos/{DATA_REPO}/contents/data/{user_id}/{filename}"
    try:
        r = requests.get(url, headers=_headers(), params={"ref": DATA_REPO_BRANCH}, timeout=_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(content)
    except Exception:
        return None


def fetch_user_leads(user_id: str) -> list[dict]:
    data = fetch_user_json(user_id, "leads.json")
    return data if isinstance(data, list) else []


def read_json(path: str):
    """Reads any JSON file at `path` (repo-root-relative) from the private repo. None if disabled/missing/error."""
    if not enabled():
        return None
    url = f"{API}/repos/{DATA_REPO}/contents/{path}"
    try:
        r = requests.get(url, headers=_headers(), params={"ref": DATA_REPO_BRANCH}, timeout=_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    except Exception:
        return None


def write_json(path: str, data, message: str) -> bool:
    """Creates or updates a JSON file at `path` (repo-root-relative) via the Contents API (get-sha-then-PUT,
    so it works whether the file already exists or not). Used for the small shared files (e.g. the users
    table) that aren't written by the GitHub Actions workflow itself. Returns True on success."""
    if not enabled():
        return False
    url = f"{API}/repos/{DATA_REPO}/contents/{path}"
    try:
        sha = None
        r = requests.get(url, headers=_headers(), params={"ref": DATA_REPO_BRANCH}, timeout=_TIMEOUT)
        if r.status_code == 200:
            sha = r.json().get("sha")
        body = {
            "message": message,
            "content": base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii"),
            "branch": DATA_REPO_BRANCH,
        }
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=_headers(), json=body, timeout=_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("data_repo.write_json(%s) failed: %s", path, e)
        return False


def trigger_run(user_id: str, rules: dict, resume_text: str = "", min_fit: int = 40, hours: int | None = None) -> dict:
    """Fires workflow_dispatch on the private repo's run-agent workflow, passing the already-resolved
    per-user rules (profile + any per-run filter overlay) as an input. Returns {"ok": True, "dispatched_at": ts}
    or {"ok": False, "error": ...}. GitHub's dispatch API gives no run id back; find_run_after() locates it."""
    if not enabled():
        return {"ok": False, "error": "DATA_REPO_TOKEN/DATA_REPO not configured"}
    url = f"{API}/repos/{DATA_REPO}/actions/workflows/{DATA_REPO_WORKFLOW}/dispatches"
    inputs = {
        "user_id": user_id,
        "rules_json": json.dumps(rules),
        "resume_text": (resume_text or "")[:20000],
        "min_fit": str(min_fit),
        "hours": str(hours or ""),
    }
    try:
        dispatched_at = time.time()
        r = requests.post(url, headers=_headers(), json={"ref": DATA_REPO_BRANCH, "inputs": inputs}, timeout=_TIMEOUT)
        if r.status_code == 204:
            return {"ok": True, "dispatched_at": dispatched_at}
        return {"ok": False, "error": f"dispatch failed: {r.status_code} {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def find_run_after(dispatched_at: float, timeout_s: int = 10):
    """Best-effort lookup of the workflow run that was just dispatched (matches by workflow + branch +
    created-at-or-after the dispatch time). Polls briefly since a run can take a few seconds to appear.
    Returns the run dict (has "id", "status", "conclusion") or None if not found within timeout_s."""
    if not enabled():
        return None
    url = f"{API}/repos/{DATA_REPO}/actions/workflows/{DATA_REPO_WORKFLOW}/runs"
    deadline = time.time() + timeout_s
    while True:
        try:
            r = requests.get(url, headers=_headers(),
                              params={"branch": DATA_REPO_BRANCH, "event": "workflow_dispatch"}, timeout=_TIMEOUT)
            r.raise_for_status()
            for run in r.json().get("workflow_runs", []):
                # created_at is UTC ("...Z"); timegm (not mktime) treats the parsed struct as UTC
                # instead of the host's local timezone, which mktime would get wrong on a non-UTC host.
                created = calendar.timegm(time.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
                if created >= dispatched_at - 5:
                    return run
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(2)


def run_status(run_id) -> dict | None:
    """{"status": "queued"|"in_progress"|"completed", "conclusion": "success"|"failure"|None} or None on error."""
    if not enabled():
        return None
    url = f"{API}/repos/{DATA_REPO}/actions/runs/{run_id}"
    try:
        r = requests.get(url, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        return {"status": j["status"], "conclusion": j.get("conclusion")}
    except Exception:
        return None
