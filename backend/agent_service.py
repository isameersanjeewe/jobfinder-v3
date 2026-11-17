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

import datetime, json, os, re, requests
from scraper_service import fetch_remotive
from free_sources import fetch_jobspy, fetch_ats
from rules_service import load_rules, apply_gate, save_company_signal, search_queries
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
Jobs gated 'staffing_c2c_only' are staffing-firm postings: save them with track='c2c' (the contract track re-checks them).
If a source errors or returns 0, note it and move on. Be economical: few, targeted calls."""


_CO_STOP = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "services", "systems", "group",
            "holdings", "the", "com", "plc", "lp", "usa", "us"}


def dedupe_key(job: dict) -> tuple:
    """Same posting from several boards (Indeed/Glassdoor/LinkedIn/employer site) has different URLs.
    Key on normalized company + title + first location segment ("Amazon.com Services LLC" == "Amazon")."""
    co = " ".join(w for w in re.findall(r"[a-z0-9&]+", (job.get("company") or "").lower()) if w not in _CO_STOP)
    title = " ".join(re.findall(r"[a-z0-9]+", (job.get("title") or "").lower()))
    city = " ".join(re.findall(r"[a-z]+", re.split(r"[,;/|(]", (job.get("location") or "").lower())[0]))
    return co, title, city


def _days_apart(a, b) -> int:
    try:
        return abs((datetime.date.fromisoformat(str(a)[:10]) - datetime.date.fromisoformat(str(b)[:10])).days)
    except ValueError:
        return 99


class AgentRun:
    def __init__(self, resume_text: str = "", user_id: str = "default", rules: dict | None = None,
                 min_fit: int = 40, resume_years: int | None = None):
        self.rules = rules or load_rules()
        self.resume = resume_text
        self.user_id = user_id
        self.min_fit = min_fit
        self.resume_years = resume_years
        self.pool: dict[str, dict] = {}
        self._urls: set[str] = set()
        self._keys: dict[tuple, str] = {}
        self._title_idx: dict[tuple, list[str]] = {}   # (company, title) -> ids, for remote multi-hub listings
        self.duplicates = 0
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
            jobs = fetch_ats(self.rules["ats_boards"], queries=search_queries(self.rules))
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
            if j["url"] in self._urls:
                continue
            key = dedupe_key(j)
            dup_id = self._keys.get(key) or self._remote_twin(j, key)
            if dup_id:  # cross-board duplicate: keep the first copy (employer boards are searched first)
                kept = self.pool[dup_id]
                self._urls.add(j["url"])
                self.duplicates += 1
                counts["duplicate"] = counts.get("duplicate", 0) + 1
                if j.get("description") and j["description"] not in (kept.get("description") or ""):
                    # merge text so a "no sponsorship" line on any copy still blocks the lead
                    kept["description"] = f"{kept.get('description') or ''}\n{j['description']}".strip()
                    self._gate(kept, kept.get("hours", hours), kept.get("track", track))
                continue
            self._gate(j, hours, track)
            self._urls.add(j["url"])
            self._keys[key] = j["id"]
            self._title_idx.setdefault(key[:2], []).append(j["id"])
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

    def _remote_twin(self, j, key):
        """Remote roles are listed under different hub cities per board ("RTP", "Durham", "Triangle"):
        same company + title, either copy remote, posted within a day -> same job."""
        for i in self._title_idx.get(key[:2], []):
            other = self.pool[i]
            if (j.get("is_remote") or other.get("is_remote")) and _days_apart(j.get("posted"), other.get("posted")) <= 1:
                return i
        return None

    def research_company_h1b(self, company):
        cached = self.rules.get("researched", {}).get(company.lower())
        if cached:  # persisted in rules.local.json by an earlier run; re-check when stale
            try:
                age = (datetime.date.today() - datetime.date.fromisoformat(cached.get("date", ""))).days
            except ValueError:
                age = 9999
            if age > (7 if cached.get("signal") == "Unverified" else 90):
                cached = None
        if cached:
            return {"company": company, "cached": True, "suggested_signal": cached["signal"],
                    "basis": cached.get("basis", ""), "researched_on": cached.get("date")}
        try:
            # One query per year: "all years" returns no table for large employers (it looked like 0 filings,
            # so big sponsors were recorded as Unverified). Last full year + current year = recent volume.
            fam = {t.upper() for t in self.rules["profile"]["titles"] + self.rules["profile"].get("core_title_terms", [])}
            this_year = datetime.date.today().year
            per_year, pm = {}, 0
            for year in (this_year - 1, this_year):
                r = requests.get("https://h1bdata.info/index.php", params={"em": company, "job": "", "city": "", "year": str(year)},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                r.raise_for_status()
                if "<table" not in r.text.lower():
                    # rate-limit/error page: don't record "0 filings" (it would be cached as Unverified forever)
                    raise ValueError("no results table in response")
                rows = re.findall(r"<tr>(.*?)</tr>", r.text, re.S)[1:]
                titles = [re.sub(r"<[^>]+>", "", re.findall(r"<td>(.*?)</td>", row, re.S)[1]).upper()
                          for row in rows if row.count("<td") > 3]
                per_year[year] = len(titles)
                pm += sum(1 for t in titles if any(f in t for f in fam))
            n = max(per_year.values())  # filings per year (current year is partial)
            # Thin volume (< ~10/yr) is Risk even with a same-title match; a same-title match lifts 10+ to Moderate.
            hint = "Strong" if n >= 100 else "Moderate" if n >= 20 or (pm and n >= 10) else "Risk" if n else "Unverified"
            return {"company": company, "lca_rows": n, "lca_by_year": per_year, "pm_family_rows": pm,
                    "latest_year": max((y for y, c in per_year.items() if c), default=None),
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
        if track == "c2c" and self.rules.get("contract_track", {}).get("enabled"):
            # contract track has its own gate (staffing vendors allowed, C2C visa patterns): re-gate before saving
            ct_hours = self.rules["contract_track"].get("recency_hours", 168)
            for i in ids:
                if i in self.pool and self.pool[i]["track"] != "c2c":
                    self.pool[i]["track"] = "c2c"
                    self._gate(self.pool[i], ct_hours, "c2c")
        saved_urls = {s["url"] for s in self.saved}
        jobs = [self.pool[i] for i in ids if i in self.pool and self.pool[i]["gate"] == "pass"
                and self.pool[i]["url"] not in saved_urls]
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
        v = self.rules["visa"]
        visa = ("Visa: the user needs employer H-1B sponsorship" +
                (" (already holds an H-1B: postings that accept transfers / change of employer are fine)" if v.get("mode") == "h1b_transfer" else "") +
                ("; company must be a Strong/Moderate sponsor" if v.get("require_company_confirmed", True) else "") +
                "; any posting saying no sponsorship/citizenship/clearance is out.") if v.get("enabled", True) else "Visa: no sponsorship needed."
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
            # research the most promising unverified companies first: best score each could reach if it sponsors
            best: dict[str, int] = {}
            for j in self.pool.values():
                if j["gate"] == "company_unverified":
                    s = score_lead(dict(j, company_h1b="Strong"), self.rules)["score"]
                    best[j["company"]] = max(best.get(j["company"], 0), s)
            looked_up = 0
            for co in sorted(best, key=lambda c: (-best[c], c)):
                if looked_up >= research_limit:
                    break
                info = self.call("research_company_h1b", {"company": co})
                if info.get("cached"):
                    continue  # free: answered from rules.local.json
                looked_up += 1
                if info.get("suggested_signal") in ("Strong", "Moderate", "Risk", "Unverified") and not info.get("error"):
                    self.call("set_company_signal", {"company": co, "signal": info["suggested_signal"], "basis": "h1bdata.info"})
        ids = [i for i, j in self.pool.items() if j["gate"] == "pass"]
        self.call("score_jobs", {"ids": ids})
        keep = [i for i in ids if not self.resume or self.pool[i].get("fit") is None or self.pool[i]["fit"] >= self.min_fit]
        self.call("save_leads", {"ids": keep})
        # staffing-firm jobs fail FT without posting-level sponsorship; route them to the contract track instead
        c2c = [i for i, j in self.pool.items() if j["gate"] == "staffing_c2c_only"]
        if c2c and self.rules.get("contract_track", {}).get("enabled"):
            self.call("save_leads", {"ids": c2c, "track": "c2c"})
        if not self.summary:
            n_c2c = sum(1 for j in self.saved if j.get("track") == "c2c")
            self.summary = (f"{len(keep)} leads saved from {len(self.pool)} postings"
                            + (f", plus {n_c2c} contract (C2C) leads" if n_c2c else "")
                            + (f"; {self.duplicates} cross-site duplicates merged" if self.duplicates else "") + ". "
                            + ("; ".join(notes) if notes else ""))

    def result(self):
        new = store.mark_seen([j["url"] for j in self.saved], self.user_id)
        for j in self.saved:
            j["is_new"] = j["url"] in new
        leads = sorted(self.saved, key=lambda j: (not j["is_new"], -(j.get("score") or 0), -(j.get("fit") or 0), j.get("geo_rank", 9)))
        gates = {}
        for j in self.pool.values():
            gates[j["gate"]] = gates.get(j["gate"], 0) + 1
        return {"summary": self.summary, "leads": leads, "new_count": len(new),
                "stats": {"scanned": len(self.pool), "duplicates_merged": self.duplicates, "gates": gates,
                          "researched": self.overrides,
                          "tracks": {t: sum(1 for j in self.saved if j.get("track") == t) for t in ("ft", "c2c")}},
                "trace": self.trace, "provider": PROVIDER}
