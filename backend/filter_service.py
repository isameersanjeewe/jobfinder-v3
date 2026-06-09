# ── FILTER SERVICE ───────────────────────────────────────────────────────────
# Pure filter functions — each takes a list in, returns a filtered list out.
# H1B scoring uses keyword signals from job description + title.

from datetime import datetime, timedelta

H1B_YES = [
    "h1b", "h-1b", "visa sponsorship", "will sponsor", "sponsorship available",
    "open to sponsorship", "we sponsor", "sponsorship provided", "work visa"
]

H1B_NO = [
    "no sponsorship", "sponsorship not available", "must be authorized",
    "us citizen only", "must be a us citizen", "no visa", "green card only",
    "permanent resident only", "citizens only", "must be eligible to work",
    "authorized to work in the us without sponsorship"
]


def filter_keywords(jobs: list[dict], keywords: list[str], exclude: list[str]) -> list[dict]:
    """
    More realistic filtering:
    - If keywords provided: include jobs that match at least one keyword (full phrase or words)
    - If no keywords: include all jobs (user wants to browse)
    - Exclude jobs with exclusion keywords
    """
    if not keywords:
        # No keywords specified - include all jobs
        result = jobs
    else:
        # Build search terms from keywords
        search_terms = set()
        for kw in keywords:
            kw_lower = kw.lower()
            search_terms.add(kw_lower)  # Full phrase
            for word in kw_lower.split():
                if len(word) > 2:  # Only add words 3+ chars (skip "and", "for", etc.)
                    search_terms.add(word)
        
        result = []
        for job in jobs:
            title_lower = job["title"].lower()
            desc_lower = job.get("description", "").lower()
            # Match if any search term appears in title or description
            if any(term in title_lower or term in desc_lower for term in search_terms):
                result.append(job)
    
    # Apply exclusions (always filter these out)
    exclude_set = {ex.lower() for ex in exclude}
    return [
        j for j in result
        if not any(ex in j["title"].lower() for ex in exclude_set)
    ]


def filter_location(jobs: list[dict], locations: list[str]) -> list[dict]:
    """Pass all if no locations given. Always pass remote jobs."""
    if not locations:
        return jobs
    return [
        j for j in jobs
        if "remote" in j["location"].lower()
        or any(loc in j["location"].lower() for loc in locations)
    ]


def filter_recency(jobs: list[dict], hours: int) -> list[dict]:
    """Keep jobs posted within last N hours. 0 = no filter."""
    if not hours:
        return jobs
    cutoff = datetime.now() - timedelta(hours=hours)
    result = []
    for job in jobs:
        try:
            if datetime.strptime(job["posted"], "%Y-%m-%d") >= cutoff:
                result.append(job)
        except Exception:
            result.append(job)
    return result


def score_h1b(jobs: list[dict]) -> list[dict]:
    """
    Scores each job for H1B sponsorship likelihood.
    Checks description + title for positive/negative signals.
    Result: 'Yes', 'No', or 'Unknown'
    """
    for job in jobs:
        text = (job.get("description", "") + " " + job["title"]).lower()
        if any(s in text for s in H1B_NO):
            job["h1b_friendly"] = "No"
        elif any(s in text for s in H1B_YES):
            job["h1b_friendly"] = "Yes"
        else:
            job["h1b_friendly"] = "Unknown"
    return jobs


def deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicate jobs by URL."""
    seen, unique = set(), []
    for job in jobs:
        if job["url"] not in seen:
            seen.add(job["url"])
            unique.append(job)
    return unique


def run_filters(jobs: list[dict], params: dict) -> list[dict]:
    """Master pipeline — runs all filters in sequence."""
    keywords  = [k.lower() for k in params.get("keywords", [])]
    exclude   = [e.lower() for e in params.get("exclude", [])]
    locations = [l.lower() for l in params.get("locations", [])]

    jobs = filter_keywords(jobs, keywords, exclude)
    jobs = filter_location(jobs, locations)
    jobs = filter_recency(jobs, params.get("hours", 0))
    jobs = score_h1b(jobs)
    jobs = deduplicate(jobs)
    return jobs
