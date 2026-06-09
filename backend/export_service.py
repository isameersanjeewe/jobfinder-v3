# ── EXPORT SERVICE ───────────────────────────────────────────────────────────
# Converts job results to downloadable formats.

import csv, io

def to_csv(jobs: list[dict]) -> str:
    """Returns CSV string — streamed as file download by FastAPI."""
    fields = ["title", "company", "location", "posted", "source",
              "h1b_friendly", "ai_h1b", "match", "url", "scraped"]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(jobs)
    return out.getvalue()
