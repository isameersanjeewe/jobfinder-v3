# ── FIT SERVICE (free, no LLM) ───────────────────────────────────────────────
# ATS-style keyword coverage, same idea as Resume-Matcher (github.com/srbhr/Resume-Matcher):
# extract skills from the JD, check which appear in the resume, penalize seniority gaps.
# LLM scoring (ai_service / agent) is optional on top of this.

import re

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


def score_lead(job: dict, rules: dict) -> dict:
    """Default 0-100 lead score (docs/CONTEXT.md): H1B 35 / Title 25 / Geo 15 / Industry 15 / Contact 10.
    Uses gate annotations (company_h1b, posting_visa) when present. Hot Lead = score >= hot_threshold.
    If the profile has visa.enabled=false, the H1B factor is not a constraint and gets full points."""
    from rules_service import company_signal, geo_ok, title_ok
    sc = rules["scoring"]
    text = f"{job.get('company','')} {job.get('title','')} {job.get('description','')}".lower()
    title = (job.get("title") or "").lower()

    sig = job.get("company_h1b") or company_signal(job.get("company", ""), rules)
    if not rules["visa"].get("enabled", True):
        h1b = max(sc["h1b_points"].values())
    else:
        h1b = 0 if job.get("posting_visa") == "No" else sc["h1b_points"].get(sig, 0)

    primary = [t.lower() for t in rules["profile"]["titles"] if "program" not in t.lower()]
    if any(t in title for t in primary) or any(t in title for t in ("product manager", "product owner")):
        title_pts = 25
    elif any(t in title for t in sc["secondary_title_terms"]):
        title_pts = 15
    elif title_ok(title, rules, include_fallback=True):
        title_pts = 5  # stretch: passes the gate but isn't a core PM/PO/program title
    else:
        title_pts = 0

    geo = 15 if geo_ok(job.get("location", ""), rules, job.get("is_remote", False)) else 0

    ind = sc["opportunistic_points"]
    for tier in sc["industry_priority"]:
        if any(_has(t, text) for t in tier["terms"]):
            ind = tier["points"]
            break
    ov = sc["regulated_ai_override"]
    if any(_has(t, text) for t in ov["ai_terms"]) and any(_has(t, text) for t in ov["regulated_terms"]):
        ind = 15  # GenAI + regulated JD = strong match regardless of industry

    contact = sc["contact_points"].get(job.get("contact_type") or "", 0)

    score = h1b + title_pts + geo + ind + contact
    return {"score": score, "hot_lead": score >= sc["hot_threshold"],
            "score_parts": {"h1b": h1b, "title": title_pts, "geo": geo, "industry": ind, "contact": contact}}
