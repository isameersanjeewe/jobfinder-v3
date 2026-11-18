# ── CONTACT FINDING (free, no scraping) ─────────────────────────────────────
# Roadmap item 3: `contact_points` in the score is always 0 today -- there's no real contact-finding.
# The free + safe option is a deep link to LinkedIn's own People-tab search, pre-filled with the
# company name plus a hiring-relevant role hint derived from the job title. The user opens it and reads
# the results themselves; nothing here logs in, scrapes, or stores a real person's data. This does not
# change scoring (contact_points stays 0 until there's a way to confirm an actual named contact) -- it's
# a research aid attached to the lead, not a new signal.

from urllib.parse import quote

_GENERIC_HINT = 'recruiter OR "talent acquisition" OR "people team" OR "hiring manager"'


def linkedin_people_url(company: str, title: str = "") -> str:
    """Deep link to LinkedIn's People-tab search for `company`, biased toward recruiting/hiring-manager
    titles relevant to `title`'s role family. Returns "" if company is blank (nothing to search for)."""
    company = (company or "").strip()
    if not company:
        return ""
    t = (title or "").lower()
    if "program" in t or "project" in t:
        hint = f'"director of program management" OR "head of program management" OR {_GENERIC_HINT}'
    elif "product" in t:
        hint = f'"head of product" OR "VP product" OR "director of product" OR {_GENERIC_HINT}'
    else:
        hint = _GENERIC_HINT
    q = f"{company} ({hint})"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(q)}"
