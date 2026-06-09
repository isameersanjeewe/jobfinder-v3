# ── MAIN API ─────────────────────────────────────────────────────────────────
# FastAPI app wiring all microservices.
# Endpoints:
#   POST /search       → search + filter + optional AI scoring
#   POST /export       → same as search, returns CSV
#   POST /parse-resume → extract keywords + text from uploaded PDF
#   POST /feedback     → thumbs up/down per job, adjusts future scoring
#   GET  /usage        → daily API call counter per source

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from datetime import date
import io, pdfplumber, json, os

from scraper_service import fetch_jsearch, fetch_adzuna, fetch_remotive
from filter_service   import run_filters
from ai_service       import enrich_jobs_with_ai, apply_feedback_boost
from export_service   import to_csv

app = FastAPI(title="JobFinder", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
)

# ── IN-MEMORY STORES (reset on server restart) ────────────────────────────────
# feedback: { job_url: "up" | "down" }
# usage:    { "YYYY-MM-DD": { source: count } }
feedback_store: dict[str, str] = {}
usage_store:    dict[str, dict] = {}

# ── DAILY API LIMITS (free tiers) ─────────────────────────────────────────────
DAILY_LIMITS = {
    "jsearch":  6,    # RapidAPI free: 200/month ÷ 30 ≈ 6/day
    "adzuna":   16,   # Adzuna free: 500/month ÷ 30 ≈ 16/day
    "remotive": 999,  # Unlimited
    "claude":   50,   # Anthropic pay-as-you-go; set your own comfort limit
}


def track_usage(source: str, count: int = 1):
    """Increments daily usage counter for a given source."""
    today = str(date.today())
    usage_store.setdefault(today, {})
    usage_store[today][source] = usage_store[today].get(source, 0) + count


def get_today_usage() -> dict:
    today = str(date.today())
    return usage_store.get(today, {})


# ── REQUEST SCHEMAS ───────────────────────────────────────────────────────────
class SearchParams(BaseModel):
    keywords:    list[str] = ["product manager", "product owner"]
    exclude:     list[str] = ["intern", "junior", "hardware", "mechanical"]
    locations:   list[str] = []
    sources:     list[str] = ["remotive"]
    h1b_only:    bool      = False
    hours:       int       = 0
    use_ai:      bool      = False
    resume_text: str       = ""

class FeedbackPayload(BaseModel):
    url:      str   # Job URL as unique ID
    title:    str
    company:  str
    signal:   str   # "up" or "down"


# ── CORE PIPELINE ─────────────────────────────────────────────────────────────
def pipeline(params: SearchParams) -> list[dict]:
    """Scrape → Filter → AI Score → Feedback Boost → Sort."""
    kws = [k.lower().strip() for k in params.keywords if k.strip()]
    loc = params.locations[0] if params.locations else ""
    jobs = []

    # Scrape from all enabled sources with error handling
    try:
        if "jsearch" in params.sources:
            jobs += fetch_jsearch(kws, loc, params.hours)
            track_usage("jsearch", len([k for k in kws if k]))
    except Exception:
        pass

    try:
        if "adzuna" in params.sources:
            jobs += fetch_adzuna(kws, loc)
            track_usage("adzuna", len([k for k in kws if k]))
    except Exception:
        pass

    try:
        if "remotive" in params.sources:
            jobs += fetch_remotive(kws)
            track_usage("remotive")
    except Exception:
        pass

    # Run all filters
    try:
        jobs = run_filters(jobs, params.dict())
    except Exception:
        pass

    # H1B filtering
    if params.h1b_only:
        jobs = [j for j in jobs if j.get("h1b_friendly") != "No"]

    # Attach prior feedback signals to each job before AI scoring
    for job in jobs:
        job["feedback"] = feedback_store.get(job["url"], "")

    # AI enrichment with error handling
    try:
        jobs = enrich_jobs_with_ai(jobs, params.resume_text, params.use_ai)
    except Exception:
        for job in jobs:
            job.setdefault("ai_h1b", "—")
            job.setdefault("match", 0)

    if params.use_ai:
        track_usage("claude", min(len(jobs), 10))

    # Boost/demote based on feedback, then sort
    try:
        jobs = apply_feedback_boost(jobs, feedback_store)
    except Exception:
        pass
    
    jobs.sort(key=lambda j: j.get("final_score", j.get("match", 0)), reverse=True)

    return jobs


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "JobFinder", "version": "3.0", "status": "running"}


@app.post("/search")
def search(params: SearchParams):
    try:
        jobs = pipeline(params)
        return JSONResponse({
            "count": len(jobs),
            "jobs":  jobs,
            "usage": get_today_usage(),
            "limits": DAILY_LIMITS
        })
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "count": 0,
            "jobs": [],
            "usage": get_today_usage(),
            "limits": DAILY_LIMITS
        }, status_code=500)


@app.post("/export")
def export(params: SearchParams):
    try:
        jobs = pipeline(params)
        csv_content = to_csv(jobs)
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=jobs.csv"}
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/feedback")
def feedback(payload: FeedbackPayload):
    """
    Stores thumbs up/down for a job.
    'up'   → job will be boosted in future results
    'down' → job will be demoted / hidden
    Feedback persists for the session (server lifetime).
    """
    feedback_store[payload.url] = payload.signal
    return {"stored": True, "url": payload.url, "signal": payload.signal}


@app.post("/parse-resume")
async def parse_resume(file: UploadFile = File(...)):
    """Extracts full text + matched keywords from uploaded resume PDF."""
    try:
        content = await file.read()
        if not content:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = " ".join(p.extract_text() or "" for p in pdf.pages)
        
        if not text:
            return JSONResponse({"keywords": [], "resume_text": ""})

        skill_keywords = [
            "product owner", "product manager", "data platform", "data governance",
            "ai product", "analytics", "sql", "agile", "saas", "cloud", "aws", "gcp",
            "roadmap", "okr", "stakeholder", "machine learning", "genai", "llm",
            "python", "tableau", "looker", "figma", "scrum", "healthcare"
        ]
        matched = [kw for kw in skill_keywords if kw in text.lower()]
        return {"keywords": matched, "resume_text": text[:2000]}
    except Exception as e:
        return JSONResponse({"error": str(e), "keywords": [], "resume_text": ""}, status_code=400)


@app.get("/usage")
def usage():
    """Returns today's API call counts vs daily limits."""
    today = get_today_usage()
    return {
        "date":   str(date.today()),
        "usage":  today,
        "limits": DAILY_LIMITS,
        "remaining": {k: max(0, DAILY_LIMITS[k] - today.get(k, 0)) for k in DAILY_LIMITS}
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0"}
