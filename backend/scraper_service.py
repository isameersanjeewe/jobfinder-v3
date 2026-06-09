# ── SCRAPER SERVICE ──────────────────────────────────────────────────────────
# Fetches jobs from real APIs — no hardcoded company slugs.
# Sources: JSearch (LinkedIn/Indeed/Glassdoor), Adzuna, Remotive (remote-only, free)
# Add a new source by following the normalize() + fetch_X() pattern.

import requests
from datetime import datetime

JSEARCH_KEY = "YOUR_JSEARCH_KEY"   # rapidapi.com → search "JSearch" → free tier
ADZUNA_ID   = "YOUR_ADZUNA_ID"     # adzuna.com/api → free
ADZUNA_KEY  = "YOUR_ADZUNA_KEY"


def normalize(title, company, location, url, posted, source, description=""):
    """Unified job shape used across all services."""
    return {
        "title":       title.strip(),
        "company":     company.strip(),
        "location":    location.strip() if location else "Remote / Not specified",
        "url":         url.strip(),
        "posted":      posted or "N/A",
        "source":      source,
        "description": description[:800],   # Truncated — used for AI scoring
        "scraped":     datetime.now().strftime("%Y-%m-%d"),
    }


def fetch_jsearch(keywords: list[str], location: str = "", hours: int = 0) -> list[dict]:
    """
    JSearch (RapidAPI) — scrapes LinkedIn, Indeed, Glassdoor, ZipRecruiter.
    Best coverage. Free tier: 200 calls/month.
    hours=0 means no recency filter; 24 = last 24h; 1 = last hour (JSearch supports: all/today/3days/week/month)
    """
    if JSEARCH_KEY == "YOUR_JSEARCH_KEY":
        return []

    date_map = {0: "all", 1: "today", 6: "today", 24: "today", 72: "3days", 168: "week"}
    date_posted = date_map.get(hours, "all")

    jobs = []
    for kw in keywords[:3]:   # Limit to 3 queries to stay in free tier
        query = f"{kw} {location}".strip()
        try:
            r = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers={
                    "X-RapidAPI-Key": JSEARCH_KEY,
                    "X-RapidAPI-Host": "jsearch.p.rapidapi.com"
                },
                params={"query": query, "num_pages": "2", "date_posted": date_posted},
                timeout=12
            )
            r.raise_for_status()
            data = r.json()
            for job in data.get("data", []):
                try:
                    jobs.append(normalize(
                        title=job.get("job_title", ""),
                        company=job.get("employer_name", ""),
                        location=f"{job.get('job_city','')}, {job.get('job_state','')}".strip(", "),
                        url=job.get("job_apply_link", job.get("job_google_link", "")),
                        posted=job.get("job_posted_at_datetime_utc", "")[:10],
                        source="JSearch",
                        description=job.get("job_description", "")
                    ))
                except Exception:
                    pass
        except Exception:
            pass
    return jobs


def fetch_adzuna(keywords: list[str], location: str = "", results: int = 20) -> list[dict]:
    """
    Adzuna — 500 free calls/month. Broad US job market coverage.
    Get key at: adzuna.com/api
    """
    if ADZUNA_ID == "YOUR_ADZUNA_ID":
        return []

    jobs = []
    for kw in keywords[:3]:
        try:
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/us/search/1",
                params={
                    "app_id": ADZUNA_ID,
                    "app_key": ADZUNA_KEY,
                    "results_per_page": results,
                    "what": kw,
                    "where": location or "United States",
                    "content-type": "application/json"
                },
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            for job in data.get("results", []):
                try:
                    jobs.append(normalize(
                        title=job.get("title", ""),
                        company=job.get("company", {}).get("display_name", ""),
                        location=job.get("location", {}).get("display_name", ""),
                        url=job.get("redirect_url", ""),
                        posted=job.get("created", "")[:10],
                        source="Adzuna",
                        description=job.get("description", "")
                    ))
                except Exception:
                    pass
        except Exception:
            pass
    return jobs


def fetch_remotive(keywords: list[str]) -> list[dict]:
    """
    Remotive — completely free, no key needed. Remote jobs only.
    Good for remote PM/data roles.
    """
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs?category=product", timeout=10)
        r.raise_for_status()
        data = r.json()
        for job in data.get("jobs", []):
            try:
                title = job.get("title", "").lower()
                # Match if any keyword or any word from keyword phrase appears in title
                keyword_words = [word for kw in keywords for word in kw.split()]
                if any(word in title for word in keyword_words):
                    jobs.append(normalize(
                        title=job.get("title", ""),
                        company=job.get("company_name", ""),
                        location="Remote",
                        url=job.get("url", ""),
                        posted=job.get("publication_date", "")[:10],
                        source="Remotive",
                        description=job.get("description", "")[:800]
                    ))
            except Exception:
                pass
    except Exception:
        pass
    return jobs
