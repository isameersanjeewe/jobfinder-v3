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
try:  # local dev: load backend/.env (never committed). On Render/GitHub Actions, real env vars are used.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from scraper_service import fetch_jsearch, fetch_adzuna, fetch_remotive
from filter_service   import run_filters
from ai_service       import enrich_jobs_with_ai, apply_feedback_boost
from export_service   import to_csv, to_xlsx, append_to_sheet
from free_sources     import fetch_jobspy, fetch_ats
from rules_service    import load_rules, apply_gate
from agent_service    import AgentRun
from fastapi          import Response
import store

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
    "jobspy":   999,  # Free OSS scraper (Indeed/LinkedIn/Google/Zip/Glassdoor)
    "ats":      999,  # Free public employer boards (Greenhouse/Lever/Ashby)
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

class AgentParams(BaseModel):
    goal:         str  = ""
    resume_text:  str  = ""
    user_id:      str  = "default"
    min_fit:      int  = 40
    resume_years: int | None = None
    hours:        int | None = None   # overrides rules recency_hours (24 daily, 168 weekly)

class SheetExport(BaseModel):
    sheet_id: str = ""              # falls back to env SHEET_ID
    tab:      str = "Leads"
    user_id:  str = "default"
    since:    str = ""
    track:    str = ""              # "c2c" for the "C2C Leads" tab, "ft" for FT tabs
    hot_only: bool = True           # only Hot Leads (score >= 65) go to the Sheet
    jobs:     list[dict] = []   # optional: export these instead of stored leads

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

    try:
        if "jobspy" in params.sources:
            jobs += fetch_jobspy(kws, loc, params.hours or 24)
            track_usage("jobspy")
    except Exception:
        pass

    try:
        if "ats" in params.sources:
            jobs += fetch_ats(load_rules()["ats_boards"])
            track_usage("ats")
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


@app.post("/agent/run")
def agent_run(params: AgentParams):
    """Agentic run: LLM (or deterministic plan) searches, gates, researches H1B, scores, saves."""
    try:
        rules = load_rules()
        if params.hours:
            rules["profile"]["recency_hours"] = params.hours
        run = AgentRun(params.resume_text, params.user_id, rules=rules, min_fit=params.min_fit, resume_years=params.resume_years)
        return JSONResponse(run.run(params.goal))
    except Exception as e:
        return JSONResponse({"error": str(e), "leads": []}, status_code=500)


@app.get("/leads")
def leads(user_id: str = "default", since: str = "", track: str = "", format: str = "json"):
    """All saved leads. format=json | csv | xlsx (xlsx/csv open directly in Google Sheets)."""
    rows = store.list_leads(user_id, since, track)
    if format == "csv":
        return Response(to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=leads.csv"})
    if format == "xlsx":
        return Response(to_xlsx(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=leads.xlsx"})
    return {"count": len(rows), "leads": rows}


@app.post("/export/sheets")
def export_sheets(p: SheetExport):
    """Append leads to the user's Google Sheet (append-only, dedupes by URL)."""
    try:
        rows = p.jobs or store.list_leads(p.user_id, p.since, p.track)
        if p.hot_only:  # docs/CONTEXT.md: only Hot Leads go to the Sheet; the rest stay in the run log
            rows = [r for r in rows if r.get("hot_lead")]
        sheet_id = p.sheet_id or os.getenv("SHEET_ID", "")
        if not sheet_id:
            raise RuntimeError("sheet_id not given and SHEET_ID not set")
        return append_to_sheet(rows, sheet_id, p.tab)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


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
