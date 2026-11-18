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


def load_rules(path: str = RULES_PATH, local_path: str | None = None, user_id: str | None = None) -> dict:
    """Profile rules + per-user rules.local.json (persisted H1B research). Research never overrides
    a company the user already tiered by hand (manual lists win).

    Multi-user: when `user_id` is given, an authenticated user's own saved profile (store.profiles,
    set via store.save_profile() -- no HTTP route yet, see CLAUDE.md Roadmap) is used as the base
    instead of the file-based one. A user with no saved
    profile yet falls back to the same file-based resolution as before (env RULES_JSON -> env
    RULES_PATH -> rules.json -> rules.example.json), so every new tester starts from the example
    profile with no setup required. See CLAUDE.md "Multi-user" section."""
    saved = None
    if user_id:
        import store  # local import: avoids a hard circular import at module load time
        saved = store.get_profile(user_id)
    if saved is not None:
        rules = saved
    elif os.getenv("RULES_JSON") and path == RULES_PATH:
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


# Phrasings the substring list misses. Applied for every profile (plus optional visa.posting_no_regex).
_NO_REGEX = [
    r"\b(not|unable to|cannot|can ?not|won't|will not|do not|does not|don't|doesn't|are not able to|is not able to)\s+"
    r"(currently\s+)?(offer|provide|support|sponsor)\w*\b[^.;]{0,60}?\b(visa|sponsorship|h-?1b|work authorization|cpt|opt)\b",
    r"\b(will not|won't|do not|does not|cannot|can ?not)\s+consider\s+(applicants|candidates|anyone|individuals)\b[^.;]{0,60}\bsponsorship",
    r"\bsponsorship\s+(is\s+|will\s+)?not\s+(be\s+)?(available|offered|provided|possible|supported)",
    r"\bsponsorship\b[^.;]{0,40}\b(is|will)\s+not\s+(be\s+)?(available|offered|provided|possible|supported)",
    r"\bnot\s+eligible\s+for\s+([\w-]+\s+){0,3}sponsorship",
    r"\b(must|required to)\s+be\s+(a\s+)?u\.?s\.?\s+persons?\b",
    r"\bno\s+(new\s+)?(visa\s+|h-?1b\s+(visa\s+)?|employment\s+|immigration\s+)?sponsorship",
    r"\b(require|need)s?\s+(\w+\s+){0,3}sponsorship[^.]{0,60}\b(not be considered|ineligible|not eligible|cannot be considered)",
]
# Citizenship words also appear in inclusive lists ("US citizens, green card and H1B holders welcome"):
# they only block when the posting does not also state sponsorship.
_SOFT_NO = {"us citizen", "u.s. citizen", "green card", "permanent resident"}
_CLEARANCE = {"security clearance", "clearance required"}
# "No new sponsorship, but change-of-employer (H-1B transfer) petitions considered" -> viable for H-1B holders.
_TRANSFER_OK = re.compile(r"change[- ]of[- ]employer|h-?1b\s+transfers?|transfers?\s+of\s+(an?\s+)?(existing\s+|current\s+)?h-?1b"
                          r"|transfer\s+(your|their|an?|existing)\s+(existing\s+)?(h-?1b|visa)")
_NEGATION_BEFORE = re.compile(r"\b(no|not|cannot|can't|unable|won't|will not|do not|does not)\b[^.;,]{0,30}$")
_NEGATION_AFTER = re.compile(r"^[^.;,]{0,20}\b(is|are|will be)\s+not\b|^[^.;,]{0,20}\bnot\s+(available|accepted|considered|supported)")
_NEGATED_CLEARANCE = re.compile(r"\b(no|not|without|n't)\b[^.]{0,25}clearance|clearance\s+(is\s+)?not\s+(required|needed)")


def _transfer_ok(t: str) -> bool:
    return any(not _NEGATION_BEFORE.search(t[max(0, m.start() - 40):m.start()]) and not _NEGATION_AFTER.search(t[m.end():m.end() + 40])
               for m in _TRANSFER_OK.finditer(t))


def posting_visa_flag(text: str, rules: dict) -> str:
    """'No' if posting language blocks sponsorship, 'Yes' if it states sponsorship, 'Transfer' if it refuses new
    sponsorship but accepts H-1B transfers (change of employer), else 'Unknown'."""
    t = re.sub(r"\s+", " ", (text or "").lower().replace("’", "'").replace("&nbsp;", " "))
    pats = rules["visa"]["posting_no_patterns"]
    yes = any(p in t for p in rules["visa"]["posting_yes_patterns"])
    hard = [p for p in pats if p not in _SOFT_NO and p not in _CLEARANCE]
    if any(p in t for p in pats if p in _CLEARANCE) and not _NEGATED_CLEARANCE.search(t):
        return "No"  # clearance roles are out even if transfers are mentioned
    if any(p in t for p in hard) or any(re.search(rx, t) for rx in _NO_REGEX + rules["visa"].get("posting_no_regex", [])):
        return "Transfer" if _transfer_ok(t) else "No"
    if not yes and any(p in t for p in pats if p in _SOFT_NO):
        return "No"
    return "Yes" if yes else "Unknown"


def title_ok(title: str, rules: dict, include_fallback: bool = False) -> bool:
    t = (title or "").lower()
    p = rules["profile"]
    if any(w in t for w in p["exclude_title_words"]):
        return False
    titles = p["titles"] + p.get("core_title_terms", []) + (p.get("fallback_titles", []) if include_fallback else [])
    return any(x.lower() in t for x in titles)


# ── In-app filters ───────────────────────────────────────────────────────────
# Any user can override the profile per run from the UI (stored only in their browser). Same shape both ways:
# profile_filters() -> what the UI shows by default; apply_filters() -> rules for one run.
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "dc": "district of columbia", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky",
    "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}
_STATE_BY_NAME = {v: k for k, v in US_STATES.items()}
VISA_OPTIONS = ("none", "new_sponsorship", "h1b_transfer")
_SENIORITY = re.compile(r"^(senior|sr\.?|staff|principal|lead|junior|jr\.?)\s+")


def _geo_from_locations(locations: list[str], remote_ok: bool) -> tuple[list[str], list[list[str]]]:
    """'Austin, TX' -> pass terms [austin, texas, ', tx'] and rank buckets [[cities], [states], [remote]].
    State abbreviations only match after a comma (', or' / ', in' would otherwise hit ordinary words)."""
    cities, states = [], []
    for loc in locations:
        parts = [s.strip().lower() for s in loc.split(",") if s.strip()]
        if not parts or parts[0] == "remote":
            continue
        for part in parts:
            abbr = part if part in US_STATES else _STATE_BY_NAME.get(part)
            if abbr:
                states += [US_STATES[abbr], f", {abbr}"]
            elif part not in ("usa", "us", "united states"):
                cities.append(part)
    cities, states = list(dict.fromkeys(cities)), list(dict.fromkeys(states))
    rank = [b for b in (cities, states) if b] + ([["remote"]] if remote_ok else [])
    return cities + states + (["remote"] if remote_ok else []), rank


def search_queries(rules: dict, limit: int = 4) -> list[str]:
    """Shortest distinct title phrases ('Senior AI Product Manager' is covered by 'product manager')."""
    base = [_SENIORITY.sub("", t.lower().strip()) for t in rules["profile"]["titles"]]
    base = sorted(dict.fromkeys(b for b in base if b), key=len)
    out = []
    for b in base:
        if not any(o in b for o in out):
            out.append(b)
    return out[:limit]


def profile_filters(rules: dict) -> dict:
    """The profile's values in the shape the UI edits."""
    p, v = rules["profile"], rules["visa"]
    industries = [t["terms"][0] for t in rules.get("scoring", {}).get("industry_priority", []) if t.get("terms")]
    return {"titles": p["titles"], "locations": p.get("locations", []), "remote_ok": bool(p.get("remote_ok")),
            "visa": v.get("mode", "new_sponsorship") if v.get("enabled", True) else "none",
            "sponsors_only": bool(v.get("require_company_confirmed", True)),
            "industries": industries, "exclude_companies": []}


def apply_filters(rules: dict, f: dict | None) -> dict:
    """Overlay one run's filters on the profile (in place; load_rules() returns a fresh dict per run).
    Keys left out or None keep the profile value."""
    if not f:
        return rules
    p, v, sc = rules["profile"], rules["visa"], rules.setdefault("scoring", {})
    if f.get("titles"):
        titles = [t.strip() for t in f["titles"] if t.strip()]
        low = " | ".join(t.lower() for t in titles)
        p["titles"] = titles
        p["core_title_terms"], p["fallback_titles"] = [], []  # the profile's role family no longer applies
        p["exclude_title_words"] = [w for w in p.get("exclude_title_words", []) if w not in low]
        sc["secondary_title_terms"] = [s for s in sc.get("secondary_title_terms", []) if s in low]
    if f.get("remote_ok") is not None:
        p["remote_ok"] = bool(f["remote_ok"])
    if f.get("locations") is not None:
        p["locations"] = [l.strip() for l in f["locations"] if l.strip()]
        p["geo_pass_terms"], p["geo_rank"] = _geo_from_locations(p["locations"], p.get("remote_ok", True))
    if f.get("visa") in VISA_OPTIONS:
        v["enabled"] = f["visa"] != "none"
        if v["enabled"]:
            v["mode"] = f["visa"]
    if f.get("sponsors_only") is not None:
        v["require_company_confirmed"] = bool(f["sponsors_only"])
    if f.get("industries"):
        terms = [t.strip().lower() for t in f["industries"] if t.strip()]
        sc["industry_priority"] = [{"points": max(5, 15 - 2 * i), "terms": [t]} for i, t in enumerate(terms)]
        rules.setdefault("industry_boost", {"terms": [], "penalty_terms": []})["terms"] = terms
    if f.get("exclude_companies"):
        rules["companies"].setdefault("excluded", []).extend(c.strip().lower() for c in f["exclude_companies"] if c.strip())
    return rules


def geo_ok(location: str, rules: dict, is_remote: bool = False) -> bool:
    loc = (location or "").lower()
    if is_remote and rules["profile"]["remote_ok"]:
        return True
    if not loc or loc in ("remote / not specified", "n/a"):
        return True  # unknown -> let the agent/LLM decide, don't silently drop
    if loc.strip(" .,") in rules["profile"].get("geo_country_only", []):
        return True  # country-only ("United States") = remote/unspecified; "Irvine, CA, United States" is not
    if not rules["profile"]["geo_pass_terms"]:
        return True  # no locations set = anywhere
    return any(re.search(_term_rx(term), loc) for term in rules["profile"]["geo_pass_terms"])


def _term_rx(term: str) -> str:
    # ", tx" starts with punctuation: a leading \b would need a word char before the comma's left side
    return (r"\b" if term[:1].isalnum() else "") + re.escape(term) + r"\b"


def geo_rank(location: str, rules: dict) -> int:
    """Lower = preferred, in the order of profile.geo_rank buckets."""
    loc = (location or "").lower()
    for i, bucket in enumerate(rules["profile"]["geo_rank"]):
        if any(re.search(_term_rx(t), loc) for t in bucket):
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
    elif (_d := _parse_date(job.get("posted", ""))) and _d > datetime.now() + timedelta(days=1):
        reason = "future_date"  # aggregator dates can be in the future: unusable as a posted date
    elif not recency_ok(job.get("posted", ""), hours):
        reason = "stale_or_undated"
    elif not rules["visa"].get("enabled", True):
        pass  # user doesn't need sponsorship: no visa/company gates
    elif track == "c2c":
        if any(p in text.lower() for p in rules["contract_track"]["exclude_patterns"]):
            reason = "c2c_visa_block"
    else:
        if job["posting_visa"] == "No" or (job["posting_visa"] == "Transfer" and rules["visa"].get("mode") != "h1b_transfer"):
            reason = "posting_no_sponsorship"  # "Transfer" only helps profiles that already hold an H-1B
        elif job["posting_visa"] != "Yes" and is_staffing(job.get("company", ""), rules):
            reason = "staffing_c2c_only"  # staffing firms need role-specific sponsorship on the posting for FT
        elif job["company_h1b"] in ("Excluded", "Risk"):
            reason = f"company_{job['company_h1b'].lower()}"
        elif rules["visa"]["require_company_confirmed"] and job["company_h1b"] == "Unverified":
            reason = "company_unverified"  # needs research -> agent tool check_h1b
    job["gate"] = reason or "pass"
    return job
