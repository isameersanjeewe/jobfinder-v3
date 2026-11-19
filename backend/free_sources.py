# ── FREE SOURCES ─────────────────────────────────────────────────────────────
# 100% free, no API keys:
#   1. JobSpy (MIT, github.com/speedyapply/JobSpy): Indeed, LinkedIn (guest), Google Jobs,
#      ZipRecruiter, Glassdoor. Indeed is the most reliable; LinkedIn rate-limits ~10 pages/IP.
#   2. Direct employer ATS boards (Greenhouse / Lever / Ashby public JSON). Freshest data,
#      canonical posted dates, no scraping ToS issues. Used for target companies in rules.json.
# Same normalize() + try/except pattern as scraper_service.py.

import html, os, re, requests, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from scraper_service import normalize

# Glassdoor and ZipRecruiter answer 403 to cloud IPs (GitHub Actions/Render) and Google returns nothing, so they only
# cost time, and the odd Glassdoor link that got through needs a sign-up. Override with JOBSPY_SITES="indeed,linkedin,...".
_ALL_JOBSPY_SITES = ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]
JOBSPY_SITES = [x for x in (os.getenv("JOBSPY_SITES") or "indeed,linkedin").replace(" ", "").split(",") if x in _ALL_JOBSPY_SITES]
UA = {"User-Agent": "Mozilla/5.0 (jobfinder-v4; personal job search)"}


def _strip_html(s: str) -> str:
    # Greenhouse content is double-escaped (&lt;p&gt;...&amp;nbsp;): unescape, strip tags, unescape again,
    # and turn non-breaking spaces into spaces so "unable to&nbsp;sponsor" still matches the visa gate.
    text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    return text.replace("\xa0", " ")


JOBSPY_TIMEOUT = int(os.getenv("JOBSPY_TIMEOUT", "120"))  # seconds per scrape_jobs call


def _with_timeout(fn, secs, **kw):
    """JobSpy's HTTP sessions have no read timeout; one stalled socket froze a whole run.
    Run in a daemon thread so a hung call is abandoned (raises TimeoutError) and never blocks exit."""
    out = {}
    def target():
        try:
            out["v"] = fn(**kw)
        except Exception as e:
            out["e"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        raise TimeoutError(f"{fn.__name__} exceeded {secs}s")
    if "e" in out:
        raise out["e"]
    return out["v"]


def fetch_jobspy(keywords: list[str], location: str = "", hours: int = 24,
                 sites: list[str] | None = None, results: int = 50, remote: bool = False,
                 proxies: list[str] | None = None, locations: list[str] | None = None) -> list[dict]:
    """One JobSpy call per keyword x location (given location + profile locations + remote US).
    Unquoted search terms (quotes made Indeed/Glassdoor return almost nothing). Deduped by URL.
    LinkedIn's guest search rate-limits hard per IP (~15 x HTTP 429/run): queried only for the
    caller's explicit location and the remote-US sweep, not every profile location. The other
    sites get the full location fan-out."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        return []
    sites = [s for s in (sites or JOBSPY_SITES) if s in JOBSPY_SITES]
    # (location, is_remote) pairs: caller's location, each profile location, then remote US (always searched)
    locs = list(dict.fromkeys(([(location, remote)] if location else []) +
                              [(l, False) for l in (locations or [])] + [("United States", True)]))
    li_locs = ({(location, remote)} if location else set()) | {("United States", True)}
    jobs, seen = [], set()
    for kw in keywords[:4]:
        for loc, is_rem in locs:
            site_list = sites if (loc, is_rem) in li_locs else [s for s in sites if s != "linkedin"]
            if not site_list:
                continue
            try:
                df = _with_timeout(
                    scrape_jobs, JOBSPY_TIMEOUT,
                    site_name=site_list,
                    search_term=kw,
                    google_search_term=f"{kw} jobs {'remote' if is_rem else loc} since yesterday",
                    location=loc,
                    is_remote=is_rem,
                    results_wanted=results,
                    hours_old=hours or None,
                    country_indeed="USA",
                    linkedin_fetch_description=True,
                    proxies=proxies,
                    verbose=0,
                )
            except Exception:
                continue
            for row in df.fillna("").to_dict("records"):
                url = str(row.get("job_url_direct") or row.get("job_url", ""))
                if not url or url in seen:
                    continue
                seen.add(url)
                try:
                    j = normalize(
                        title=str(row.get("title", "")),
                        company=str(row.get("company", "")),
                        location=str(row.get("location", "")),
                        url=url,
                        posted=str(row.get("date_posted", ""))[:10],
                        source=f"JobSpy/{row.get('site', '')}",
                        description=str(row.get("description", "")),
                    )
                    j["is_remote"] = bool(row.get("is_remote"))
                    jobs.append(j)
                except Exception:
                    pass
    return jobs


# ── ATS boards ───────────────────────────────────────────────────────────────

def _greenhouse(slug: str, company: str) -> list[dict]:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     params={"content": "true"}, headers=UA, timeout=12)
    r.raise_for_status()
    out = []
    for x in r.json().get("jobs", []):
        posted = (x.get("first_published") or x.get("updated_at") or "")[:10]
        j = normalize(x.get("title", ""), company, (x.get("location") or {}).get("name", ""),
                      x.get("absolute_url", ""), posted, "ATS/Greenhouse", _strip_html(x.get("content", "")))
        j["is_remote"] = "remote" in j["location"].lower()
        out.append(j)
    return out


def _lever(slug: str, company: str) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, headers=UA, timeout=12)
    r.raise_for_status()
    out = []
    for x in r.json():
        ts = x.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        j = normalize(x.get("text", ""), company, (x.get("categories") or {}).get("location", ""),
                      x.get("hostedUrl", ""), posted, "ATS/Lever", x.get("descriptionPlain", ""))
        j["is_remote"] = x.get("workplaceType") == "remote" or "remote" in j["location"].lower()
        out.append(j)
    return out


def _ashby(slug: str, company: str) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=12)
    r.raise_for_status()
    out = []
    for x in r.json().get("jobs", []):
        j = normalize(x.get("title", ""), company, x.get("location", ""), x.get("jobUrl", ""),
                      (x.get("publishedAt") or "")[:10], "ATS/Ashby", x.get("descriptionPlain", ""))
        j["is_remote"] = bool(x.get("isRemote")) or "remote" in j["location"].lower()
        out.append(j)
    return out


WORKDAY_QUERIES = ["product manager", "product owner", "program manager"]
WORKDAY_MAX_DAYS = 7  # only fetch details (date/location/description) for jobs this fresh


def _workday_days_ago(posted_on: str) -> int | None:
    """Workday lists only relative dates: 'Posted Today', 'Posted Yesterday', 'Posted 3 Days Ago', 'Posted 30+ Days Ago'."""
    s = (posted_on or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*day", s)
    return int(m.group(1)) + (1 if "+" in s else 0) if m else None


def _wd_call(method: str, url: str, **kw):
    """Workday returns 429 (HTML) on bursts from one IP. Back off and retry, then raise."""
    import time
    for wait in (2, 5, 10, None):
        r = requests.request(method, url, timeout=12, **kw)
        if r.status_code != 429 or wait is None:
            r.raise_for_status()
            return r.json()
        time.sleep(wait)


_WD_LOCK = threading.Lock()  # Workday's 429 limit is shared across tenants on one IP: one board at a time


def _workday(tenant: str, site: str, company: str, queries: list[str] | None = None) -> list[dict]:
    with _WD_LOCK:
        return _workday_unlocked(tenant, site, company, queries or WORKDAY_QUERIES)


def _workday_unlocked(tenant: str, site: str, company: str, queries: list[str] | None = None) -> list[dict]:
    """Public Workday jobs API. tenant = '<name>.wd<N>' (e.g. 'crowdstrike.wd5'), site = career site id.
    List search (POST .../jobs, 20/page) -> detail GET for fresh PM-family titles: canonical startDate,
    all locations, remoteType, description. Skips postings whose application endDate has passed."""
    import time
    from datetime import date, timedelta
    host =f"https://{tenant}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant.split('.')[0]}/{site}"
    hdr = {**UA, "Accept": "application/json", "Content-Type": "application/json"}
    listing = {}
    queries = [q.lower() for q in (queries or WORKDAY_QUERIES)]
    # Workday search is fuzzy: keep only listings whose title contains one of the searched phrases
    # (or, for a phrase like "product manager", its distinctive first word, e.g. "Group Product Lead")
    hit = re.compile("|".join(re.escape(q.split()[0] if len(q.split()) > 1 else q) for q in queries), re.I)
    for q in queries:
        for offset in range(0, 100, 20):
            page = _wd_call("POST", f"{api}/jobs", headers=hdr,
                            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}).get("jobPostings", [])
            hits = [x for x in page if x.get("externalPath") and hit.search(x.get("title", ""))]
            listing.update((x["externalPath"], x) for x in hits)
            if len(page) < 20:  # don't stop on a page with no hits: some large tenants rank poorly
                break
            time.sleep(0.4)  # stay under Workday's per-IP burst limit

    today = date.today()

    def build(x):
        days = _workday_days_ago(x.get("postedOn", ""))
        posted = (today - timedelta(days=days)).isoformat() if days is not None else ""
        loc, desc, remote = x.get("locationsText", ""), "", "remote" in x.get("locationsText", "").lower()
        if days is not None and days <= WORKDAY_MAX_DAYS:
            try:
                d = _wd_call("GET", f"{api}{x['externalPath']}", headers=hdr)["jobPostingInfo"]
                if d.get("endDate") and d["endDate"][:10] < today.isoformat():
                    return None  # application deadline passed (NVIDIA-style "posted yesterday" trap)
                posted = (d.get("startDate") or posted)[:10]
                loc = "; ".join([d.get("location", "")] + d.get("additionalLocations", [])) or loc
                desc = _strip_html(d.get("jobDescription", ""))
                remote = remote or (d.get("remoteType") or "").lower() == "remote"
            except Exception:
                pass  # keep listing-level data
        j = normalize(x.get("title", ""), company, loc, f"{host}/{site}{x['externalPath']}", posted, "ATS/Workday", desc)
        j["is_remote"] = remote or "remote" in j["location"].lower()
        return j

    with ThreadPoolExecutor(max_workers=3) as ex:
        return [j for j in ex.map(build, listing.values()) if j]


ATS_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
                "workday": lambda slug, company, queries=None: _workday(*slug.split("/", 1), company, queries)}  # slug = "tenant.wdN/site"


def fetch_company_board(company: str, ats: str | None, slug: str, queries: list[str] | None = None) -> list[dict]:
    """Fetch one employer board. ats=None -> try each ATS until one answers.
    queries: title phrases for boards that need a search (Workday); the others return every job."""
    for name in ([ats] if ats else list(ATS_FETCHERS)):
        try:
            fn = ATS_FETCHERS[name]
            jobs = fn(slug, company.title(), queries) if name == "workday" else fn(slug, company.title())
            if jobs or ats:
                return jobs
        except Exception:
            continue
    return []


def fetch_ats(boards: dict, companies: list[str] | None = None, queries: list[str] | None = None) -> list[dict]:
    """Fetch all configured employer boards in parallel. boards = rules['ats_boards']."""
    items = [(c, v) for c, v in boards.items() if not c.startswith("_") and (not companies or c in companies)]
    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda cv: fetch_company_board(cv[0], cv[1].get("ats"), cv[1]["slug"], queries), items):
            jobs += res
    return jobs
