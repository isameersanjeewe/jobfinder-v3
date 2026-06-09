# ── AI SCORING SERVICE ───────────────────────────────────────────────────────
# Uses Claude API to score jobs for:
#   1. H1B sponsorship likelihood
#   2. Resume match score (0-100)
#   3. Feedback boost — liked jobs push similar ones up, disliked ones down

import requests

ANTHROPIC_KEY = "YOUR_ANTHROPIC_API_KEY"  # console.anthropic.com → API Keys
CLAUDE_MODEL  = "claude-3-5-sonnet-20241022"


def _call_claude(prompt: str, max_tokens: int = 200) -> str:
    """Raw Claude API call."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        return r.json()["content"][0]["text"].strip()
    except Exception:
        return ""


def score_h1b_ai(job: dict) -> str:
    """
    Claude reads full job description and returns H1B likelihood.
    Returns: 'High', 'Low', or 'Unknown'
    Smarter than keyword matching — catches indirect signals.
    """
    if ANTHROPIC_KEY == "YOUR_ANTHROPIC_API_KEY":
        return job.get("h1b_friendly", "Unknown")

    desc = job.get("description", "")[:600]
    if not desc:
        return job.get("h1b_friendly", "Unknown")

    result = _call_claude(
        f"""Assess H1B visa sponsorship likelihood for this job.
Reply with ONLY one word: High, Low, or Unknown.

Job: {job['title']} at {job['company']}
Description: {desc}"""
    )
    return result if result in ["High", "Low", "Unknown"] else "Unknown"


def score_resume_match(job: dict, resume_text: str) -> int:
    """
    Claude scores resume fit for a specific job (0-100).
    Higher = stronger profile match.
    """
    if ANTHROPIC_KEY == "YOUR_ANTHROPIC_API_KEY" or not resume_text:
        return 0

    result = _call_claude(
        f"""Score resume-to-job fit from 0 to 100. Reply with ONLY a number.

JOB: {job['title']} at {job['company']}
DESCRIPTION: {job.get('description','')[:400]}
RESUME: {resume_text[:800]}"""
    )
    try:
        return min(100, max(0, int(result)))
    except Exception:
        return 0


def enrich_jobs_with_ai(jobs: list[dict], resume_text: str = "", use_ai: bool = False) -> list[dict]:
    """
    Enriches top 10 jobs with AI H1B + match scores.
    Caps at 10 to preserve daily API quota.
    """
    if not use_ai or ANTHROPIC_KEY == "YOUR_ANTHROPIC_API_KEY":
        for job in jobs:
            job.setdefault("ai_h1b", "—")
            job.setdefault("match", 0)
        return jobs

    for job in jobs[:10]:
        job["ai_h1b"] = score_h1b_ai(job)
        job["match"]  = score_resume_match(job, resume_text) if resume_text else 0

    for job in jobs[10:]:
        job.setdefault("ai_h1b", "—")
        job.setdefault("match", 0)

    return jobs


def apply_feedback_boost(jobs: list[dict], feedback_store: dict) -> list[dict]:
    """
    Adjusts 'final_score' based on user feedback.
    👍 up   → +20 bonus on top of match score
    👎 down → score set to -1 (sinks to bottom or can be hidden)
    No feedback → final_score = match score as-is
    """
    for job in jobs:
        base = job.get("match", 50)   # Default 50 if no AI score
        signal = feedback_store.get(job["url"], "")
        if signal == "up":
            job["final_score"] = base + 20
        elif signal == "down":
            job["final_score"] = -1
        else:
            job["final_score"] = base
    return jobs
