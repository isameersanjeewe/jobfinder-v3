# ── RULES SERVICE ────────────────────────────────────────────────────────────
# Applies the user's profile rules to normalized jobs.
# Order: title -> geo -> recency -> posting visa language -> staffing firm -> company H1B signal.
# Company-level LCA signal beats keyword scanning.
#
# Profile resolution (first found wins): env RULES_JSON (the JSON itself, e.g. a CI secret) ->
# env RULES_PATH -> backend/rules.json (personal, gitignored) -> backend/rules.example.json (committed template).

import hashlib, json, os, re
from datetime import datetime, timedelta, timezone
import data_repo

_HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.getenv("RULES_PATH") or next(
    (p for p in (os.path.join(_HERE, "rules.json"), os.path.join(_HERE, "rules.example.json")) if os.path.exists(p)),
    os.path.join(_HERE, "rules.example.json"))


RULES_LOCAL_PATH = os.getenv("RULES_LOCAL_PATH", os.path.join(os.path.dirname(__file__), "rules.local.json"))
_TIER = {"Strong": "strong", "Moderate": "moderate", "Risk": "risk"}
# Roadmap item 6: the shared, growing H1B company-signal database. Locally this is the same
# rules.local.json "company_signals" map that already existed (one file per deployment -- already
# shared across users today since RULES_LOCAL_PATH is a single global path, not per-user). The gap
# this closes is durability: Render's free web service has no persistent disk, so that file is wiped
# every redeploy. write-through/read-through to the private data repo's data/_company_signals.json
# (same pattern as store.py's data/_users.json) makes it durable and shared across redeploys/instances.
COMPANY_SIGNALS_REPO_PATH = "data/_company_signals.json"
# Roadmap item 5: a company-level auto-reject flag, separate from the LCA-tier company_signal above --
# sourced from how many of a company's OWN postings explicitly said no sponsorship, not one posting's
# language. Threshold overridable for testing/tuning; not exposed in the profile (company-wide signal).
AUTO_REJECT_THRESHOLD = int(os.getenv("AUTO_REJECT_THRESHOLD", "2"))


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
    owner = os.getenv("OWNER_USER_ID", "").strip()
    if saved is not None:
        rules = saved
    elif user_id and owner and user_id != owner and path == RULES_PATH:
        # RULES_JSON / rules.json is the OWNER's private profile: never hand it to another user (it would leak
        # their titles, places and visa status, and search with them). Others start neutral until they save filters.
        rules = starter_profile()
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


def starter_profile() -> dict:
    """The committed example profile with no places (= anywhere + remote): a neutral start for a new user."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.example.json")) as f:
        rules = json.load(f)
    p = rules["profile"]
    p["locations"], p["remote_ok"] = [], True
    p["geo_pass_terms"], p["geo_rank"] = _geo_from_locations([], True)
    rules["starter"] = True
    return rules


def _atomic_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _write_through_company_signals(signals: dict) -> None:
    """Best-effort durability for the shared company-signal database (roadmap item 6): mirrors the full
    map into the private data repo at data/_company_signals.json, same pattern as store.py's
    data/_users.json. No-op when data_repo isn't configured (local-only, exactly today's behavior);
    data_repo.write_json already swallows/logs its own errors, so a flaky GitHub API call never breaks
    a run -- the local file (the source apply_gate/research actually read from) is already saved by then."""
    if data_repo.enabled():
        data_repo.write_json(COMPANY_SIGNALS_REPO_PATH, signals, "Update company signals")


def save_company_signal(company: str, signal: str, basis: str = "", local_path: str | None = None) -> dict:
    """Persist one research result so it's never researched twice. Atomic local write, merged (not
    replaced) so an existing no_sponsorship_count/auto_reject (see record_no_sponsorship below) for the
    same company isn't lost, then best-effort write-through to the shared private-repo store."""
    path = local_path or RULES_LOCAL_PATH
    data = _read_local(path)
    entry = data.setdefault("company_signals", {}).setdefault(company.lower(), {})
    entry.update(signal=signal, basis=basis, date=datetime.now().strftime("%Y-%m-%d"))
    _atomic_write(path, data)
    _write_through_company_signals(data["company_signals"])
    return entry


def record_no_sponsorship(company: str, count: int = 1, local_path: str | None = None) -> dict:
    """Accumulates explicit no-sponsorship posting detections per company (roadmap item 5) -- separate
    from the LCA-tier company_signal: a company flips to auto-reject once enough of ITS OWN postings
    said no, independent of this one posting's language or the company's filing volume. Lives in the
    same shared, durable store as save_company_signal() (see COMPANY_SIGNALS_REPO_PATH)."""
    path = local_path or RULES_LOCAL_PATH
    data = _read_local(path)
    entry = _bump_no_sponsorship(data, company, count=count)
    _atomic_write(path, data)
    _write_through_company_signals(data["company_signals"])
    return entry


NO_SPONSORSHIP_URLS_KEPT = 50  # per company; bounded so the shared JSON can't grow without limit


def _bump_no_sponsorship(data: dict, company: str, count: int = 0, urls=None) -> dict:
    """Mutates `data` in place. With `urls`, counts only postings never counted before (stored as short
    hashes), so the same still-open posting seen on every daily run counts once, not once per run."""
    entry = data.setdefault("company_signals", {}).setdefault(company.lower(), {})
    if urls is not None:
        known = list(entry.get("no_sponsorship_urls", []))
        fresh = {hashlib.sha256(u.encode("utf-8")).hexdigest()[:16] for u in urls if u} - set(known)
        count = len(fresh)
        entry["no_sponsorship_urls"] = (known + sorted(fresh))[-NO_SPONSORSHIP_URLS_KEPT:]
    entry["no_sponsorship_count"] = entry.get("no_sponsorship_count", 0) + count
    entry["auto_reject"] = entry["no_sponsorship_count"] >= AUTO_REJECT_THRESHOLD
    entry.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    return entry


def record_no_sponsorship_hits(hits: dict, local_path: str | None = None) -> dict:
    """Batch form used at run end: {company: {posting urls}} -> one local write and ONE write-through to
    the data repo for the whole run (was one GitHub commit per company). URL-deduped across runs."""
    hits = {co: urls for co, urls in hits.items() if co and urls}
    if not hits:
        return {}
    path = local_path or RULES_LOCAL_PATH
    data = _read_local(path)
    out = {co: _bump_no_sponsorship(data, co, urls=urls) for co, urls in hits.items()}
    _atomic_write(path, data)
    _write_through_company_signals(data["company_signals"])
    return out


def company_auto_reject(company: str, rules: dict) -> bool:
    """True if accumulated posting-level no-sponsorship detections (record_no_sponsorship) crossed
    AUTO_REJECT_THRESHOLD for this company. Read from rules["researched"], populated by load_rules()."""
    info = rules.get("researched", {}).get((company or "").lower())
    return bool(info and info.get("auto_reject"))


def sync_company_signals_from_repo(local_path: str | None = None) -> int:
    """Pulls the shared company-signals store from the private data repo into the local rules.local.json
    cache, mirroring store.sync_users_from_repo(). Call once at backend startup so a freshly redeployed
    Render instance (local disk wiped) doesn't start company research/auto-reject cold -- "suggest from
    what we already know" instead of researching every company fresh again. No-op (returns 0) if
    data_repo isn't configured, or the shared file doesn't exist yet."""
    if not data_repo.enabled():
        return 0
    remote = data_repo.read_json(COMPANY_SIGNALS_REPO_PATH)
    if not remote:
        return 0
    path = local_path or RULES_LOCAL_PATH
    data = _read_local(path)
    data.setdefault("company_signals", {}).update(remote)  # repo is the shared source of truth
    _atomic_write(path, data)
    return len(remote)


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
            "industries": industries, "exclude_companies": [], "starter": bool(rules.get("starter"))}


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


_ABBR_RX = re.compile(r"(?:^|[,;/|(-]\s*|\s)([A-Z]{2})(?=\s*(?:$|[,;/|)-]|\d{5}))")   # "Austin, TX" / "Austin TX" / "TX, USA"
_ABBR_LOWER_RX = re.compile(r",\s*([a-z]{2})(?=\s*(?:$|[,;/|)-]|\d{5}))")                 # "austin, tx"


def named_states(location: str) -> set[str]:
    """US state abbreviations a location names: "TX, USA", "Austin, TX 78701", "Remote - Texas".
    Abbreviations count only as an upper-case token between separators ("IN" in "Remote IN TEAM" doesn't)."""
    found = {m.group(1).lower() for m in _ABBR_RX.finditer(location or "") if m.group(1).lower() in US_STATES}
    low = (location or "").lower()
    found |= {m.group(1) for m in _ABBR_LOWER_RX.finditer(low) if m.group(1) in US_STATES}
    found |= {a for a, name in US_STATES.items() if re.search(rf"\b{name}\b", low)}
    return found


def _profile_states(rules: dict) -> set[str]:
    terms = rules["profile"].get("geo_pass_terms", [])
    return {t[2:] for t in terms if t.startswith(", ")} | {_STATE_BY_NAME[t] for t in terms if t in _STATE_BY_NAME}


def geo_ok(location: str, rules: dict, is_remote: bool = False, remote_trusted: bool = False) -> bool:
    """Only places the user picked, plus truly remote roles when remote_ok. `remote_trusted` = the remote flag
    comes from the employer's own board (Greenhouse/Lever/Ashby/Workday), which lists HQ cities on real remote
    roles. An aggregator's flag (JobSpy) is NOT trusted when the location names another state: "TX, USA"
    flagged remote was how unwanted locations leaked in."""
    loc = (location or "").lower()
    terms = rules["profile"]["geo_pass_terms"]
    places = [t for t in terms if t != "remote"]
    if places and any(re.search(_term_rx(t), loc) for t in places):
        return True
    states = named_states(location)
    if states and states & _profile_states(rules):
        return True  # "NJ, USA" (abbreviation without a leading comma) is still the user's state
    if rules["profile"]["remote_ok"] and (is_remote or "remote" in loc):
        if "remote" in loc or remote_trusted or not states:
            return True
        return not places  # untrusted remote flag + another named state: only OK when no places are set
    if not loc or loc in ("remote / not specified", "n/a"):
        return True  # unknown -> let the agent/LLM decide, don't silently drop
    if loc.strip(" .,") in rules["profile"].get("geo_country_only", []):
        return True  # country-only ("United States") = remote/unspecified; "Irvine, CA, United States" is not
    return not places  # no places set = anywhere


def remote_trusted(job: dict) -> bool:
    """Employer boards (ATS/Greenhouse|Lever|Ashby|Workday) and the remote-only Remotive board. Aggregators and
    unknown sources (incl. kits saved before the source was recorded) are not trusted."""
    return bool(job.get("is_remote")) and str(job.get("source", "")).startswith(("ATS/", "Remotive"))


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
    elif not geo_ok(job.get("location", ""), rules, job.get("is_remote", False), remote_trusted(job)):
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
        if company_auto_reject(job.get("company", ""), rules):
            reason = "company_auto_reject"  # accumulated no-sponsorship history at this company (item 5)
        elif job["posting_visa"] == "No" or (job["posting_visa"] == "Transfer" and rules["visa"].get("mode") != "h1b_transfer"):
            reason = "posting_no_sponsorship"  # "Transfer" only helps profiles that already hold an H-1B
        elif job["posting_visa"] != "Yes" and is_staffing(job.get("company", ""), rules):
            reason = "staffing_c2c_only"  # staffing firms need role-specific sponsorship on the posting for FT
        elif job["company_h1b"] in ("Excluded", "Risk"):
            reason = f"company_{job['company_h1b'].lower()}"
        elif rules["visa"]["require_company_confirmed"] and job["company_h1b"] == "Unverified":
            reason = "company_unverified"  # needs research -> agent tool check_h1b
    job["gate"] = reason or "pass"
    return job


# ── Sponsorship-question answer bank (roadmap item 4) ────────────────────────
# A consistent, pre-written answer to the near-universal ATS screening question ("Will you now or in
# the future require sponsorship to work in the US?"), keyed to the user's visa mode -- not improvised
# per application. Free, no LLM. A profile can override the wording via visa.sponsorship_answer.
SPONSORSHIP_ANSWERS = {
    "new_sponsorship": "Yes, I will require employer sponsorship for a work visa (H-1B) now or in the future.",
    "h1b_transfer": "I currently hold a valid H-1B visa. I would not need a new H-1B filed -- my employer "
                    "would need to file an H-1B transfer (change of employer) petition.",
}


def sponsorship_answer(rules: dict) -> str | None:
    """None when the user doesn't need sponsorship at all (visa.enabled=false); otherwise the canned
    answer for their visa.mode, or their own override text if they set visa.sponsorship_answer."""
    v = rules.get("visa", {})
    if not v.get("enabled", True):
        return None
    override = (v.get("sponsorship_answer") or "").strip()
    if override:
        return override
    return SPONSORSHIP_ANSWERS.get(v.get("mode", "new_sponsorship"), SPONSORSHIP_ANSWERS["new_sponsorship"])
