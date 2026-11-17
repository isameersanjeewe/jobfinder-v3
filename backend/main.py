# ── MAIN API ─────────────────────────────────────────────────────────────────
# FastAPI app wiring all microservices.
# Endpoints:
#   POST /search       → search + filter + optional AI scoring
#   POST /export       → same as search, returns CSV
#   POST /parse-resume → extract keywords + text from uploaded PDF
#   POST /feedback     → thumbs up/down per job, adjusts future scoring
#   GET  /usage        → daily API call counter per source

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated, Literal
from datetime import date
import io, pdfplumber, json, os, hmac, logging, threading, time, uuid
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
from rules_service    import load_rules, apply_gate, apply_filters, profile_filters
from agent_service    import AgentRun
from fastapi          import Response
import store

log = logging.getLogger("jobfinder")
app = FastAPI(title="JobFinder", version="4.0", docs_url=None if os.getenv("API_TOKEN") else "/docs", redoc_url=None)

# ── SECURITY ──────────────────────────────────────────────────────────────────
# API_TOKEN set  -> every endpoint except / and /health needs "Authorization: Bearer <token>".
# API_TOKEN unset -> open (local dev only). A public deployment MUST set it: otherwise anyone can
# start long scraping runs, read saved leads, or append rows to your Sheet with the server's credentials.
API_TOKEN = os.getenv("API_TOKEN", "")
PUBLIC_PATHS = {"/", "/health"}
if not API_TOKEN:
    log.warning("API_TOKEN not set: API is unauthenticated (fine for localhost, unsafe when deployed)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
)


@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    if API_TOKEN and request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        sent = request.headers.get("authorization", "")
        if not hmac.compare_digest(sent.encode(), f"Bearer {API_TOKEN}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_PAGES = 20
_agent_lock = threading.Lock()          # one agent run at a time (each run scrapes for minutes)
_jobs: dict[str, dict] = {}             # background agent runs: id -> {status, result, started}

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
def Short():
    return Field(default="", max_length=200)


def UserId():
    return Field(default="default", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")


class SearchParams(BaseModel):
    keywords:    list[str] = Field(default=["product manager", "product owner"], max_length=10)
    exclude:     list[str] = Field(default=["intern", "junior", "hardware", "mechanical"], max_length=30)
    locations:   list[str] = Field(default=[], max_length=5)
    sources:     list[Literal["ats", "jobspy", "remotive", "jsearch", "adzuna"]] = ["remotive"]
    h1b_only:    bool      = False
    hours:       int       = Field(default=0, ge=0, le=720)
    use_ai:      bool      = False
    resume_text: str       = Field(default="", max_length=20000)

Phrase = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]


class AgentFilters(BaseModel):
    """Per-run overrides of the profile, set in the UI. Omitted/None fields keep the profile value."""
    titles:            list[Phrase] | None = Field(default=None, max_length=15)
    locations:         list[Phrase] | None = Field(default=None, max_length=8)
    remote_ok:         bool | None = None
    visa:              Literal["none", "new_sponsorship", "h1b_transfer"] | None = None
    sponsors_only:     bool | None = None   # company must be a confirmed Strong/Moderate H-1B sponsor
    industries:        list[Phrase] | None = Field(default=None, max_length=10)
    exclude_companies: list[Phrase] | None = Field(default=None, max_length=100)


class AgentParams(BaseModel):
    filters:      AgentFilters | None = None
    goal:         str  = Field(default="", max_length=500)
    resume_text:  str  = Field(default="", max_length=20000)
    user_id:      str  = UserId()
    min_fit:      int  = Field(default=40, ge=0, le=100)
    resume_years: int | None = Field(default=None, ge=0, le=60)
    hours:        int | None = Field(default=None, ge=1, le=720)   # overrides recency_hours (24 daily, 168 weekly)

class SheetExport(BaseModel):
    sheet_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")  # falls back to env SHEET_ID
    tab:      str = Field(default="Leads", min_length=1, max_length=100)
    user_id:  str = UserId()
    since:    str = Field(default="", max_length=20)
    track:    Literal["", "ft", "c2c"] = ""   # "c2c" for the "C2C Leads" tab, "ft" for FT tabs
    hot_only: bool = True                     # only Hot Leads (score >= 65) go to the Sheet
    jobs:     list[dict] = Field(default=[], max_length=500)   # optional: export these instead of stored leads

class FeedbackPayload(BaseModel):
    url:      str = Field(max_length=2000)   # Job URL as unique ID
    title:    str = Short()
    company:  str = Short()
    signal:   Literal["up", "down", ""]


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
    return {"service": "JobFinder", "version": "4.0", "status": "running", "auth": bool(API_TOKEN)}


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
    except Exception:
        log.exception("search failed")
        return JSONResponse({
            "error": "search failed (see server log)",
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
    except Exception:
        log.exception("export failed")
        return JSONResponse({"error": "export failed (see server log)"}, status_code=500)


def _run_agent(params: AgentParams) -> dict:
    rules = apply_filters(load_rules(), params.filters.model_dump() if params.filters else None)
    if params.hours:
        rules["profile"]["recency_hours"] = params.hours
    run = AgentRun(params.resume_text, params.user_id, rules=rules, min_fit=params.min_fit, resume_years=params.resume_years)
    res = run.run(params.goal)
    for l in res["leads"]:  # the UI doesn't need full JDs; keep responses small
        l.pop("description", None)
    return res


@app.post("/agent/run")
def agent_run(params: AgentParams):
    """Synchronous agent run (CLI/tests). The UI uses /agent/start + /agent/jobs/{id} instead."""
    if not _agent_lock.acquire(blocking=False):
        return JSONResponse({"error": "an agent run is already in progress", "leads": []}, status_code=429)
    try:
        return JSONResponse(_run_agent(params))
    except Exception:
        log.exception("agent run failed")
        return JSONResponse({"error": "agent run failed (see server log)", "leads": []}, status_code=500)
    finally:
        _agent_lock.release()


@app.post("/agent/start")
def agent_start(params: AgentParams):
    """Start an agent run in the background (runs take minutes; free hosts drop long HTTP requests)."""
    if not _agent_lock.acquire(blocking=False):
        running = next((k for k, v in _jobs.items() if v["status"] == "running"), None)
        return JSONResponse({"error": "an agent run is already in progress", "job_id": running}, status_code=429)
    job_id = uuid.uuid4().hex
    for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:  # keep the last 20 results only
        _jobs.pop(k, None)
    _jobs[job_id] = {"status": "running", "started": time.time(), "result": None}

    def work():
        try:
            _jobs[job_id].update(status="done", result=_run_agent(params))
        except Exception:
            log.exception("agent job failed")
            _jobs[job_id].update(status="error", result={"error": "agent run failed (see server log)", "leads": []})
        finally:
            _agent_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/profile/filters")
def get_profile_filters():
    """Defaults for the UI's Search filters panel (the server profile). Requires the token like any endpoint."""
    return profile_filters(load_rules())


@app.get("/agent/jobs/{job_id}")
def agent_job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {"job_id": job_id, "status": j["status"], "elapsed_s": int(time.time() - j["started"]), "result": j["result"]}


@app.get("/leads")
def leads(user_id: str = "default", since: str = "", track: str = "", format: str = "json"):
    """All saved leads. format=json | csv | xlsx (xlsx/csv open directly in Google Sheets)."""
    if format not in ("json", "csv", "xlsx") or track not in ("", "ft", "c2c") or len(user_id) > 64 or len(since) > 20:
        return JSONResponse({"error": "bad parameters"}, status_code=400)
    rows = store.list_leads(user_id, since, track)
    for r in rows:
        r.pop("description", None)
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
    if payload.signal:
        if payload.url not in feedback_store and len(feedback_store) >= 5000:
            feedback_store.pop(next(iter(feedback_store)))  # bounded: oldest entry out
        feedback_store[payload.url] = payload.signal
    else:
        feedback_store.pop(payload.url, None)
    return {"stored": True, "url": payload.url, "signal": payload.signal}


@app.post("/parse-resume")
async def parse_resume(file: UploadFile = File(...)):
    """Extracts full text + matched keywords from uploaded resume PDF."""
    try:
        content = await file.read(MAX_RESUME_BYTES + 1)
        if not content:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        if len(content) > MAX_RESUME_BYTES:
            return JSONResponse({"error": "File too large (max 5 MB)"}, status_code=413)
        if not content.startswith(b"%PDF-"):
            return JSONResponse({"error": "Not a PDF"}, status_code=400)

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = " ".join(p.extract_text() or "" for p in pdf.pages[:MAX_RESUME_PAGES])
        
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
    except Exception:
        log.exception("resume parse failed")
        return JSONResponse({"error": "could not read this PDF", "keywords": [], "resume_text": ""}, status_code=400)


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
    return {"status": "ok", "version": "4.0"}
