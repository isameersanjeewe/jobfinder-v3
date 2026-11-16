# ── RULES SERVICE ────────────────────────────────────────────────────────────
# Applies the user's profile rules to normalized jobs.
# Order: title -> geo -> recency -> posting visa language -> staffing firm -> company H1B signal.
# Company-level LCA signal beats keyword scanning.
#
# Profile resolution (first found wins): env RULES_JSON (the JSON itself, e.g. a CI secret) ->
# env RULES_PATH -> backend/rules.json (personal, gitignored) -> backend/rules.example.json (committed template).

import json, os, re
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.getenv("RULES_PATH") or next(
    (p for p in (os.path.join(_HERE, "rules.json"), os.path.join(_HERE, "rules.example.json")) if os.path.exists(p)),
    os.path.join(_HERE, "rules.example.json"))


RULES_LOCAL_PATH = os.getenv("RULES_LOCAL_PATH", os.path.join(os.path.dirname(__file__), "rules.local.json"))
_TIER = {"Strong": "strong", "Moderate": "moderate", "Risk": "risk"}


def _read_local(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_rules(path: str = RULES_PATH, local_path: str | None = None) -> dict:
    """Profile rules + per-user rules.local.json (persisted H1B research). Research never overrides
    a company the user already tiered by hand (manual lists win)."""
    if os.getenv("RULES_JSON") and path == RULES_PATH:
        rules = json.loads(os.environ["RULES_JSON"])
    else:
        with open(path) as f:
            rules = json.load(f)
    researched = _read_local(local_path or RULES_LOCAL_PATH).get("company_signals", {})
    rules["researched"] = researched
    for co, info in researched.items():
        tier = _TIER.get(info.get("signal"))
        if tier and company_signal(co, rules) == "Unverified":
            rules["companies"].setdefault(tier, []).append(co)
    return rules


def save_company_signal(company: str, signal: str, basis: str = "", local_path: str | None = None) -> dict:
    """Persist one research result so it's never researched twice. Atomic write."""
    path = local_path or RULES_LOCAL_PATH
    data = _read_local(path)
    entry = {"signal": signal, "basis": basis, "date": datetime.now().strftime("%Y-%m-%d")}
    data.setdefault("company_signals", {})[company.lower()] = entry
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return entry


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9& ]+", " ", (s or "").lower()).strip()


def company_signal(company: str, rules: dict) -> str:
    """Strong | Moderate | Risk | Excluded | Unverified. Exclusions/risk checked first (manual overrides win)."""
    c = _norm(company)
    if not c:
        return "Unverified"
    tiers = rules["companies"]
    for tier, label in (("excluded", "Excluded"), ("risk", "Risk"), ("strong", "Strong"), ("moderate", "Moderate")):
        for name in tiers.get(tier, []):
            n = _norm(name)
            if c == n or c.startswith(n + " ") or f" {n} " in f" {c} ":
                return label
    return "Unverified"


def is_staffing(company: str, rules: dict) -> bool:
    c = _norm(company)
    return bool(c) and any(c == (n := _norm(s)) or c.startswith(n + " ") or f" {n} " in f" {c} "
                           for s in rules["companies"].get("staffing", []))


def posting_visa_flag(text: str, rules: dict) -> str:
    """'No' if posting language blocks sponsorship, 'Yes' if it states sponsorship, else 'Unknown'."""
    t = (text or "").lower()
    if any(p in t for p in rules["visa"]["posting_no_patterns"]):
        return "No"
    if any(p in t for p in rules["visa"]["posting_yes_patterns"]):
        return "Yes"
    return "Unknown"


def title_ok(title: str, rules: dict, include_fallback: bool = False) -> bool:
    t = (title or "").lower()
    p = rules["profile"]
    if any(w in t for w in p["exclude_title_words"]):
        return False
    titles = p["titles"] + (p["fallback_titles"] if include_fallback else [])
    core = ["product manager", "product owner", "program manager", "product lead"]
    if include_fallback:
        core += ["project manager", "program lead"]
    return any(x.lower() in t for x in titles) or any(x in t for x in core)


def geo_ok(location: str, rules: dict, is_remote: bool = False) -> bool:
    loc = (location or "").lower()
    if is_remote and rules["profile"]["remote_ok"]:
        return True
    if not loc or loc in ("remote / not specified", "n/a"):
        return True  # unknown -> let the agent/LLM decide, don't silently drop
    if loc.strip(" .,") in rules["profile"].get("geo_country_only", []):
        return True  # country-only ("United States") = remote/unspecified; "Irvine, CA, United States" is not
    return any(re.search(rf"\b{re.escape(term)}\b", loc) for term in rules["profile"]["geo_pass_terms"])


def geo_rank(location: str, rules: dict) -> int:
    """Lower = preferred, in the order of profile.geo_rank buckets."""
    loc = (location or "").lower()
    for i, bucket in enumerate(rules["profile"]["geo_rank"]):
        if any(re.search(rf"\b{re.escape(t)}\b", loc) for t in bucket):
            return i
    return len(rules["profile"]["geo_rank"])


def _parse_date(s: str):
    if not s or s == "N/A":
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt)
        except ValueError:
            continue
    return None


def recency_ok(posted: str, hours: int) -> bool:
    """Strict: unknown date fails (a canonical posted date is required)."""
    if not hours:
        return True
    d = _parse_date(posted)
    if d is None:
        return False
    # Date-only values: compare at day granularity so a 24h window keeps today + yesterday
    cutoff = datetime.now() - timedelta(hours=hours)
    return d >= cutoff.replace(hour=0, minute=0, second=0, microsecond=0)


def apply_gate(job: dict, rules: dict, hours: int, track: str = "ft") -> dict:
    """Annotates job with gate results. job['gate'] = 'pass' | reason string."""
    text = f"{job.get('title','')} {job.get('description','')}"
    job["company_h1b"] = company_signal(job.get("company", ""), rules)
    job["posting_visa"] = posting_visa_flag(text, rules)
    job["geo_rank"] = geo_rank(job.get("location", ""), rules)

    reason = None
    if not title_ok(job.get("title", ""), rules, include_fallback=(track == "c2c")):
        reason = "title"
    elif not geo_ok(job.get("location", ""), rules, job.get("is_remote", False)):
        reason = "geography"
    elif not recency_ok(job.get("posted", ""), hours):
        reason = "stale_or_undated"
    elif not rules["visa"].get("enabled", True):
        pass  # user doesn't need sponsorship: no visa/company gates
    elif track == "c2c":
        if any(p in text.lower() for p in rules["contract_track"]["exclude_patterns"]):
            reason = "c2c_visa_block"
    else:
        if job["posting_visa"] == "No":
            reason = "posting_no_sponsorship"
        elif job["posting_visa"] != "Yes" and is_staffing(job.get("company", ""), rules):
            reason = "staffing_c2c_only"  # staffing firms need role-specific sponsorship on the posting for FT
        elif job["company_h1b"] in ("Excluded", "Risk"):
            reason = f"company_{job['company_h1b'].lower()}"
        elif rules["visa"]["require_company_confirmed"] and job["company_h1b"] == "Unverified":
            reason = "company_unverified"  # needs research -> agent tool check_h1b
    job["gate"] = reason or "pass"
    return job
