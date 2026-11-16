# ── AGENT SERVICE ────────────────────────────────────────────────────────────
# What makes this agentic: the LLM gets a GOAL (find fresh, H1B-viable, high-fit leads),
# a set of TOOLS (search, research H1B, score, save), and decides the loop itself:
# which sources/queries to run, when results are too thin and the search should broaden,
# which unknown companies are worth researching, and what to save.
#
# Provider-agnostic, free options first:
#   LLM_PROVIDER=openai  + LLM_BASE_URL=http://localhost:11434/v1 (Ollama, free/local)
#                        or https://api.groq.com/openai/v1 (Groq free tier)
#                        or https://generativelanguage.googleapis.com/v1beta/openai/ (Gemini free tier)
#   LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY (paid)
#   LLM_PROVIDER=none -> deterministic plan (same tools, fixed order). Still fully functional.

import json, os, re, requests
from scraper_service import fetch_remotive
from free_sources import fetch_jobspy, fetch_ats
from rules_service import load_rules, apply_gate, save_company_signal
from fit_service import keyword_fit, score_lead
import store

PROVIDER = os.getenv("LLM_PROVIDER", "none").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5" if PROVIDER == "anthropic" else "llama3.1")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "14"))

TOOLS = [
    {"name": "search_jobs",
     "description": "Search one source. Results are gated by the user's rules (title, geography, recency, visa). Returns counts per gate outcome, passing jobs, and unverified companies worth researching.",
     "schema": {"type": "object", "properties": {
         "source": {"type": "string", "enum": ["ats", "jobspy", "remotive"], "description": "ats = direct employer boards for target companies (freshest). jobspy = Indeed/LinkedIn/Google/ZipRecruiter/Glassdoor. remotive = remote-only."},
         "keywords": {"type": "array", "items": {"type": "string"}, "description": "Job titles to query (max 4)."},
         "location": {"type": "string"},
         "hours": {"type": "integer", "description": "Recency window in hours. 24 default, 168 to broaden."},
         "remote": {"type": "boolean"},
         "track": {"type": "string", "enum": ["ft", "c2c"]}},
         "required": ["source"]}},
    {"name": "research_company_h1b",
     "description": "Look up a company's public H1B LCA filings (h1bdata.info). Use for companies the gate marked unverified.",
     "schema": {"type": "object", "properties": {"company": {"type": "string"}}, "required": ["company"]}},
    {"name": "set_company_signal",
     "description": "Record an H1B signal for a company after research, then re-gate its jobs. Only upgrade to Strong/Moderate with real, recent filing evidence.",
     "schema": {"type": "object", "properties": {
         "company": {"type": "string"}, "signal": {"type": "string", "enum": ["Strong", "Moderate", "Risk", "Unverified"]},
         "basis": {"type": "string"}}, "required": ["company", "signal", "basis"]}},
    {"name": "score_jobs",
     "description": "Score job ids: lead score 0-100 (H1B 35/Title 25/Geo 15/Industry 15/Contact 10; hot_lead >= 65) plus separate resume keyword fit (0-100) with matched/missing skills and years gap.",
     "schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}},
    {"name": "save_leads",
     "description": "Save chosen job ids to the user's lead tracker. Only save jobs whose gate is 'pass'.",
     "schema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}}, "track": {"type": "string", "enum": ["ft", "c2c"]}},
         "required": ["ids"]}},
    {"name": "finish", "description": "End the run with a short summary for the user (what was found, skipped sources, follow-ups).",
     "schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]

SYSTEM = """You are a job-search agent for one user. Goal: return every fresh, eligible, high-fit lead, and nothing else.
Rules (from the user's profile, non-negotiable):
- Recency: default {hours}h. Only broaden to 168h if fewer than 5 leads pass after all sources.
- Geography: {locations}{remote}. The search tools already apply the user's gate.
- {visa}
- Research unverified companies only if they have a passing-otherwise job. Upgrade only with recent same-title or high overall LCA volume. Under ~10 filings/year = Risk.
- Priority industries, highest first: {industries}. Weak fits: {penalties}.
Process: search ats first, then jobspy, then remotive. Score passing jobs, save those with fit >= {min_fit} (or strong strategic fit), then finish.
If a source errors or returns 0, note it and move on. Be economical: few, targeted calls."""


class AgentRun:
    def __init__(self, resume_text: str = "", user_id: str = "default", rules: dict | None = None,
                 min_fit: int = 40, resume_years: int | None = None):
        self.rules = rules or load_rules()
        self.resume = resume_text
        self.user_id = user_id
        self.min_fit = min_fit
        self.resume_years = resume_years
        self.pool: dict[str, dict] = {}
        self.overrides: dict[str, str] = {}
        self.saved: list[dict] = []
        self.trace: list[dict] = []
        self.summary = ""

    # ── tools ────────────────────────────────────────────────────────────────
    def _gate(self, job, hours, track):
        apply_gate(job, self.rules, hours, track)
        sig = self.overrides.get(job.get("company", "").lower())
        if sig and track == "ft" and job["gate"] in ("company_unverified", "pass", "company_risk"):
            job["company_h1b"] = sig
            if job["posting_visa"] != "No":
                job["gate"] = "pass" if sig in ("Strong", "Moderate") else f"company_{sig.lower()}"
        return job

    def search_jobs(self, source, keywords=None, location="", hours=None, remote=False, track="ft"):
        # small local models send "24", "true", or a stringified list; coerce instead of crashing the gate
        hours = int(hours) if str(hours or "").strip().isdigit() else self.rules["profile"]["recency_hours"]
        remote = str(remote).lower() == "true"
        if isinstance(keywords, str):
            keywords = re.findall(r"[A-Za-z][\w &/-]+", keywords)
        if keywords and any(re.fullmatch(r"(job )?title ?\d*", k.strip().lower()) for k in keywords):
            keywords = None  # placeholder titles -> use the profile's titles
        kws = keywords or self.rules["profile"]["titles"][:4]
        if source == "ats":
            jobs = fetch_ats(self.rules["ats_boards"])
        elif source == "jobspy":
            jobs = fetch_jobspy(kws, location, hours, remote=remote,
                                proxies=[p for p in os.getenv("JOBSPY_PROXIES", "").split(",") if p] or None,
                                locations=self.rules["profile"].get("locations", []))
        else:
            jobs = fetch_remotive([k.lower() for k in kws])
        counts, passing, unverified = {}, [], set()
        for j in jobs:
            j["id"] = f"j{len(self.pool)}"
            j["track"] = track
            j["hours"] = hours
            self._gate(j, hours, track)
            if any(p["url"] == j["url"] for p in self.pool.values()):
                continue
            self.pool[j["id"]] = j
            counts[j["gate"]] = counts.get(j["gate"], 0) + 1
            if j["gate"] == "pass":
                passing.append(j)
            elif j["gate"] == "company_unverified":
                unverified.add(j["company"])
        passing.sort(key=lambda j: j["geo_rank"])
        return {"source": source, "fetched": len(jobs), "gate_counts": counts,
                "passing": [{k: j[k] for k in ("id", "title", "company", "location", "posted", "company_h1b")} for j in passing[:25]],
                "unverified_companies": sorted(unverified)[:15]}

    def research_company_h1b(self, company):
        cached = self.rules.get("researched", {}).get(company.lower())
        if cached:  # persisted in rules.local.json by an earlier run
            return {"company": company, "cached": True, "suggested_signal": cached["signal"],
                    "basis": cached.get("basis", ""), "researched_on": cached.get("date")}
        try:
            r = requests.get("https://h1bdata.info/index.php", params={"em": company, "job": "", "city": "", "year": "all years"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            rows = re.findall(r"<tr>(.*?)</tr>", r.text, re.S)[1:]
            titles = [re.sub(r"<[^>]+>", "", re.findall(r"<td>(.*?)</td>", row, re.S)[1]).upper() for row in rows if row.count("<td") > 3]
            years = re.findall(r"\b(20\d\d)\b", r.text)
            pm = [t for t in titles if "PRODUCT MANAGER" in t or "PROGRAM MANAGER" in t or "PRODUCT OWNER" in t]
            n = len(titles)
            hint = "Strong" if n >= 100 else "Moderate" if n >= 20 or pm else "Risk" if n else "Unverified"
            return {"company": company, "lca_rows": n, "pm_family_rows": len(pm), "latest_year": max(years) if years else None,
                    "suggested_signal": hint, "verify": f"https://www.myvisajobs.com/search/?q={company}"}
        except Exception as e:
            return {"company": company, "error": f"lookup failed: {e.__class__.__name__}", "suggested_signal": "Unverified"}

    def set_company_signal(self, company, signal, basis=""):
        self.overrides[company.lower()] = signal
        try:
            self.rules.setdefault("researched", {})[company.lower()] = save_company_signal(company, signal, basis)
        except OSError:
            pass  # read-only disk (e.g. some hosts): keep in-run override only
        changed = 0
        for j in self.pool.values():
            if j.get("company", "").lower() == company.lower():
                before = j["gate"]
                self._gate(j, j.get("hours", 24), j.get("track", "ft"))
                changed += before != j["gate"]
        return {"company": company, "signal": signal, "jobs_regated": changed}

    def score_jobs(self, ids):
        out = []
        bt, pt = self.rules["industry_boost"]["terms"], self.rules["industry_boost"]["penalty_terms"]
        for i in ids:
            j = self.pool.get(i)
            if not j:
                continue
            j.update(keyword_fit(j, self.resume, self.resume_years, bt, pt))
            j.update(score_lead(j, self.rules))
            out.append({"id": i, "title": j["title"], "company": j["company"], "score": j["score"],
                        "hot_lead": j["hot_lead"], "fit": j["fit"],
                        "missing": j["missing"][:5], "years_gap": j["years_gap"]})
        return {"scores": out}

    def save_leads(self, ids, track="ft"):
        jobs = [self.pool[i] for i in ids if i in self.pool and self.pool[i]["gate"] == "pass"]
        for j in jobs:
            if "score" not in j:  # LLM may save without scoring first
                j.update(score_lead(j, self.rules))
        added = store.save_leads(jobs, track, self.user_id)
        self.saved += jobs
        return {"saved": len(jobs), "new_in_tracker": added, "rejected_not_passing": len(ids) - len(jobs)}

    def finish(self, summary):
        self.summary = summary
        return {"ok": True}

    def call(self, name, args):
        try:
            res = getattr(self, name)(**args)
        except Exception as e:
            res = {"error": f"{e.__class__.__name__}: {e}"}
        self.trace.append({"tool": name, "args": args, "result_keys": list(res.keys()) if isinstance(res, dict) else None})
        return res

    # ── loops ────────────────────────────────────────────────────────────────
    def run(self, goal: str = "") -> dict:
        goal = goal or "Find today's new leads for my search."
        if PROVIDER == "anthropic" and ANTHROPIC_KEY:
            self._loop_anthropic(goal)
        elif PROVIDER == "openai":
            self._loop_openai(goal)
        else:
            self._plan()
        return self.result()

    def _system(self):
        p, sc = self.rules["profile"], self.rules.get("scoring", {})
        visa = ("Visa: the user needs employer sponsorship; company must be a Strong/Moderate sponsor; any posting saying "
                "no sponsorship/citizenship/clearance is out.") if self.rules["visa"].get("enabled", True) else "Visa: no sponsorship needed."
        fill = {"{min_fit}": str(self.min_fit), "{hours}": str(p["recency_hours"]),
                "{locations}": ", ".join(p.get("locations", [])) or "any",
                "{remote}": ", or US remote" if p.get("remote_ok") else "",
                "{visa}": visa,
                "{industries}": "; ".join(", ".join(t["terms"][:3]) for t in sc.get("industry_priority", [])) or "none set",
                "{penalties}": ", ".join(self.rules["industry_boost"].get("penalty_terms", [])) or "none"}
        s = SYSTEM
        for k, v in fill.items():
            s = s.replace(k, v)
        return s

    def _loop_anthropic(self, goal):
        msgs = [{"role": "user", "content": goal}]
        tools = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in TOOLS]
        for _ in range(MAX_STEPS):
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=90, headers={
                "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": LLM_MODEL, "max_tokens": 1500, "system": self._system(), "tools": tools, "messages": msgs}).json()
            content = r.get("content", [])
            msgs.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                self.summary = self.summary or " ".join(b.get("text", "") for b in content if b.get("type") == "text")
                return
            results = [{"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(self.call(u["name"], u["input"]))} for u in uses]
            msgs.append({"role": "user", "content": results})
            if any(u["name"] == "finish" for u in uses):
                return
        self._plan(skip_search=bool(self.pool))

    def _loop_openai(self, goal):
        msgs = [{"role": "system", "content": self._system()}, {"role": "user", "content": goal}]
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in TOOLS]
        for _ in range(MAX_STEPS):
            try:
                r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=120,
                                  headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                                  json={"model": LLM_MODEL, "messages": msgs, "tools": tools}).json()
                m = r["choices"][0]["message"]
            except Exception:
                break
            msgs.append(m)
            calls = m.get("tool_calls") or []
            if not calls:
                if not self.saved:  # model stopped (or wrote a tool call as text) without saving: safety net
                    self._plan(skip_search=False)
                    self.summary = "LLM stopped early; deterministic plan used. " + self.summary
                    return
                self.summary = self.summary or (m.get("content") or "")
                return
            for c in calls:
                args = c["function"].get("arguments") or "{}"
                res = self.call(c["function"]["name"], json.loads(args) if isinstance(args, str) else args)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res)})
            if any(c["function"]["name"] == "finish" for c in calls):
                return
        self._plan(skip_search=bool(self.pool))

    def _plan(self, skip_search=False, research_limit=5):
        """Deterministic agent: same tools, fixed policy. Used with no LLM or as a safety net."""
        notes = []
        if not skip_search:
            titles = self.rules["profile"]["titles"]
            for src, kw in (("ats", None), ("jobspy", titles[:4]), ("jobspy", titles[4:8]), ("remotive", titles)):
                res = self.call("search_jobs", {"source": src, "keywords": kw, "hours": self.rules["profile"]["recency_hours"]})
                if not res.get("fetched"):
                    notes.append(f"{src}: 0 fetched")
            for co in sorted({j["company"] for j in self.pool.values() if j["gate"] == "company_unverified"})[:research_limit]:
                info = self.call("research_company_h1b", {"company": co})
                if info.get("suggested_signal") in ("Strong", "Moderate", "Risk", "Unverified") and not info.get("cached") and not info.get("error"):
                    self.call("set_company_signal", {"company": co, "signal": info["suggested_signal"], "basis": "h1bdata.info"})
        ids = [i for i, j in self.pool.items() if j["gate"] == "pass"]
        self.call("score_jobs", {"ids": ids})
        keep = [i for i in ids if not self.resume or self.pool[i].get("fit") is None or self.pool[i]["fit"] >= self.min_fit]
        self.call("save_leads", {"ids": keep})
        if not self.summary:
            self.summary = f"{len(keep)} leads saved from {len(self.pool)} postings. " + ("; ".join(notes) if notes else "")

    def result(self):
        new = store.mark_seen([j["url"] for j in self.saved], self.user_id)
        for j in self.saved:
            j["is_new"] = j["url"] in new
        leads = sorted(self.saved, key=lambda j: (not j["is_new"], -(j.get("score") or 0), -(j.get("fit") or 0), j.get("geo_rank", 9)))
        gates = {}
        for j in self.pool.values():
            gates[j["gate"]] = gates.get(j["gate"], 0) + 1
        return {"summary": self.summary, "leads": leads, "new_count": len(new),
                "stats": {"scanned": len(self.pool), "gates": gates, "researched": self.overrides},
                "trace": self.trace, "provider": PROVIDER}
