# ── FIT SERVICE ───────────────────────────────────────────────────────────────
# ATS-style keyword coverage, same idea as Resume-Matcher (github.com/srbhr/Resume-Matcher):
# extract skills from the JD, check which appear in the resume, penalize seniority gaps.
# This is keyword_fit() -- free, no LLM, and always the fallback (see llm_gap_fit below for why it's
# "fixed" by roadmap item 2: a small fixed term list makes one match/miss swing the score 33-50 points).
#
# llm_gap_fit() (roadmap item 2): an optional LLM JD-vs-resume gap read for top leads only, via a BYO
# key (same LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL pattern documented in agent_service.py's
# header -- no new provider abstraction). Never required: no key configured -> llm_configured() is
# False -> callers use keyword_fit() exactly as before. Recommended free provider: Groq + an
# open-source Llama model (LLM_PROVIDER=openai, LLM_BASE_URL=https://api.groq.com/openai/v1).

import json, os, re
import requests

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_FIT_TIMEOUT = int(os.getenv("LLM_FIT_TIMEOUT", "20"))

SKILLS = [
    "product strategy", "roadmap", "okr", "kpi", "stakeholder", "agile", "scrum", "safe", "jira", "confluence",
    "user research", "a/b test", "experimentation", "go-to-market", "prd", "requirements", "backlog",
    "sql", "python", "tableau", "looker", "power bi", "snowflake", "databricks", "dbt", "airflow", "spark",
    "aws", "azure", "gcp", "api", "data platform", "data governance", "data quality", "data pipeline",
    "data product", "analytics", "etl", "master data", "metadata", "knowledge graph", "ontology",
    "machine learning", "ml", "genai", "generative ai", "llm", "rag", "agentic", "prompt", "nlp",
    "model risk", "evaluation", "mlops", "personalization", "recommendation",
    "healthcare", "pharma", "life sciences", "clinical", "hipaa", "gxp", "fda", "payer", "claims",
    "ehr", "emr", "fhir", "hl7", "regulated", "compliance", "saas", "b2b", "platform", "multi-tenant",
    "iot", "fintech", "insurance", "ads", "advertising", "growth", "people management", "direct reports",
]


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None


def required_years(jd: str) -> int | None:
    m = re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years", jd.lower())
    return max(int(x) for x in m) if m else None


def keyword_fit(job: dict, resume_text: str, resume_years: int | None = None, boost_terms=None, penalty_terms=None) -> dict:
    jd = f"{job.get('title','')} {job.get('description','')}".lower()
    rs = (resume_text or "").lower()
    jd_skills = [s for s in SKILLS if _has(s, jd)]
    if not rs or not jd_skills:
        # No description text (common for board listings): unknown fit, not zero fit
        return {"fit": None, "matched": [], "missing": jd_skills[:8], "years_gap": None}
    matched = [s for s in jd_skills if _has(s, rs)]
    missing = [s for s in jd_skills if s not in matched]
    score = 100 * len(matched) / len(jd_skills)
    score += 5 * sum(1 for t in (boost_terms or []) if _has(t, jd))
    score -= 10 * sum(1 for t in (penalty_terms or []) if _has(t, jd) and not _has(t, rs))
    gap = None
    req = required_years(jd)
    if req and resume_years:
        gap = req - resume_years
        if gap > 0:
            score -= 8 * gap
    return {"fit": int(max(0, min(100, score))), "matched": matched, "missing": missing[:8], "years_gap": gap}


def llm_configured() -> bool:
    """True when a usable BYO LLM key is set (see agent_service.py's header for the config pattern).
    Every LLM-touching feature in this file/outreach.py checks this first and falls back to free,
    deterministic behavior when it's False -- an LLM is always optional, never required to function."""
    return (LLM_PROVIDER == "openai" and bool(LLM_API_KEY)) or (LLM_PROVIDER == "anthropic" and bool(ANTHROPIC_KEY))


_FIT_PROMPT = (
    "Compare this resume to this job description. Reply with ONLY compact JSON, no prose, no markdown "
    'fences: {{"fit": <0-100 integer, overall match>, "matched": [<up to 8 short requirement/skill phrases '
    'the resume clearly covers>], "missing": [<up to 5 short requirement/skill phrases the resume is '
    'missing or weak on>], "years_gap": <integer, required years of experience minus the resume\'s, or '
    "null if not stated>}}\n\nJOB TITLE: {title}\nJOB DESCRIPTION:\n{jd}\n\nRESUME:\n{resume}"
)


def llm_gap_fit(job: dict, resume_text: str) -> dict | None:
    """LLM JD-vs-resume gap analysis (roadmap item 2): a real read of what's covered and what's missing,
    instead of keyword_fit's blunt percentage-of-a-120-fixed-term-list match. Returns None -- the caller
    then keeps whatever keyword_fit already computed -- when no BYO key is configured, there's no JD
    text to compare, or the call/response fails for any reason. Never raises."""
    if not llm_configured() or not resume_text or not (job.get("description") or "").strip():
        return None
    prompt = _FIT_PROMPT.format(title=job.get("title", ""), jd=(job.get("description") or "")[:4000],
                                 resume=resume_text[:4000])
    try:
        if LLM_PROVIDER == "anthropic":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=LLM_FIT_TIMEOUT, headers={
                "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": LLM_MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        else:
            r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=LLM_FIT_TIMEOUT,
                               headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                               json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        fit = int(max(0, min(100, float(data.get("fit", 0)))))
        matched = [str(x)[:60] for x in (data.get("matched") or [])][:8]
        missing = [str(x)[:60] for x in (data.get("missing") or [])][:5]
        yg = data.get("years_gap")
        years_gap = int(yg) if isinstance(yg, (int, float)) and not isinstance(yg, bool) else None
        return {"fit": fit, "matched": matched, "missing": missing, "years_gap": years_gap, "fit_source": "llm"}
    except Exception:
        return None


def feedback_bias(job: dict, signals: dict | None) -> int:
    """Roadmap item 1: wires thumbs up/down (/feedback) into the score Full Search actually uses.
    `signals` = store.feedback_signal_counts(user_id): accumulated {"company": {name: {up,down}},
    "title": {pattern: {up,down}}}. +3/-3 per net company signal, +2/-2 per net title-pattern signal
    (seniority-stripped, same normalization as rules_service.search_queries), clamped to +/-10 so a
    couple of clicks nudge future ranking without overwhelming the other 0-100 factors."""
    if not signals:
        return 0
    from rules_service import _SENIORITY
    co = (job.get("company") or "").strip().lower()
    co_counts = signals.get("company", {}).get(co, {})
    co_net = co_counts.get("up", 0) - co_counts.get("down", 0)
    tp = _SENIORITY.sub("", (job.get("title") or "").strip().lower())
    tp_counts = signals.get("title", {}).get(tp, {})
    tp_net = tp_counts.get("up", 0) - tp_counts.get("down", 0)
    return max(-10, min(10, co_net * 3 + tp_net * 2))


def score_lead(job: dict, rules: dict, feedback_signals: dict | None = None) -> dict:
    """Default 0-100 lead score (docs/CONTEXT.md): H1B 35 / Title 25 / Geo 15 / Industry 15 / Contact 10,
    plus an optional feedback bias (roadmap item 1, see feedback_bias() above). Uses gate annotations
    (company_h1b, posting_visa) when present. Hot Lead = score >= hot_threshold. If the profile has
    visa.enabled=false, the H1B factor is not a constraint and gets full points."""
    from rules_service import company_signal, geo_ok, remote_trusted, title_ok
    sc = rules["scoring"]
    text = f"{job.get('company','')} {job.get('title','')} {job.get('description','')}".lower()
    title = (job.get("title") or "").lower()

    sig = job.get("company_h1b") or company_signal(job.get("company", ""), rules)
    if not rules["visa"].get("enabled", True):
        h1b = max(sc["h1b_points"].values())
    else:
        h1b = 0 if job.get("posting_visa") == "No" else sc["h1b_points"].get(sig, 0)

    secondary = [s.lower() for s in sc.get("secondary_title_terms", [])]
    # primary = the user's own titles; core_title_terms only widen the gate (they score as stretch)
    primary = [t.lower() for t in rules["profile"]["titles"] if not any(s in t.lower() for s in secondary)]
    if any(t in title for t in primary):
        title_pts = 25
    elif any(t in title for t in secondary):
        title_pts = 15
    elif title_ok(title, rules, include_fallback=True):
        title_pts = 5  # stretch: passes the gate but isn't a core PM/PO/program title
    else:
        title_pts = 0

    geo = 15 if geo_ok(job.get("location", ""), rules, job.get("is_remote", False), remote_trusted(job)) else 0

    ind = sc["opportunistic_points"]
    for tier in sc["industry_priority"]:
        if any(_has(t, text) for t in tier["terms"]):
            ind = tier["points"]
            break
    ov = sc["regulated_ai_override"]
    if any(_has(t, text) for t in ov["ai_terms"]) and any(_has(t, text) for t in ov["regulated_terms"]):
        ind = 15  # GenAI + regulated JD = strong match regardless of industry

    contact = sc["contact_points"].get(job.get("contact_type") or "", 0)
    fb = feedback_bias(job, feedback_signals)

    score = max(0, min(100, h1b + title_pts + geo + ind + contact + fb))
    return {"score": score, "hot_lead": score >= sc["hot_threshold"],
            "score_parts": {"h1b": h1b, "title": title_pts, "geo": geo, "industry": ind, "contact": contact, "feedback": fb}}
