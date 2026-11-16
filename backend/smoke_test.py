# ── LIVE SMOKE TEST ──────────────────────────────────────────────────────────
# Hits every real source once and prints counts + samples. Run from backend/:
#   python smoke_test.py
import time
from rules_service import load_rules, apply_gate
from free_sources import fetch_ats, fetch_jobspy, fetch_company_board
from scraper_service import fetch_remotive
from agent_service import AgentRun

rules = load_rules()


def check(name, fn):
    t = time.time()
    try:
        jobs = fn()
    except Exception as e:
        print(f"[FAIL] {name}: {e.__class__.__name__}: {e}")
        return []
    print(f"[{'OK' if jobs else 'EMPTY'}] {name}: {len(jobs)} jobs in {time.time()-t:.1f}s")
    for j in jobs[:2]:
        print(f"       {j['posted']} | {j['title'][:50]} | {j['company']} | {j['location'][:30]}")
    return jobs


print("── Employer boards (per company) ──")
for co, v in rules["ats_boards"].items():
    if not co.startswith("_"):
        n = len(fetch_company_board(co, v.get("ats"), v["slug"]))
        print(f"  {'✓' if n else '✗'} {co:16} {n} jobs")

print("\n── Sources ──")
jobs = check("ATS boards (all)", lambda: fetch_ats(rules["ats_boards"]))
for site in ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]:
    jobs += check(f"JobSpy/{site}", lambda s=site: fetch_jobspy(["Product Manager"], "", 24, sites=[s], locations=rules["profile"].get("locations", [])))
jobs += check("Remotive", lambda: fetch_remotive(["product manager"]))

print("\n── Gate results (24h) ──")
counts = {}
for j in jobs:
    g = apply_gate(j, rules, 24)["gate"]
    counts[g] = counts.get(g, 0) + 1
print(counts)

print("\n── Full agent run ──")
r = AgentRun("product manager data platform sql agile genai llm stakeholder roadmap analytics", min_fit=0).run()
print(r["summary"], "| provider:", r["provider"])
for l in r["leads"][:10]:
    print(f"  score {l.get('score', '—'):>3}{' HOT' if l.get('hot_lead') else '    '} | fit {l.get('fit') if l.get('fit') is not None else '—':>3} | {l['company_h1b']:8} | {l['title'][:45]} | {l['company']} | {l['location'][:25]}")
