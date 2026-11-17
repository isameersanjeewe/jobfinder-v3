# ── LIVE SMOKE TEST ──────────────────────────────────────────────────────────
# Hits every real source once, prints counts + samples, and verifies the H1B sponsorship
# filter on real postings. Run from backend/:
#   python smoke_test.py            # full (includes a complete agent run, ~10-20 min)
#   python smoke_test.py --quick    # sources + H1B checks only, Indeed/Glassdoor, no agent run
import re, sys, time
from rules_service import load_rules, apply_gate, posting_visa_flag, company_signal, is_staffing, _NO_REGEX
from free_sources import fetch_ats, fetch_jobspy, fetch_company_board
from scraper_service import fetch_remotive
from agent_service import AgentRun

QUICK = "--quick" in sys.argv
rules = load_rules()
failures = []


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


def verdict(ok: bool, name: str, detail: str = ""):
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def no_reason(text: str) -> str:
    """Which phrase made a posting 'No' (for eyeballing false positives)."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    for p in rules["visa"]["posting_no_patterns"]:
        if p in t:
            i = t.index(p)
            return f'"{p}" … {t[max(0, i-50):i+len(p)+40]!r}'
    for rx in _NO_REGEX:
        m = re.search(rx, t)
        if m:
            return f"regex … {t[max(0, m.start()-40):m.end()+30]!r}"
    return "?"


if not QUICK:
    print("── Employer boards (per company) ──")
    for co, v in rules["ats_boards"].items():
        if not co.startswith("_"):
            n = len(fetch_company_board(co, v.get("ats"), v["slug"]))
            print(f"  {'✓' if n else '✗'} {co:16} {n} jobs")

print("\n── Sources ──")
jobs = check("ATS boards (all)", lambda: fetch_ats(rules["ats_boards"]))
for site in (["indeed", "glassdoor"] if QUICK else ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]):
    jobs += check(f"JobSpy/{site}", lambda s=site: fetch_jobspy(["Product Manager"], "", 24, sites=[s], locations=rules["profile"].get("locations", [])))
if not QUICK:
    jobs += check("Remotive", lambda: fetch_remotive(["product manager"]))

print("\n── Gate results (24h / 7d) ──")
for hours in (24, 168):
    counts = {}
    for j in jobs:
        g = apply_gate(j, rules, hours)["gate"]
        counts[g] = counts.get(g, 0) + 1
    print(f"  {hours:>3}h:", dict(sorted(counts.items(), key=lambda kv: -kv[1])))

# ── H1B sponsorship filter on real postings ──────────────────────────────────
print("\n── H1B sponsorship filter (live postings) ──")
visa_on = rules["visa"].get("enabled", True)
with_text = [j for j in jobs if len(j.get("description") or "") > 200]
flags = {"No": [], "Yes": [], "Transfer": [], "Unknown": []}
for j in with_text:
    flags[posting_visa_flag(f"{j['title']} {j['description']}", rules)].append(j)
print(f"  postings with a description: {len(with_text)}/{len(jobs)}  ->  "
      f"no-sponsorship {len(flags['No'])}, transfer-only {len(flags['Transfer'])}, states sponsorship {len(flags['Yes'])}, silent {len(flags['Unknown'])}")
print(f"  profile visa mode: {rules['visa'].get('mode', 'new_sponsorship')} (transfer-only postings {'kept' if rules['visa'].get('mode') == 'h1b_transfer' else 'blocked'})")
late = [j for j in flags["No"] if posting_visa_flag(j["description"][:800], rules) != "No"]
print(f"  'No' found only after char 800 (missed before the truncation fix): {len(late)}")
print("  sample no-sponsorship matches (check for false positives):")
for j in flags["No"][:6]:
    print(f"    - {j['company']} | {j['title'][:40]} | {no_reason(j['title'] + ' ' + j['description'])[:150]}")
for j in flags["Transfer"][:3]:
    print(f"    ~ transfer-only: {j['company']} | {j['title'][:40]}")
print("  sample 'states sponsorship':")
for j in flags["Yes"][:3]:
    t = j["description"].lower()
    i = next((t.index(p) for p in rules["visa"]["posting_yes_patterns"] if p in t), 0)
    print(f"    - {j['company']} | {j['title'][:40]} | {t[max(0, i-40):i+50]!r}")

# invariants: nothing that passes the FT gate may contradict the sponsorship rules
for hours in (24, 168):
    passed = [j for j in jobs if apply_gate(j, rules, hours, "ft")["gate"] == "pass"]
    if not visa_on:
        print(f"  {hours}h: visa filter disabled in profile; {len(passed)} pass")
        continue
    bad_post = [j for j in passed if j["posting_visa"] == "No" or
                (j["posting_visa"] == "Transfer" and rules["visa"].get("mode") != "h1b_transfer")]
    bad_co = [j for j in passed if j["company_h1b"] not in ("Strong", "Moderate")]
    bad_staff = [j for j in passed if is_staffing(j["company"], rules) and j["posting_visa"] != "Yes"]
    verdict(not (bad_post or bad_co or bad_staff), f"{hours}h FT passes respect sponsorship rules",
            f"{len(passed)} pass; violations: posting-no {len(bad_post)}, weak-company {len(bad_co)}, staffing {len(bad_staff)}")
blocked = [j for j in flags["No"] if apply_gate(j, rules, 168, "ft")["gate"] == "pass"]
verdict(not blocked or not visa_on, "every no-sponsorship posting is blocked on FT", f"{len(flags['No'])} checked")

# live company research (h1bdata.info): a known high-volume sponsor and a nonexistent company
probe = AgentRun(rules=rules)
big = probe.research_company_h1b("Microsoft")
none = probe.research_company_h1b("Zzqx Nonexistent Widgets Labs")
print(f"  research Microsoft -> {big.get('suggested_signal')} ({big.get('lca_rows', big.get('error'))} rows)")
print(f"  research nonexistent -> {none.get('suggested_signal')} ({none.get('lca_rows', none.get('error'))} rows)")
if big.get("error"):
    print("  [WARN] h1bdata.info unreachable/rate-limited: research falls back to Unverified (not cached)")
else:
    verdict(big.get("suggested_signal") in ("Strong", "Moderate"), "h1bdata.info: large sponsor detected")
    verdict(none.get("suggested_signal") in ("Unverified", "Risk"), "h1bdata.info: unknown company not upgraded")

if not QUICK:
    print("\n── Full agent run ──")
    r = AgentRun("product manager data platform sql agile genai llm stakeholder roadmap analytics", min_fit=0).run()
    print(r["summary"], "| provider:", r["provider"])
    print("  stats:", {k: r["stats"][k] for k in ("scanned", "duplicates_merged", "tracks")})
    for l in r["leads"][:12]:
        print(f"  score {l.get('score', '—'):>3}{' HOT' if l.get('hot_lead') else '    '} | {l.get('track','ft'):3} | fit {l.get('fit') if l.get('fit') is not None else '—':>4} | {l['company_h1b']:10} | {l['posting_visa']:7} | {l['title'][:40]} | {l['company']} | {l['location'][:22]}")
    ft = [l for l in r["leads"] if l.get("track", "ft") == "ft"]
    if visa_on:
        verdict(all(l["posting_visa"] != "No" and l["company_h1b"] in ("Strong", "Moderate")
                    and (l["posting_visa"] != "Transfer" or rules["visa"].get("mode") == "h1b_transfer") for l in ft),
                "agent FT leads all pass the sponsorship rules", f"{len(ft)} FT leads")
    c2c = [l for l in r["leads"] if l.get("track") == "c2c"]
    verdict(all(not any(p in f"{l['title']} {l.get('description','')}".lower() for p in rules["contract_track"]["exclude_patterns"]) for l in c2c),
            "agent C2C leads have no USC/GC-only or no-H1B language", f"{len(c2c)} C2C leads")
    from agent_service import dedupe_key
    keys = {dedupe_key(l)[:2] if l.get("is_remote") else dedupe_key(l) for l in r["leads"]}
    verdict(len(keys) == len(r["leads"]), "no duplicate leads", f"{len(r['leads'])} leads, {len(keys)} unique")

print("\n" + ("SMOKE: ALL CHECKS PASSED" if not failures else f"SMOKE: {len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
