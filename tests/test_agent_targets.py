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
# code-review fixes: no-comma and lower-case state codes, trusted employer-board remote flags, "no places = anywhere"
assert rules_service.named_states("Austin TX") == {"tx"} and rules_service.named_states("austin, tx") == {"tx"}
assert not rules_service.geo_ok("Austin TX", R, True), "aggregator remote flag + 'Austin TX'"
assert rules_service.geo_ok("Seattle, WA", R, True, remote_trusted=True), "employer board says remote: HQ city is fine"
assert rules_service.remote_trusted({"is_remote": True, "source": "ATS/Lever"})
assert not rules_service.remote_trusted({"is_remote": True, "source": "JobSpy/indeed"})
ANY = rules_with([], remote_ok=True)
assert rules_service.geo_ok("Austin, TX", ANY, False) and rules_service.geo_ok("Austin, TX", ANY, True), "no places = anywhere"
NR = rules_with(["Denver, CO"], remote_ok=False)
assert not rules_service.geo_ok("Remote", NR, True) and not rules_service.geo_ok("Remote, US", NR, False), "remote off = no remote"
print("GEO: picked places only; remote flag can't smuggle in another state OK")


# ── 2. Agent uses its own saved targets, and re-checks saved Full Search leads against them ──
def J(t, c, l, remote=False, source="ATS/Greenhouse"):
    j = normalize(t, c, l, f"https://x/{c}/{t}/{l}".replace(" ", "_"), today, source, "product roadmap sql agile")
    j["is_remote"] = remote
    return j

saved = [J("Product Manager", "Acme Health", "Austin, TX"),          # passed under the old (example) profile
         J("Senior Product Manager", "Globex", "TX, USA", remote=True, source="JobSpy/google"),  # aggregator flag
         J("Product Manager", "Initech", "Denver, CO"),
         J("Product Owner", "Globex", "Remote", remote=True)]
store.save_leads(saved, "ft", "owner")
# no Agent targets saved -> saved leads are NOT re-checked (Full Search gated them with that browser's own filters)
res0 = agent_mode.run_cycle("owner", NOW)
assert not any("Set aside" in e["message"] for e in res0["events"])
store.delete_kits("owner", list(store.kit_urls("owner")))
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
store.save_kit("owner", "https://x/old-remote", "c0", {"url": "https://x/old-remote", "title": "Product Manager",
                                                     "location": "North America", "is_remote": True, "board_source": "ATS/Lever"})
res2 = agent_mode.run_cycle("owner", NOW)
urls = store.kit_urls("owner")
assert "https://x/old-austin" not in urls and "https://x/old-applied" in urls and "https://x/old-remote" in urls, urls
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
store.propose_approval("tgt-a", "preference_change", "p1", {"change": "drop a title"}, "weekly review")
assert c.post("/agentmode/approvals/accept-all").status_code == 401
r = c.post("/agentmode/approvals/accept-all", headers=A)
assert r.status_code == 200 and r.json()["accepted"] == 3, r.text
assert {w["company"].lower() for w in store.list_watchlist("tgt-a")} >= {"wayne labs", "stark bio", "tyrell data"}
assert len(store.list_approvals("tgt-b", "pending")) == 1, "accept-all never touches another user's approvals"
assert [a["kind"] for a in store.list_approvals("tgt-a", "pending")] == ["preference_change"], "plan changes stay one-by-one"
assert c.post("/agentmode/approvals/accept-all", headers=A).json()["accepted"] == 0
assert c.post("/agentmode/approvals/1", json={"decision": "accept"}, headers=A).status_code in (200, 404), "id route still works"
print("ENDPOINTS: targets save/keep/clear + scheduler snapshot, accept-all scoped per user OK")

# ── 4. other users never inherit the owner's profile; JobSpy skips boards that block cloud IPs ──
os.environ["RULES_JSON"] = json.dumps(dict(EX, profile=dict(EX["profile"], titles=["Owner Only Title"])))
os.environ["OWNER_USER_ID"] = "tgt-a"
assert rules_service.load_rules(user_id="tgt-a")["profile"]["titles"] == ["Owner Only Title"], "owner keeps RULES_JSON"
st = rules_service.load_rules(user_id="tgt-b")
assert st.get("starter") and st["profile"]["titles"] != ["Owner Only Title"] and st["profile"]["locations"] == []
assert rules_service.geo_ok("Austin, TX", st, False), "starter profile = anywhere"
assert c.get("/profile/filters", headers=B).json()["starter"] is True
assert c.get("/profile/filters", headers=A).json()["starter"] is False
store.save_profile("tgt-b", dict(EX, profile=dict(EX["profile"], titles=["B Title"])))
assert rules_service.load_rules(user_id="tgt-b")["profile"]["titles"] == ["B Title"], "a saved profile still wins"
os.environ.pop("OWNER_USER_ID"); os.environ.pop("RULES_JSON")
import free_sources
assert free_sources.JOBSPY_SITES == ["indeed", "linkedin"], free_sources.JOBSPY_SITES
print("PROFILES: owner profile never leaks to other users; starter = anywhere; JobSpy sites trimmed OK")

# ── 5. clicks don't wait on GitHub: writes push in the background, and /state never pulls over an unpushed write ──
import threading, time as _time, data_repo
gate, pulls, pushes, push_ok = threading.Event(), [], [], [True]
orig = (data_repo.enabled, data_repo.write_json, agent_mode.sync_from_repo)
main._PUSH_BACKOFF = 0
data_repo.enabled = lambda: True
data_repo.write_json = lambda path, data, msg: (gate.wait(5), pushes.append(path), push_ok[0])[2]
agent_mode.sync_from_repo = lambda u: pulls.append(u)
def settle():
    for _ in range(100):
        if "tgt-a" not in main._sync_pending: return
        _time.sleep(0.05)
t0 = _time.time()
assert c.post("/agentmode/watchlist", json={"company": "Wayne Labs 2"}, headers=A).status_code == 200
assert c.post("/agentmode/watchlist", json={"company": "Wayne Labs 3"}, headers=A).status_code == 200
assert _time.time() - t0 < 2, "the response must not wait for the data-repo push"
c.get("/agentmode/state", headers=A)
assert "tgt-a" not in pulls, "a pending push must not be overwritten by the older repo copy"
gate.set(); settle()
assert len(pushes) == 2, pushes   # the second write is coalesced into one more push
st = c.get("/agentmode/state", headers=A).json()
assert "tgt-a" in pulls, "once pushed, /state pulls scheduled-run kits again"
assert st["scoring"]["max"]["h1b"] == max(EX["scoring"]["h1b_points"].values()) and st["scoring"]["hot"] == EX["scoring"]["hot_threshold"]
# a push that keeps failing (write_json returns False, it doesn't raise) must block pulls and be retried
push_ok[0] = False; pulls.clear(); pushes.clear()
c.post("/agentmode/watchlist", json={"company": "Wayne Labs 4"}, headers=A); settle()
assert "tgt-a" in main._sync_failed and len(pushes) == 3, (main._sync_failed, pushes)   # 3 attempts
c.get("/agentmode/state", headers=A)
assert "tgt-a" not in pulls, "never pull over a write that didn't reach the repo"
settle(); push_ok[0] = True
c.get("/agentmode/state", headers=A); settle()
assert "tgt-a" not in main._sync_failed, "/state retried the push and it went through"
assert any(w["company"].lower() == "wayne labs 4" for w in store.list_watchlist("tgt-a")), store.list_watchlist("tgt-a")
data_repo.enabled, data_repo.write_json, agent_mode.sync_from_repo = orig
kits = [k for k in store.list_kits("owner", 30) if k.get("score_parts")]
assert kits and set(kits[0]["score_parts"]) >= {"h1b", "title", "geo", "industry", "contact"}, "kits explain their Match score"
assert all(isinstance(k.get("matched"), list) for k in kits)
print("SYNC: writes return at once, push in the background, no stale pull OK")

# ── 6. skip: "not for me" / "too senior" / hide a company, and the next best pick comes in ──
assert agent_mode.years_asked("8+ years of product management experience and 3 years of SQL experience") == 8
assert agent_mode.years_asked("5-7 years of relevant experience") == 5
assert agent_mode.years_asked("Founded 20 years ago. 401k after 2 years.") is None, "only years tied to experience count"
assert agent_mode.years_asked("at least 10 yrs experience") == 10
def JD(t, co, desc):
    j = normalize(t, co, "Remote", f"https://y/{co}/{t}".replace(" ", "_"), today, "ATS/Greenhouse", desc)
    j["is_remote"] = True
    return j
pool = [JD("Product Manager", "Acme Health", "roadmap sql agile; 4+ years of experience"),
        JD("Product Owner", "Globex", "roadmap sql agile; 12+ years of product experience"),
        JD("Technical Product Manager", "Initech", "roadmap agile sql"),
        JD("Data Product Manager", "Acme Health", "roadmap sql agile"),
        JD("Platform Product Owner", "Globex", "roadmap sql; 3 years experience"),
        JD("Growth Product Manager", "Initech", "roadmap sql agile")]
store.save_leads(pool, "ft", "tgt-b")
store.add_watchlist("tgt-b", "Acme Health", "", "acmehealth", "manual")
store.propose_approval("tgt-b", "watchlist_add", "acme health inc", {"company": "Acme Health Inc"}, "strong sponsor")
agent_mode.run_cycle("tgt-b", NOW)
U = {j["company"] + "/" + j["title"]: j["url"] for j in pool}
kits0 = store.kit_urls("tgt-b")
assert len(kits0) >= 3, kits0

# a years limit drops kits that ask for more and refills; saving targets or "use profile" keeps the skip rules
r = c.post("/agentmode/settings", json={"filters": {"max_years": 8}}, headers=B).json()
assert r["filters"] == {"max_years": 8}, r
assert U["Globex/Product Owner"] not in store.kit_urls("tgt-b")
assert r["pruned"] == (1 if U["Globex/Product Owner"] in kits0 else 0)
assert json.loads(store.get_agent_settings("tgt-b")["rules_snapshot"])["profile"]["max_years"] == 8, "scheduled runs get the limit"
assert not agent_mode._targets_set(store.get_agent_settings("tgt-b")), "skip rules alone are not custom targets"
r = c.post("/agentmode/settings", json={"filters": {"titles": ["Product Manager", "Product Owner"], "locations": [], "remote_ok": True}}, headers=B).json()
assert r["filters"]["max_years"] == 8 and r["filters"]["titles"], r
r = c.post("/agentmode/settings", json={"clear_filters": True}, headers=B).json()
assert r["filters"] == {"max_years": 8}, r

# skip one pick: Skipped status (a down signal), kit gone, the next best saved lead gets a kit
victim = U["Globex/Platform Product Owner"]
assert victim in store.kit_urls("tgt-b")
before = store.kit_urls("tgt-b")
d = c.post("/agentmode/kits/skip", json={"url": victim, "reason": "too_senior"}, headers=B).json()
assert d["removed"] == [victim] and victim not in store.kit_urls("tgt-b"), d
assert store.lead_statuses("tgt-b")[victim]["status"] == "Skipped"
left = {j["url"] for j in pool} - before - {U["Globex/Product Owner"]}
assert [k["url"] for k in d["added"]] and d["added"][0]["url"] in left, (d["added"], left)
assert "years_asked" in d["added"][0]
assert c.post("/agentmode/kits/skip", json={"url": victim}, headers=B).status_code == 404, "already skipped"
assert c.post("/agentmode/kits/skip", json={"url": next(iter(store.kit_urls("tgt-b")))}, headers=A).status_code == 404, "another user's kit"

# hide a company from one of its picks: every unacted kit from it goes, it leaves the watchlist, its suggestions are dismissed
acme = next(u for u in store.kit_urls("tgt-b") if "Acme" in u)
d = c.post("/agentmode/kits/skip", json={"url": acme, "reason": "company"}, headers=B).json()
assert not any("Acme" in u for u in store.kit_urls("tgt-b")), store.kit_urls("tgt-b")
assert d["filters"]["exclude_companies"] == ["Acme Health"] and d["filters"]["max_years"] == 8 and d["dismissed"] == 1, d
assert not any(w["company"] == "acme health" for w in store.list_watchlist("tgt-b"))
assert not [a for a in store.list_approvals("tgt-b", "pending") if "acme" in a["key"]]
assert "acme health" in json.loads(store.get_agent_settings("tgt-b")["rules_snapshot"])["companies"]["excluded"]
res = agent_mode.run_cycle("tgt-b", NOW)
assert not any("Acme" in u or u == U["Globex/Product Owner"] for u in res["picks"]), res["picks"]
for j in pool:   # nothing hidden or over the limit comes back, and nothing hidden is suggested for the watchlist
    if j["company"] == "Acme Health" or j["url"] == U["Globex/Product Owner"]:
        assert j["url"] not in store.kit_urls("tgt-b")
n = agent_mode.propose_watchlist("tgt-b", agent_mode.rules_for("tgt-b", store.get_agent_settings("tgt-b")["filters"]),
                                 [dict(pool[0], company_h1b="Strong", hot_lead=True)], {}, [], lambda *a: None)
assert n == 0, "a hidden company is never suggested"

# hide by name (Suggestions' button / Agent settings), un-hide, and validation
assert c.post("/agentmode/hide-company", json={"companies": []}, headers=B).status_code == 422
d = c.post("/agentmode/hide-company", json={"companies": ["Initech", "Globex"]}, headers=B).json()
assert set(d["filters"]["exclude_companies"]) == {"Acme Health", "Initech", "Globex"} and "added" in d, d
assert not store.kit_urls("tgt-b") - {k for k in store.kit_urls("tgt-b") if store.lead_statuses("tgt-b").get(k)}
r = c.post("/agentmode/settings", json={"filters": {"exclude_companies": ["Globex"], "max_years": 0}}, headers=B).json()
assert r["filters"] == {"exclude_companies": ["Globex"]}, r
assert c.post("/agentmode/settings", json={"filters": {"max_years": 31}}, headers=B).status_code == 422
print("SKIP: skip/too senior/hide company refill the picks; years limit and hidden list persist OK")

print("ALL OK")
