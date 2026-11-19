# Location picks + Agent targets (2026-11-19): strict geography, Agent's own saved filters, saved-lead re-check,
# accept-all approvals. Offline, fictional companies and the example profile only. Run: python tests/test_agent_targets.py
import sys, os, json, datetime, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
TMP = tempfile.mkdtemp(prefix="jf_targets_")
os.environ["DB_PATH"] = os.path.join(TMP, "leads.db")
os.environ["RULES_LOCAL_PATH"] = os.path.join(TMP, "rules.local.json")
os.environ.pop("RULES_JSON", None)
for k in ("LLM_PROVIDER", "LLM_API_KEY", "DATA_REPO_TOKEN", "DATA_REPO", "API_TOKEN", "ADMIN_TOKEN"):
    os.environ.pop(k, None)
EX = json.load(open(os.path.join(HERE, "..", "backend", "rules.example.json")))
EX["companies"].update(strong=["acme health", "globex", "initech"], moderate=[], risk=[], excluded=[])
json.dump(EX, open(os.path.join(TMP, "rules.json"), "w"))
os.environ["RULES_PATH"] = os.path.join(TMP, "rules.json")

import agent_mode, rules_service, store
from scraper_service import normalize

today = datetime.date.today().isoformat()
NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def rules_with(locations, remote_ok=True):
    return rules_service.apply_filters(rules_service.load_rules(), {"locations": locations, "remote_ok": remote_ok})


# ── 1. geography: only the places the user picked (+ truly remote) ──
R = rules_with(["Denver, CO", "Ohio"])
cases = [  # (location, board remote flag, expected)
    ("TX, USA", True, False),              # the reported leak: aggregator flags it remote, but it names another state
    ("TX, USA", False, False),
    ("Austin, TX", True, False),
    ("San Francisco, CA", True, False),
    ("Remote - Austin, TX", True, True),   # the posting itself says remote
    ("Remote, US", True, True),
    ("Remote", False, True),
    ("United States", False, True),        # country only = unspecified, still allowed
    ("CO, USA", False, True),              # abbreviation without a leading comma is still the user's state
    ("Denver, CO 80202", False, True),
    ("Lakewood, Colorado", False, True),
    ("Columbus, OH", False, True),
    ("Newark, DE", False, False),
    ("", False, True),                     # unknown: let ranking decide
]
for loc, rem, want in cases:
    assert rules_service.geo_ok(loc, R, rem) is want, (loc, rem, want)
assert rules_service.named_states("Remote IN TEAM") == set(), "upper-case words are not state codes unless between separators"
assert rules_service.named_states("TX, USA") == {"tx"}
NR = rules_with(["Denver, CO"], remote_ok=False)
assert not rules_service.geo_ok("Remote", NR, True) and not rules_service.geo_ok("Remote, US", NR, False), "remote off = no remote"
print("GEO: picked places only; remote flag can't smuggle in another state OK")


# ── 2. Agent uses its own saved targets, and re-checks saved Full Search leads against them ──
def J(t, c, l, remote=False):
    j = normalize(t, c, l, f"https://x/{c}/{t}/{l}".replace(" ", "_"), today, "mock", "product roadmap sql agile")
    j["is_remote"] = remote
    return j

saved = [J("Product Manager", "Acme Health", "Austin, TX"),          # passed under the old (example) profile
         J("Senior Product Manager", "Globex", "TX, USA", remote=True),
         J("Product Manager", "Initech", "Denver, CO"),
         J("Product Owner", "Globex", "Remote", remote=True)]
store.save_leads(saved, "ft", "owner")
store.save_agent_settings("owner", filters=json.dumps({"locations": ["Denver, CO"], "remote_ok": True}))
res = agent_mode.run_cycle("owner", NOW)
picked = set(res["picks"])
assert picked and not any("Austin" in u or "TX,_USA" in u for u in picked), picked
assert any("Denver" in u for u in picked)
assert any("Set aside 2 saved lead(s)" in e["message"] for e in res["events"]), [e["message"] for e in res["events"]]
# kits made under old targets are cleared next run, unless the user already acted on them
store.save_kit("owner", "https://x/old-austin", "c0", {"url": "https://x/old-austin", "title": "Product Manager", "location": "Austin, TX"})
store.save_kit("owner", "https://x/old-applied", "c0", {"url": "https://x/old-applied", "title": "Product Manager", "location": "Austin, TX"})
store.set_lead_status("owner", "https://x/old-applied", "Applied")
res2 = agent_mode.run_cycle("owner", NOW)
urls = store.kit_urls("owner")
assert "https://x/old-austin" not in urls and "https://x/old-applied" in urls, urls
assert any("Cleared 1 apply kit" in e["message"] for e in res2["events"])
assert agent_mode.rules_for("owner", "not json")["profile"]["locations"] == EX["profile"]["locations"], "bad JSON -> profile"
print("AGENT TARGETS: run uses saved targets; out-of-area saved leads are set aside OK")


# ── 3. endpoints: save/clear targets, snapshot for the scheduler, accept-all ──
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)
store.create_user("tgt-a", "tgt-pass-a1")
store.create_user("tgt-b", "tgt-pass-b1")
A = {"X-Session-Token": c.post("/auth/login", json={"user_id": "tgt-a", "passcode": "tgt-pass-a1"}).json()["token"]}
B = {"X-Session-Token": c.post("/auth/login", json={"user_id": "tgt-b", "passcode": "tgt-pass-b1"}).json()["token"]}

s = c.post("/agentmode/settings", json={"filters": {"locations": ["Denver, CO"], "remote_ok": False, "titles": ["Product Manager"]}}, headers=A)
assert s.status_code == 200 and s.json()["filters"]["locations"] == ["Denver, CO"], s.text
snap = json.loads(store.get_agent_settings("tgt-a")["rules_snapshot"])
assert snap["profile"]["locations"] == ["Denver, CO"] and snap["profile"]["remote_ok"] is False, "scheduler snapshot carries the targets"
assert c.post("/agentmode/settings", json={"notes": "x"}, headers=A).json()["filters"]["locations"] == ["Denver, CO"], "other saves keep targets"
assert c.get("/agentmode/state", headers=B).json()["settings"]["filters"] is None, "targets are per user"
assert c.post("/agentmode/settings", json={"filters": {"locations": ["x" * 81]}}, headers=A).status_code == 422
cl = c.post("/agentmode/settings", json={"clear_filters": True}, headers=A).json()
assert cl["filters"] is None and json.loads(store.get_agent_settings("tgt-a")["rules_snapshot"])["profile"]["locations"] == EX["profile"]["locations"]

for co in ("Wayne Labs", "Stark Bio", "Tyrell Data"):
    store.propose_approval("tgt-a", "watchlist_add", co.lower(), {"company": co}, "strong sponsor")
store.propose_approval("tgt-b", "watchlist_add", "oscorp", {"company": "Oscorp"}, "strong sponsor")
assert c.post("/agentmode/approvals/accept-all").status_code == 401
r = c.post("/agentmode/approvals/accept-all", headers=A)
assert r.status_code == 200 and r.json()["accepted"] == 3, r.text
assert {w["company"].lower() for w in store.list_watchlist("tgt-a")} >= {"wayne labs", "stark bio", "tyrell data"}
assert len(store.list_approvals("tgt-b", "pending")) == 1, "accept-all never touches another user's approvals"
assert c.post("/agentmode/approvals/accept-all", headers=A).json()["accepted"] == 0
assert c.post("/agentmode/approvals/1", json={"decision": "accept"}, headers=A).status_code in (200, 404), "id route still works"
print("ENDPOINTS: targets save/keep/clear + scheduler snapshot, accept-all scoped per user OK")

print("ALL OK")
