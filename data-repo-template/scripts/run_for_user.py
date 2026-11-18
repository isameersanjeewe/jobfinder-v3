# ── RUN FOR USER (runs INSIDE the private jobfinder-data repo, via GitHub Actions) ───────────────────
# Scrapes for one user and commits the result back into this same repo as JSON (the durable store that
# survives Render redeploys -- see docs/MULTI_USER_SETUP.md in the app repo and CLAUDE.md's "Multi-user"
# section there for why). This script makes NO changes to the app repo's backend code: it reuses
# agent_service.AgentRun and store.py completely unmodified, just pointed at a throwaway sqlite file so
# their existing dedupe ("only new leads") logic works as-is, then exports the result to JSON here.
#
# Called by .github/workflows/run-agent.yml with these env vars (from workflow_dispatch inputs):
#   USER_ID          required, same user_id the owner created with backend/add_user.py
#   RULES_JSON       required, the full resolved profile (Render already applied any in-app filter
#                    overlay before dispatching -- this script does not call rules_service at all)
#   RESUME_TEXT      optional, defaults to ""
#   MIN_FIT          optional, defaults to "40"
#   HOURS            optional recency-window override, "" = use RULES_JSON's own recency_hours
#   APP_BACKEND_DIR  path to the checked-out PUBLIC app repo's backend/ dir (default "app-src/backend")
#
# Writes/updates, relative to the repo root this script runs from:
#   data/<user_id>/leads.json   -- full lead list (array of lead dicts), the source of truth Render reads
#   data/<user_id>/seen.json    -- array of every URL ever seen for this user (dedupe memory)

import json
import os
import sys
import tempfile

USER_ID = os.environ["USER_ID"]
RULES = json.loads(os.environ["RULES_JSON"])
RESUME_TEXT = os.environ.get("RESUME_TEXT", "")
MIN_FIT = int(os.environ.get("MIN_FIT", "40") or "40")
HOURS = os.environ.get("HOURS", "").strip()
APP_BACKEND_DIR = os.environ.get("APP_BACKEND_DIR", "app-src/backend")

sys.path.insert(0, os.path.abspath(APP_BACKEND_DIR))
import store  # noqa: E402  (from the checked-out public app repo, not this repo)
from agent_service import AgentRun  # noqa: E402

DATA_DIR = os.path.join("data", USER_ID)
os.makedirs(DATA_DIR, exist_ok=True)
leads_path = os.path.join(DATA_DIR, "leads.json")
seen_path = os.path.join(DATA_DIR, "seen.json")

existing_leads = json.load(open(leads_path)) if os.path.exists(leads_path) else []
existing_seen = json.load(open(seen_path)) if os.path.exists(seen_path) else []

# Seed a throwaway sqlite DB (never committed) so AgentRun's existing save_leads()/mark_seen() dedupe
# logic runs completely unchanged. This runner is a fresh GitHub Actions VM each time, so there is no
# leftover state to worry about between runs other than what we just loaded from the committed JSON.
tmp_db = tempfile.mktemp(suffix=".db")
store.DB_PATH = tmp_db
with store._conn() as c:
    c.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(USER_ID, u) for u in existing_seen if u])
    c.executemany(
        "INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?)",
        [(USER_ID, j["url"], j.get("track", "ft"), json.dumps(j), j.get("first_seen", ""), j.get("status", "Not Applied"))
         for j in existing_leads if j.get("url")])

if HOURS:
    RULES.setdefault("profile", {})["recency_hours"] = int(HOURS)

run = AgentRun(RESUME_TEXT, USER_ID, rules=RULES, min_fit=MIN_FIT)
result = run.run()

all_leads = store.list_leads(USER_ID)
with store._conn() as c:
    all_seen = sorted(r[0] for r in c.execute("SELECT url FROM seen WHERE user_id=?", (USER_ID,)))

with open(leads_path, "w") as f:
    json.dump(all_leads, f, indent=2)
with open(seen_path, "w") as f:
    json.dump(all_seen, f, indent=2)
try:
    os.remove(tmp_db)
except OSError:
    pass

print(json.dumps({
    "summary": result.get("summary"),
    "new_count": result.get("new_count"),
    "stats": result.get("stats"),
    "total_leads": len(all_leads),
}, indent=1))
