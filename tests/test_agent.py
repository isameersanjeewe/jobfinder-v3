import sys, os, json, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
os.environ["DB_PATH"] = "/tmp/test_leads.db"
if os.path.exists("/tmp/test_leads.db"): os.remove("/tmp/test_leads.db")
os.environ["RULES_LOCAL_PATH"] = "/tmp/test_rules.local.json"
if os.path.exists("/tmp/test_rules.local.json"): os.remove("/tmp/test_rules.local.json")
os.environ.pop("RULES_JSON", None)

# Test profile = committed example + fictional companies. Never depends on anyone's personal rules.json.
EX = json.load(open(os.path.join(HERE, "..", "backend", "rules.example.json")))
EX["companies"].update(strong=["acme health", "initech", "globex"], moderate=["soylent"],
                       risk=["umbrella bank"], excluded=["hooli"])
json.dump(EX, open("/tmp/test_rules.json", "w"))
os.environ["RULES_PATH"] = "/tmp/test_rules.json"

import agent_service, free_sources, rules_service
from scraper_service import normalize
today = datetime.date.today().isoformat(); old = "2026-09-01"
def J(t,c,l,d,desc="",remote=False,u=None):
    j = normalize(t,c,l,u or f"https://x/{c}/{t}".replace(" ","_"),d,"mock",desc); j["is_remote"]=remote; return j
FIX = [
 J("Manager, Technical Product Owner - Data & Analytics","Acme Health","Round Rock, TX",today,"healthcare data platform sql agile genai 10+ years"),
 J("Senior Product Manager","Umbrella Bank","Austin, TX",today,"payments"),
 J("Technical Program Manager","Initech","Remote",today,"Initech is unable to sponsor a work visa for this role",remote=True),
 J("Senior Product Manager, Analytics","Globex","Austin, TX",today,"ai llm saas stakeholder roadmap"),
 J("Product Manager","Initech","Seattle, WA",today,"payments"),
 J("Product Manager","Initech","Remote",old,"payments",remote=True),
 J("Product Manager, AI","Tinyco","Remote",today,"llm rag",remote=True),
 J("Software Engineer","Initech","Remote",today,"",remote=True),
]
agent_service.fetch_ats = lambda boards, companies=None: [dict(j) for j in FIX[:4]]
agent_service.fetch_jobspy = lambda *a, **k: [dict(j) for j in FIX[3:]]
agent_service.fetch_remotive = lambda kws: []
_real_research = agent_service.AgentRun.research_company_h1b
agent_service.AgentRun.research_company_h1b = lambda self, company: {"company":company,"lca_rows":3,"suggested_signal":"Risk"}
resume = "Product manager data platform sql agile genai llm saas stakeholder roadmap analytics python"

r = agent_service.AgentRun(resume, min_fit=30).run()
print("SUMMARY:", r["summary"]); print("GATES:", r["stats"]["gates"])
print("LEADS:", [(l["company"], l["fit"], l["is_new"]) for l in r["leads"]])
assert {l["company"] for l in r["leads"]} == {"Acme Health","Globex"}, r["leads"]
r2 = agent_service.AgentRun(resume, min_fit=30).run()
assert r2["new_count"] == 0, "second run should report nothing new"

# system prompt is built from the profile, nothing hardcoded
sp = agent_service.AgentRun(resume)._system()
assert "Austin, TX" in sp and "sponsorship" in sp and "{" not in sp, sp

# scripted OpenAI-compatible LLM loop
script = iter([
 {"tool_calls":[{"id":"1","function":{"name":"search_jobs","arguments":json.dumps({"source":"ats"})}}]},
 {"tool_calls":[{"id":"2","function":{"name":"score_jobs","arguments":json.dumps({"ids":["j0","j3"]})}}]},
 {"tool_calls":[{"id":"3","function":{"name":"save_leads","arguments":json.dumps({"ids":["j0","j1","j3"]})}}]},
 {"tool_calls":[{"id":"4","function":{"name":"finish","arguments":json.dumps({"summary":"2 leads: Acme, Globex"})}}]},
])
class R:
    def __init__(s,m): s.m=m
    def json(s): return {"choices":[{"message":{"role":"assistant","content":None,**s.m}}]}
agent_service.requests.post = lambda *a, **k: R(next(script))
agent_service.PROVIDER = "openai"
r3 = agent_service.AgentRun(resume, user_id="u2").run()
print("LLM LOOP:", r3["summary"], [t["tool"] for t in r3["trace"]], len(r3["leads"]))
assert len(r3["leads"]) == 2 and r3["summary"].startswith("2 leads")
agent_service.PROVIDER = "none"

# scoring model (H1B 35 / Title 25 / Geo 15 / Industry 15 / Contact 10; Hot >= 65)
from fit_service import score_lead
rules = rules_service.load_rules()
def S(job, rl=None): rl = rl or rules; return score_lead(rules_service.apply_gate(dict(job), rl, 24), rl)
s1 = S(J("Senior Product Manager","Acme Health","Round Rock, TX",today,"data platform"))
print("SCORE strong local PM:", s1["score"], s1["score_parts"])
assert s1["score"] >= 65 and s1["hot_lead"]
s2 = S(J("Product Lead","Tinyco","Remote",today,"workflow tooling",remote=True))
print("SCORE unverified remote stretch:", s2["score"], s2["score_parts"])
assert s2["score"] < 65 and not s2["hot_lead"] and s2["score_parts"]["title"] == 5
s3 = S(J("Product Manager","Initech","Remote",today,"unable to sponsor",remote=True))
assert s3["score_parts"]["h1b"] == 0, "explicit no-sponsorship -> 0 H1B points"
s4 = S(J("Product Manager","Initech","Remote",today,"genai agentic workflows in a regulated compliance setting",remote=True))
assert s4["score_parts"]["industry"] == 15, "GenAI + regulated override"
assert S(J("Technical Program Manager","Initech","Austin, TX",today,"payments"))["score_parts"]["title"] == 15
assert all("score" in l and "hot_lead" in l for l in r["leads"])
assert [l["score"] for l in r["leads"]] == sorted([l["score"] for l in r["leads"]], reverse=True)

# visa.enabled=false: no visa/company gates, H1B factor gets full points
nv = json.loads(json.dumps(rules)); nv["visa"]["enabled"] = False
assert rules_service.apply_gate(J("Product Manager","Initech","Remote",today,"unable to sponsor",remote=True), nv, 24)["gate"] == "pass"
assert rules_service.apply_gate(J("Product Manager","Tinyco","Austin, TX",today), nv, 24)["gate"] == "pass"
assert S(J("Product Manager","Tinyco","Austin, TX",today), nv)["score_parts"]["h1b"] == 35
assert "no sponsorship needed" in agent_service.AgentRun(resume, rules=nv)._system()

# profile loading: RULES_JSON env (e.g. a CI secret) wins over files
os.environ["RULES_JSON"] = json.dumps(dict(EX, profile=dict(EX["profile"], name="from-env")))
assert rules_service.load_rules()["profile"]["name"] == "from-env"
del os.environ["RULES_JSON"]
assert rules_service.load_rules()["profile"]["name"] == "example"
print("PROFILE: visa toggle + RULES_JSON loading OK")

# recency window: weekly (168h) keeps a 5-day-old job, daily (24h) drops it
five_days = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
pm5 = J("Senior Product Manager","Globex","Austin, TX",five_days,"saas")
assert rules_service.apply_gate(dict(pm5), rules, 168)["gate"] == "pass"
assert rules_service.apply_gate(dict(pm5), rules, 24)["gate"] == "stale_or_undated"
_fx = agent_service.fetch_ats
agent_service.fetch_ats = lambda boards, companies=None: [dict(pm5, url="https://x/globex5d")]
wk = rules_service.load_rules(); wk["profile"]["recency_hours"] = 168
assert agent_service.AgentRun(resume, user_id="wk", rules=wk).search_jobs("ats")["gate_counts"] == {"pass": 1}
assert agent_service.AgentRun(resume, user_id="dy").search_jobs("ats")["gate_counts"] == {"stale_or_undated": 1}
agent_service.fetch_ats = _fx
print("RECENCY: 168h passes 5-day-old job, 24h rejects it")

# staffing-firm rule: FT fails unless the posting confirms sponsorship; C2C passes
apex = J("Product Manager - II","Apex Systems","Austin, TX",today,"data platform roadmap")
assert rules_service.apply_gate(dict(apex), rules, 24, "ft")["gate"] == "staffing_c2c_only"
assert rules_service.apply_gate(dict(apex), rules, 168, "c2c")["gate"] == "pass"
apex_ok = dict(apex, description="H1B sponsorship available for this role")
rules_ok = json.loads(json.dumps(rules)); rules_ok["companies"]["moderate"].append("apex systems")
assert rules_service.apply_gate(apex_ok, rules_ok, 24, "ft")["gate"] == "pass"
a = agent_service.AgentRun(resume, user_id="apex"); a.pool["j0"] = rules_service.apply_gate(dict(apex), rules, 24)
a.set_company_signal("Apex Systems", "Moderate")  # research override must not bypass the staffing rule
assert a.pool["j0"]["gate"] == "staffing_c2c_only"
print("STAFFING: Apex fails FT, passes C2C")

# persisted H1B research: 2nd run makes no research call for the same company
os.remove("/tmp/test_rules.local.json")
agent_service.AgentRun.research_company_h1b = _real_research
calls = []
class H:  # fake h1bdata.info page: 2 LCA rows, no PM titles -> Risk
    text = "<tr><th>h</th></tr>" + "<tr><td>Newco</td><td>DATA ANALYST</td><td>x</td><td>2025</td></tr>" * 2
def fake_get(*a, **k): calls.append(k.get("params", {}).get("em")); return H()
_get = agent_service.requests.get; agent_service.requests.get = fake_get
newco = J("Senior Product Manager","Newco Labs","Austin, TX",today,"saas",u="https://x/newco")
agent_service.fetch_ats = lambda boards, companies=None: [dict(newco)]
agent_service.fetch_jobspy = lambda *a, **k: []
agent_service.AgentRun(resume, user_id="p1").run()
assert calls == ["Newco Labs"], calls
saved = json.load(open("/tmp/test_rules.local.json"))["company_signals"]
assert saved["newco labs"]["signal"] == "Risk", saved
assert rules_service.company_signal("Newco Labs", rules_service.load_rules()) == "Risk"
r5 = agent_service.AgentRun(resume, user_id="p2").run()
assert calls == ["Newco Labs"], f"second run re-researched: {calls}"
assert r5["stats"]["gates"] == {"company_risk": 1}
# Unverified result is cached too (no re-lookup), and manual tiers are never overridden by research
rules_service.save_company_signal("Mystery Co", "Unverified", "no filings")
rules_service.save_company_signal("Umbrella Bank", "Strong", "h1bdata")
lr = rules_service.load_rules()
assert agent_service.AgentRun(rules=lr).research_company_h1b("Mystery Co")["cached"] and calls == ["Newco Labs"]
assert rules_service.company_signal("Umbrella Bank", lr) == "Risk"
agent_service.requests.get = _get
print("RESEARCH CACHE: second run made 0 research calls")

# Workday fetcher (mocked HTTP): relative dates, detail enrichment, expired-deadline skip
wd = free_sources._workday_days_ago
assert (wd("Posted Today"), wd("Posted Yesterday"), wd("Posted 3 Days Ago"), wd("Posted 30+ Days Ago"), wd("")) == (0, 1, 3, 31, None)
class WR:
    status_code = 200
    def __init__(s, d): s.d = d
    def json(s): return s.d
    def raise_for_status(s): pass
LIST = {"jobPostings": [
  {"title": "Senior Product Manager, AI", "externalPath": "/job/a", "locationsText": "2 Locations", "postedOn": "Posted Today"},
  {"title": "Program Manager", "externalPath": "/job/b", "locationsText": "USA - Remote", "postedOn": "Posted 2 Days Ago"},
  {"title": "Sales Engineer", "externalPath": "/job/c", "locationsText": "TX", "postedOn": "Posted Today"},
  {"title": "Product Owner", "externalPath": "/job/d", "locationsText": "TX", "postedOn": "Posted 30+ Days Ago"}]}
DET = {"/job/a": {"startDate": today, "location": "Seattle, Washington, United States", "additionalLocations": ["Round Rock, Texas, United States"],
                  "jobDescription": "<p>GenAI saas</p>", "remoteType": "Hybrid"},
       "/job/b": {"startDate": today, "endDate": "2020-01-01", "location": "Remote"}}
_rq = free_sources.requests.request
free_sources.requests.request = lambda m, url, **k: WR(LIST if m == "POST" else {"jobPostingInfo": DET[url[url.index("/job/"):]]})
wjobs = {j["title"]: j for j in free_sources.fetch_company_board("acme health", "workday", "acme.wd1/Careers")}
free_sources.requests.request = _rq
assert set(wjobs) == {"Senior Product Manager, AI", "Product Owner"}, wjobs.keys()  # sales filtered, expired /job/b skipped
wa = wjobs["Senior Product Manager, AI"]
assert wa["posted"] == today and "Round Rock" in wa["location"] and "GenAI" in wa["description"]
assert wa["url"] == "https://acme.wd1.myworkdayjobs.com/Careers/job/a" and wa["source"] == "ATS/Workday"
assert rules_service.apply_gate(dict(wa), rules, 24)["gate"] == "pass"
assert rules_service.apply_gate(dict(wjobs["Product Owner"]), rules, 168)["gate"] == "stale_or_undated"
print("WORKDAY: dates, detail enrichment, deadline skip OK")

# geography: a specific out-of-area US city must fail even with "United States" in it
for loc, ok in [("Irvine, California, United States", False), ("Boston, MA, USA", False), ("United States", True),
                ("USA", True), ("Round Rock, Texas, United States", True), ("Austin, TX, USA", True),
                ("US-Remote", True), ("Sunnyvale, CA; Austin, TX", True)]:
    assert rules_service.geo_ok(loc, rules) == ok, loc

# JobSpy searches the profile's locations + remote, never hardcoded places
seen_locs = []
class DF:
    def fillna(s, v): return s
    def to_dict(s, how): return []
import types
sys.modules["jobspy"] = types.SimpleNamespace(scrape_jobs=lambda **k: seen_locs.append((k["location"], k["is_remote"])) or DF())
free_sources.fetch_jobspy(["Product Manager"], "", 24, sites=["indeed"], locations=["Austin, TX", "Denver, CO"])
assert seen_locs == [("Austin, TX", False), ("Denver, CO", False), ("United States", True)], seen_locs
del sys.modules["jobspy"]

# a hung JobSpy call is abandoned, not allowed to freeze the run
import time as _t
assert free_sources._with_timeout(lambda x: x * 2, 1, x=21) == 42
_t0 = _t.time()
try:
    free_sources._with_timeout(lambda: _t.sleep(30), 1); raise AssertionError("no timeout")
except TimeoutError:
    assert _t.time() - _t0 < 3
print("JOBSPY: profile locations + timeout guard OK")

# Sheets append maps to the tab's own header, never FIELDS order
from export_service import sheet_rows
HDR = ["Rank","Role","Vendor","Notes","Location","Posted","Job Link","Status"]
row = sheet_rows([dict(apex, posting_visa="Unknown")], HDR)[0]
assert row[1] == "Product Manager - II" and row[2] == "Apex Systems" and row[4] == "Austin, TX"
assert row[6] == apex["url"] and row[7] == "Not Applied" and row[0] == "" and row[3] == "" and row[5] == today
FT_HDR = ["Applied","Date Pulled","Role","Company","Location","H1B Signal","Source","Job Link","Contact","Score","Hot Lead"]
row = sheet_rows([dict(r["leads"][0])], FT_HDR)[0]
assert row[2] and row[3] == r["leads"][0]["company"] and row[7] == r["leads"][0]["url"] and row[9] == r["leads"][0]["score"]
print("SHEETS: rows aligned to existing tab headers")

# small-model robustness: stringly-typed args are coerced; stopping without saving falls back to the plan
agent_service.fetch_ats = lambda boards, companies=None: [dict(j) for j in FIX[:4]]
agent_service.fetch_jobspy = lambda *a, **k: []
ag = agent_service.AgentRun(resume, user_id="u_small")
res = ag.search_jobs("ats", keywords="['job title 1', 'job title 2']", hours="24", remote="true")
assert res["gate_counts"].get("pass") == 2, res
script = iter([
 {"tool_calls":[{"id":"1","function":{"name":"search_jobs","arguments":json.dumps({"source":"ats","hours":"24"})}}]},
 {"content":'{"name": "search_jobs", "parameters": {"source": "ats"}}'}])
agent_service.requests.post = lambda *a, **k: R(next(script))
agent_service.PROVIDER = "openai"
r6 = agent_service.AgentRun(resume, user_id="u_small2").run()
agent_service.PROVIDER = "none"
assert len(r6["leads"]) == 2 and r6["summary"].startswith("LLM stopped early"), r6["summary"]
print("SMALL-LLM: arg coercion + fallback OK")

# exports + API
from export_service import to_csv, to_xlsx
hdr = to_csv(r["leads"]).splitlines()[0]
assert "company_h1b" in hdr and "score" in hdr and "hot_lead" in hdr and "fit" in hdr
open("/tmp/l.xlsx","wb").write(to_xlsx(r["leads"]))
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)
print("/agent/run", c.post("/agent/run", json={"resume_text":resume,"user_id":"api"}).status_code)
assert c.post("/agent/run", json={"resume_text":resume,"user_id":"api7","hours":168}).status_code == 200
print("/leads json", c.get("/leads").json()["count"])
print("/leads csv", c.get("/leads?format=csv").headers["content-type"])
print("/leads xlsx", c.get("/leads?format=xlsx").status_code)
print("/export/sheets (no creds)", c.post("/export/sheets", json={"sheet_id":"abc"}).json())
print("/search", c.post("/search", json={"sources":["remotive"]}).status_code)
print("ALL OK")
