# ── RUN AGENT CYCLES (runs INSIDE the private jobfinder-data repo, via scheduled-agent.yml) ──────────
# Sibling of run_for_user.py (whose contract is unchanged). Runs one Agent v1 cycle (backend/agent_mode.py
# in the public app repo) for EVERY user whose agent mode is enabled and whose interval has elapsed, in one
# job, then writes each user's state back as JSON for the workflow to commit:
#   data/<user_id>/agent.json   -- agent state (settings, statuses, watchlist, approvals, activity, kits, thumbs)
#   data/<user_id>/leads.json   -- updated with watchlist leads the cycle saved
#
#   python scripts/run_agent_cycle.py --check   stdlib only, no app code: prints due=true|false (also to
#                                               $GITHUB_OUTPUT) so the workflow can skip installing deps
#   python scripts/run_agent_cycle.py           run every due user (needs APP_BACKEND_DIR + deps installed)
#
# Env: APP_BACKEND_DIR (default app-src/backend); optional LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL
# (repo secrets, never printed), RESUME_TEXT and RULES_JSON fallbacks for a user with no saved snapshot.

import glob
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _due_users() -> list[str]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for path in sorted(glob.glob(os.path.join("data", "*", "agent.json"))):
        try:
            rows = json.load(open(path)).get("agent_settings") or []
        except (OSError, ValueError):
            continue
        s = rows[0] if rows else {}
        if not s.get("enabled"):
            continue
        try:
            due = not s.get("next_run") or datetime.strptime(s["next_run"], ISO) <= now
        except ValueError:
            due = True
        if due:
            out.append(os.path.basename(os.path.dirname(path)))
    return out


def main() -> int:
    users = _due_users()
    if "--check" in sys.argv:
        line = f"due={'true' if users else 'false'}"
        print(line, f"({len(users)} user(s))")
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(line + "\n")
        return 0

    sys.path.insert(0, os.path.abspath(os.environ.get("APP_BACKEND_DIR", "app-src/backend")))
    import store  # noqa: E402  (from the checked-out public app repo)
    store.DB_PATH = tempfile.mktemp(suffix=".db")  # throwaway, seeded from the committed JSON below
    import rules_service  # noqa: E402
    import agent_mode  # noqa: E402

    signals_path = os.path.join("data", "_company_signals.json")
    if os.path.exists(signals_path):  # shared H1B company DB (roadmap item 6) -> local cache rules_service reads
        local = tempfile.mktemp(suffix=".json")
        json.dump({"company_signals": json.load(open(signals_path))}, open(local, "w"))
        rules_service.RULES_LOCAL_PATH = local

    fallback_rules = os.environ.get("RULES_JSON", "")
    summary = []
    for uid in users:
        d = os.path.join("data", uid)
        leads_path, agent_path = os.path.join(d, "leads.json"), os.path.join(d, "agent.json")
        leads = json.load(open(leads_path)) if os.path.exists(leads_path) else []
        with store._conn() as c:
            c.executemany("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?)",
                          [(uid, j["url"], j.get("track", "ft"), json.dumps(j), j.get("first_seen", ""),
                            j.get("status", "Not Applied")) for j in leads if j.get("url")])
        store.import_agent_state(uid, json.load(open(agent_path)))
        snap = store.get_agent_settings(uid).get("rules_snapshot") or fallback_rules
        if not snap:
            summary.append({"user_id": uid, "ok": False, "error": "no rules snapshot (enable agent mode in the app once)"})
            continue
        os.environ["RULES_JSON"] = snap
        try:
            res = agent_mode.run_cycle(uid, rules=rules_service.load_rules(user_id=uid))
            summary.append({"user_id": uid, "ok": True, "kits": len(res["picks"]), "llm_calls": res["llm_calls"],
                            "llm_stopped": res["llm_stopped"]})
        except Exception as e:  # one user's failure never blocks the others; partial kits are already saved
            summary.append({"user_id": uid, "ok": False, "error": e.__class__.__name__})
        with open(agent_path, "w") as f:
            json.dump(store.export_agent_state(uid), f, indent=1)
        with open(leads_path, "w") as f:
            json.dump(store.list_leads(uid), f, indent=2)
    print(json.dumps(summary, indent=1))
    try:
        os.remove(store.DB_PATH)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
