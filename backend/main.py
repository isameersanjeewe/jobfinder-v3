# ── MAIN API ─────────────────────────────────────────────────────────────────
# FastAPI app wiring all microservices.
# Endpoints:
#   POST /search       → search + filter + optional AI scoring
#   POST /export       → same as search, returns CSV
#   POST /parse-resume → extract keywords + text from uploaded PDF
#   POST /feedback     → thumbs up/down per job, adjusts future scoring
#   GET  /usage        → daily API call counter per source

from fastapi import FastAPI, UploadFile, File, Request, Depends, Header, HTTPException, Path
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
from rules_service    import load_rules, apply_gate, apply_filters, profile_filters, sync_company_signals_from_repo
from agent_service    import AgentRun
from fastapi          import Response
import agent_mode
import auth
import data_repo
import outreach
import store

log = logging.getLogger("jobfinder")
app = FastAPI(title="JobFinder", version="4.0", docs_url=None if os.getenv("API_TOKEN") else "/docs", redoc_url=None)

# ── SECURITY ──────────────────────────────────────────────────────────────────
# API_TOKEN set  -> every endpoint except / and /health needs "Authorization: Bearer <token>".
# API_TOKEN unset -> open (local dev only). A public deployment MUST set it: otherwise anyone can
# start long scraping runs, read saved leads, or append rows to your Sheet with the server's credentials.
API_TOKEN = os.getenv("API_TOKEN", "")
# ADMIN_TOKEN: separate owner-only secret for /admin/* (header X-Admin-Token). Testers may be given
# API_TOKEN to reach the app, so it must not double as the admin password. Unset -> /admin/* disabled.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
PUBLIC_PATHS = {"/", "/health"}
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
if not API_TOKEN:
    log.warning("API_TOKEN not set: API is unauthenticated (fine for localhost, unsafe when deployed)")



@app.on_event("startup")
def _hydrate_users_from_data_repo():
    """Render's free plan wipes local SQLite/disk on every redeploy; pull existing testers' accounts and
    the shared H1B company-signal database (roadmap item 6) back from the private repo (if configured)
    before anyone tries to log in or run a search. See store.sync_users_from_repo and
    rules_service.sync_company_signals_from_repo."""
    n = store.sync_users_from_repo()
    if n:
        log.info("Loaded %d user(s) from the data repo into the local cache", n)
    m = sync_company_signals_from_repo()
    if m:
        log.info("Loaded %d company signal(s) from the data repo into the local cache", m)


@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    if API_TOKEN and request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        sent = request.headers.get("authorization", "")
        if not hmac.compare_digest(sent.encode(), f"Bearer {API_TOKEN}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=SECURITY_HEADERS)
    if request.url.path.startswith("/admin") and request.method != "OPTIONS":
        sent_admin = request.headers.get("x-admin-token", "")
        if not ADMIN_TOKEN:
            return JSONResponse({"error": "admin disabled: set ADMIN_TOKEN"}, status_code=403, headers=SECURITY_HEADERS)
        if not hmac.compare_digest(sent_admin.encode(), ADMIN_TOKEN.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=SECURITY_HEADERS)
    resp = await call_next(request)
    resp.headers.update(SECURITY_HEADERS)
    return resp


# CORS must be registered AFTER the auth middleware above: Starlette runs the last-added middleware
# outermost, so this order makes CORS wrap auth and its 401/403 responses carry Access-Control-Allow-Origin.
# Without it a browser blocks the 401, and the UI shows "server unavailable" instead of "wrong token".
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization", "X-Session-Token", "X-Admin-Token"],
)


# ── PER-USER AUTH (second layer, on top of API_TOKEN above) ───────────────────
# API_TOKEN gates the whole API from the public internet. This layer identifies WHICH authenticated
# user is calling, via a signed session token from POST /auth/login (see auth.py). Every endpoint that
# used to accept a client-supplied `user_id` field now takes it from this instead -- see CLAUDE.md
# "Multi-user" section: trusting a client-supplied user_id let anyone read/write any other user's data.
def current_user(x_session_token: str = Header(default="", alias="X-Session-Token")) -> str:
    user_id = auth.verify_session_token(x_session_token)
    if not user_id or not store.user_exists(user_id):  # a removed user's unexpired token stops working
        raise HTTPException(status_code=401, detail="missing or invalid session token; call /auth/login first")
    return user_id


def optional_user(x_session_token: str = Header(default="", alias="X-Session-Token")) -> str:
    """Like current_user(), but "" instead of a 401 when there's no valid session -- for /feedback,
    which the legacy unauthenticated /search pipeline's global feedback_store also still uses (see
    CLAUDE.md roadmap item 1: that global dict is kept for /search; per-user persistence is additive)."""
    user_id = auth.verify_session_token(x_session_token)
    return user_id if user_id and store.user_exists(user_id) else ""


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
    return Field(default="default", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_@-]+(\.[A-Za-z0-9_@-]+)*$")


class LoginParams(BaseModel):
    user_id:  str = UserId()
    passcode: str = Field(min_length=4, max_length=128)


class AddUserParams(BaseModel):
    user_id:  str = UserId()
    passcode: str = Field(min_length=4, max_length=128)


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
    max_years:         int | None = Field(default=None, ge=0, le=30)   # Agent: skip postings asking more; 0 = no limit


class AgentParams(BaseModel):
    # No user_id field: who this run is for comes from the verified session token (see current_user()
    # below), never from the client. A client-supplied user_id here would let anyone read/write any
    # other user's leads/profile just by naming their id.
    filters:      AgentFilters | None = None
    goal:         str  = Field(default="", max_length=500)
    resume_text:  str  = Field(default="", max_length=20000)
    min_fit:      int  = Field(default=40, ge=0, le=100)
    resume_years: int | None = Field(default=None, ge=0, le=60)
    hours:        int | None = Field(default=None, ge=1, le=720)   # overrides recency_hours (24 daily, 168 weekly)

class SheetExport(BaseModel):
    sheet_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")  # falls back to env SHEET_ID
    tab:      str = Field(default="Leads", min_length=1, max_length=100)
    # No user_id field -- see AgentParams comment above; same reasoning.
    since:    str = Field(default="", max_length=20)
    track:    Literal["", "ft", "c2c"] = ""   # "c2c" for the "C2C Leads" tab, "ft" for FT tabs
    hot_only: bool = True                     # only Hot Leads (score >= 65) go to the Sheet
    jobs:     list[dict] = Field(default=[], max_length=500)   # optional: export these instead of stored leads

class FeedbackPayload(BaseModel):
    url:      str = Field(max_length=2000)   # Job URL as unique ID
    title:    str = Short()
    company:  str = Short()
    signal:   Literal["up", "down", ""]


class OutreachParams(BaseModel):
    title:       str = Short()
    company:     str = Short()
    description: str = Field(default="", max_length=8000)
    resume_text: str = Field(default="", max_length=20000)
    tone:        Literal["recruiter", "hiring_manager"] = "recruiter"


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


LOGIN_MAX_FAILURES, LOGIN_WINDOW_S = 5, 900   # per user_id: 5 wrong passcodes -> locked 15 min
_login_failures: dict[str, list[float]] = {}


@app.post("/auth/login")
def login(p: LoginParams):
    """Verifies a passcode the owner created with backend/add_user.py and issues a signed session
    token (header X-Session-Token on every later request). No self-serve signup endpoint exists."""
    now = time.time()
    fails = [t for t in _login_failures.get(p.user_id, []) if now - t < LOGIN_WINDOW_S]
    if len(fails) >= LOGIN_MAX_FAILURES:
        return JSONResponse({"error": "too many attempts; try again later"}, status_code=429)
    if not store.verify_user(p.user_id, p.passcode):
        _login_failures[p.user_id] = fails + [now]
        if len(_login_failures) > 10_000:
            _login_failures.pop(next(iter(_login_failures)))  # bounded
        return JSONResponse({"error": "invalid user_id or passcode"}, status_code=401)
    _login_failures.pop(p.user_id, None)
    token = auth.make_session_token(p.user_id)
    return {"token": token, "user_id": p.user_id, "expires_days": auth.SESSION_DAYS}


@app.post("/admin/add-user")
def admin_add_user(p: AddUserParams):
    """Creates a tester or rotates their passcode. Owner-only: this endpoint is protected by the separate
    ADMIN_TOKEN (header X-Admin-Token, see SECURITY above), never by API_TOKEN -- testers may hold
    API_TOKEN, so it can't be the admin password, and this can't be used for self-serve signup, matching backend/add_user.py's own intent.
    Exists because Render's free plan has no Shell access to run that CLI script directly on the
    deployed instance; call this instead, e.g.:
      curl -X POST https://<your-render-url>/admin/add-user \\
        -H "Authorization: Bearer <API_TOKEN>" -H "X-Admin-Token: <ADMIN_TOKEN>" -H "Content-Type: application/json" \\
        -d '{"user_id": "...", "passcode": "..."}'
    """
    existed = store.user_exists(p.user_id)
    store.create_user(p.user_id, p.passcode)
    return {"created": not existed, "reset": existed, "user_id": p.user_id,
            "synced_to_data_repo": data_repo.enabled()}


@app.get("/admin/users")
def admin_list_users():
    """Lists all testers (user_id + created_at, no passcode data). Same owner-only API_TOKEN gate."""
    return {"users": store.list_users()}


@app.delete("/admin/users/{user_id}")
def admin_remove_user(user_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_@-]+(\.[A-Za-z0-9_@-]+)*$")]):
    """Removes a tester's account and all their data (leads, seen, profile), locally and in the private
    data repo when configured. Same owner-only API_TOKEN gate as the rest of /admin/*."""
    existed = store.delete_user(user_id)
    if not existed:
        return JSONResponse({"error": f"no such user '{user_id}'"}, status_code=404)
    return {"removed": True, "user_id": user_id}


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


def _run_agent(params: AgentParams, user_id: str) -> dict:
    rules = apply_filters(load_rules(user_id=user_id), params.filters.model_dump() if params.filters else None)
    if params.hours:
        rules["profile"]["recency_hours"] = params.hours
    run = AgentRun(params.resume_text, user_id, rules=rules, min_fit=params.min_fit, resume_years=params.resume_years)
    res = run.run(params.goal)
    for l in res["leads"]:  # the UI doesn't need full JDs; keep responses small
        l.pop("description", None)
    return res


@app.post("/agent/run")
def agent_run(params: AgentParams, user_id: str = Depends(current_user)):
    """Synchronous agent run (CLI/tests). The UI uses /agent/start + /agent/jobs/{id} instead."""
    if not _agent_lock.acquire(blocking=False):
        return JSONResponse({"error": "an agent run is already in progress", "leads": []}, status_code=429)
    try:
        return JSONResponse(_run_agent(params, user_id))
    except Exception:
        log.exception("agent run failed")
        return JSONResponse({"error": "agent run failed (see server log)", "leads": []}, status_code=500)
    finally:
        _agent_lock.release()


@app.post("/agent/start")
def agent_start(params: AgentParams, user_id: str = Depends(current_user)):
    """Start an agent run in the background (runs take minutes; free hosts drop long HTTP requests).
    When DATA_REPO_TOKEN/DATA_REPO are configured (see data_repo.py / docs/MULTI_USER_SETUP.md), the
    actual scrape is dispatched to the private data repo's GitHub Actions workflow instead of running
    here in-process -- Render then only triggers + polls it. Unconfigured (the default), this runs
    exactly as it always has."""
    if data_repo.enabled():
        rules = apply_filters(load_rules(user_id=user_id), params.filters.model_dump() if params.filters else None)
        if params.hours:
            rules["profile"]["recency_hours"] = params.hours
        dispatch = data_repo.trigger_run(user_id, rules, params.resume_text, params.min_fit, params.hours)
        if not dispatch["ok"]:
            return JSONResponse({"error": dispatch["error"]}, status_code=502)
        job_id = f"remote:{uuid.uuid4().hex}"
        for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:
            _jobs.pop(k, None)
        _jobs[job_id] = {"status": "running", "started": time.time(), "result": None, "user_id": user_id,
                          "remote": {"user_id": user_id, "dispatched_at": dispatch["dispatched_at"], "run_id": None}}
        return {"job_id": job_id, "status": "running"}

    if not _agent_lock.acquire(blocking=False):
        running = next((k for k, v in _jobs.items() if v["status"] == "running"), None)
        return JSONResponse({"error": "an agent run is already in progress", "job_id": running}, status_code=429)
    job_id = uuid.uuid4().hex
    for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:  # keep the last 20 results only
        _jobs.pop(k, None)
    _jobs[job_id] = {"status": "running", "started": time.time(), "result": None, "user_id": user_id}

    def work():
        try:
            _jobs[job_id].update(status="done", result=_run_agent(params, user_id))
        except Exception:
            log.exception("agent job failed")
            _jobs[job_id].update(status="error", result={"error": "agent run failed (see server log)", "leads": []})
        finally:
            _agent_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/profile/filters")
def get_profile_filters(user_id: str = Depends(current_user)):
    """Defaults for the UI's Search filters panel (this user's saved profile, or the example profile
    if they have none yet). Requires the token like any endpoint, plus a valid session."""
    return profile_filters(load_rules(user_id=user_id))


@app.get("/agent/jobs/{job_id}")
def agent_job(job_id: str, user_id: str = Depends(current_user)):
    j = _jobs.get(job_id)
    if not j or j.get("user_id") != user_id:  # don't let one user poll/read another's job by id
        return JSONResponse({"error": "unknown job"}, status_code=404)
    if job_id.startswith("remote:") and j["status"] == "running":
        remote = j["remote"]
        if remote["run_id"] is None:
            run = data_repo.find_run_after(remote["dispatched_at"], timeout_s=5)
            if run:
                remote["run_id"] = run["id"]
        if remote["run_id"] is not None:
            st = data_repo.run_status(remote["run_id"])
            if st and st["status"] == "completed":
                if st["conclusion"] == "success":
                    leads = data_repo.fetch_user_leads(remote["user_id"])
                    j.update(status="done", result={"leads": leads, "new_count": None,
                                                      "summary": f"remote run: {len(leads)} total leads"})
                else:
                    j.update(status="error",
                              result={"error": f"remote run {st['conclusion'] or 'failed'} (see the private "
                                                "repo's Actions tab for logs)", "leads": []})
    return {"job_id": job_id, "status": j["status"], "elapsed_s": int(time.time() - j["started"]), "result": j["result"]}


@app.get("/leads")
def leads(since: str = "", track: str = "", format: str = "json", user_id: str = Depends(current_user)):
    """All saved leads for the calling user. format=json | csv | xlsx (xlsx/csv open directly in Google Sheets).
    Prefers the private data repo's copy when DATA_REPO_TOKEN/DATA_REPO are configured (that's the durable
    source of truth once set up); falls back to local SQLite otherwise -- see data_repo.py."""
    if format not in ("json", "csv", "xlsx") or track not in ("", "ft", "c2c") or len(since) > 20:
        return JSONResponse({"error": "bad parameters"}, status_code=400)
    rows = data_repo.fetch_user_leads(user_id) if data_repo.enabled() else store.list_leads(user_id, since, track)
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
def export_sheets(p: SheetExport, user_id: str = Depends(current_user)):
    """Append leads to the user's Google Sheet (append-only, dedupes by URL)."""
    try:
        rows = p.jobs or (data_repo.fetch_user_leads(user_id) if data_repo.enabled() else store.list_leads(user_id, p.since, p.track))
        if p.hot_only:  # docs/CONTEXT.md: only Hot Leads go to the Sheet; the rest stay in the run log
            rows = [r for r in rows if r.get("hot_lead")]
        sheet_id = p.sheet_id or os.getenv("SHEET_ID", "")
        if not sheet_id:
            raise RuntimeError("sheet_id not given and SHEET_ID not set")
        return append_to_sheet(rows, sheet_id, p.tab)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/feedback")
def feedback(payload: FeedbackPayload, user_id: str = Depends(optional_user)):
    """
    Stores thumbs up/down for a job.
    'up'   → job will be boosted in future results
    'down' → job will be demoted / hidden
    The legacy global feedback_store (server-lifetime only) keeps driving the old /search pipeline's
    ai_service.apply_feedback_boost exactly as before. When the caller has a valid session (the Full
    Search UI always does post-login), the same signal is also persisted per-user in store.py and feeds
    fit_service.score_lead via AgentRun.feedback_signals -- roadmap item 1: feedback that actually
    changes future Full Search scoring, not just the legacy pipeline.
    """
    if not user_id:  # anonymous legacy /search caller only: a signed-in user's thumbs never leak into others' ranking
        if payload.signal:
            if payload.url not in feedback_store and len(feedback_store) >= 5000:
                feedback_store.pop(next(iter(feedback_store)))  # bounded: oldest entry out
            feedback_store[payload.url] = payload.signal
        else:
            feedback_store.pop(payload.url, None)
    else:
        store.save_feedback(user_id, payload.url, payload.company, payload.title, payload.signal)
    return {"stored": True, "url": payload.url, "signal": payload.signal}


@app.post("/outreach/draft")
def outreach_draft(p: OutreachParams, user_id: str = Depends(current_user)):
    """Drafts (never sends) a short outreach note for one lead -- roadmap item 7. BYO LLM key (see
    CLAUDE.md), falls back to a template with zero network calls when none is configured. The user
    copies this and sends it themselves; this project never submits applications or sends messages for
    anyone. `description` is optional -- leads returned to the UI have it stripped to keep payloads
    small (see the 2026-11-16 OOM fix), so a draft from the UI today is title/company/resume only."""
    try:
        return outreach.draft_outreach({"title": p.title, "company": p.company, "description": p.description},
                                        p.resume_text, p.tone)
    except Exception:
        log.exception("outreach draft failed")
        return JSONResponse({"error": "could not draft outreach (see server log)"}, status_code=500)


# ── AGENT MODE (Agent v1, the "Agent" tab; see agent_mode.py) ─────────────────────────────────────
# Separate from /agent/* (= the Full Search pipeline, deliberately not renamed). Every route takes
# user_id from the verified session (current_user), never from the client; approval ids and URLs are
# looked up WHERE user_id = <session user>, so another user's ids behave exactly like missing ones.
_agentmode_running: set[str] = set()
_agentmode_lock = threading.Lock()


class AgentModeSettings(BaseModel):
    enabled:        bool | None = None
    interval_hours: Literal[6, 12, 24, 48, 72, 168] | None = None
    notes:          str | None = Field(default=None, max_length=2000)   # plain-sentence preferences for the LLM
    resume_text:    str | None = Field(default=None, max_length=20000)
    filters:        AgentFilters | None = None   # the Agent's own titles/locations/etc. (same shape as Search filters)
    clear_filters:  bool = False                 # True = go back to the profile's defaults


class AgentModeRun(BaseModel):
    wait: bool = False   # True = run synchronously (tests/CLI); the UI runs in the background and polls /state


class LeadStatusParams(BaseModel):
    url:     str = Field(min_length=1, max_length=2000)
    status:  Literal["New", "Applied", "Interview", "Rejected", "Skipped"]
    title:   str = Short()
    company: str = Short()


class KitSkip(BaseModel):
    url:    str = Field(min_length=1, max_length=2000)
    reason: Literal["not_for_me", "too_senior", "company"] = "not_for_me"


class HideCompany(BaseModel):
    companies: list[Phrase] = Field(min_length=1, max_length=10)


class ApprovalDecision(BaseModel):
    decision: Literal["accept", "reject"]


class WatchlistAdd(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    ats:     Literal["", "greenhouse", "lever", "ashby"] = ""
    slug:    str = Field(default="", max_length=60, pattern=r"^[A-Za-z0-9_.-]*$")


_agentmode_loaded: set[str] = set()
_sync_pending: set[str] = set()       # users with a push queued or in flight
_sync_dirty: set[str] = set()         # a write arrived while the push was in flight: push again
_sync_failed: set[str] = set()        # last push failed: local SQLite is newer than the repo, never pull over it
_sync_lock = threading.Lock()
_sync_threads: set[threading.Thread] = set()
_user_locks: dict[str, threading.Lock] = {}
_PUSH_BACKOFF = 2   # seconds; tests set 0


def _ulock(user_id: str) -> threading.Lock:
    """One lock per user: a pull (GitHub GET + import over SQLite) and a write never interleave."""
    with _sync_lock:
        return _user_locks.setdefault(user_id, threading.Lock())


def agent_writer(user_id: str = Depends(current_user)):
    """Dependency for every /agentmode write: holds the user's lock for the request, after loading the durable
    state once (Render wipes SQLite on deploy), so a concurrent /state pull can't import over this write."""
    lock = _ulock(user_id)
    lock.acquire()
    try:
        _agentmode_load(user_id)
        yield user_id
    finally:
        lock.release()


def _push_once(user_id: str) -> bool:
    if not data_repo.enabled():
        return True
    with _ulock(user_id):   # consistent snapshot; the slow PUT happens outside the lock
        data = store.export_agent_state(user_id)
    for attempt in range(3):
        if data_repo.write_json(agent_mode.REPO_PATH.format(user_id=user_id), data, f"Update agent state for {user_id}"):
            return True
        time.sleep(_PUSH_BACKOFF * (attempt + 1))
    return False


def _agentmode_sync_later(user_id: str) -> None:
    """Push this user's agent state to the data repo in the background, so a click returns at SQLite speed
    instead of waiting ~1-3 s on GitHub. Writes that arrive mid-push are coalesced into one more push. A push
    that keeps failing leaves the user in _sync_failed: /state then retries the push instead of pulling the
    older repo copy over the newer local state. Shutdown waits for in-flight pushes (see _drain_pushes)."""
    with _sync_lock:
        if user_id in _sync_pending:
            _sync_dirty.add(user_id)
            return
        _sync_pending.add(user_id)

    def push():
        ok = False
        try:
            while True:
                try:
                    ok = _push_once(user_id)
                except Exception:
                    ok = False
                    log.exception("agent state sync failed")
                with _sync_lock:
                    if user_id not in _sync_dirty:
                        (_sync_failed.discard if ok else _sync_failed.add)(user_id)
                        if not ok:
                            log.warning("agent state for %s not pushed; will retry on the next /state", user_id)
                        return
                    _sync_dirty.discard(user_id)
        finally:
            with _sync_lock:
                _sync_pending.discard(user_id); _sync_dirty.discard(user_id)
                _sync_threads.discard(threading.current_thread())
    t = threading.Thread(target=push, daemon=True)
    with _sync_lock:
        _sync_threads.add(t)
    t.start()


@app.on_event("shutdown")
def _drain_pushes():
    """Render sends SIGTERM on deploy and then wipes the disk: finish queued pushes first (bounded)."""
    deadline = time.time() + 25
    for t in list(_sync_threads):
        t.join(max(0, deadline - time.time()))


def _agentmode_load(user_id: str) -> None:
    """Render's disk is wiped on every deploy: pull this user's agent state from the data repo before any
    write, or a write on a fresh instance would sync an EMPTY state back over the durable copy."""
    if user_id not in _agentmode_loaded and user_id not in _agentmode_running:
        agent_mode.sync_from_repo(user_id)   # no-op unless the private data repo is configured
        _agentmode_loaded.add(user_id)


def _scoring_info(user_id: str, filters_json) -> dict:
    """Maximum points per Match part and the HOT threshold for this user's profile (the UI's breakdown)."""
    try:
        if isinstance(filters_json, dict):
            filters_json = json.dumps(filters_json)
        sc = agent_mode.rules_for(user_id, filters_json)["scoring"]
        ind = [t.get("points", 0) for t in sc.get("industry_priority", [])] + [sc.get("opportunistic_points", 0), 15]
        return {"max": {"h1b": max(sc["h1b_points"].values()), "title": 25, "geo": 15, "industry": max(ind),
                        "contact": max(list(sc.get("contact_points", {}).values()) or [10])},
                "hot": sc.get("hot_threshold", 65)}
    except Exception:
        return {"max": {"h1b": 35, "title": 25, "geo": 15, "industry": 15, "contact": 10}, "hot": 65}


@app.get("/agentmode/state")
def agentmode_state(user_id: str = Depends(current_user)):
    """Everything the Agent tab shows: on-duty settings, Today brief, kits, activity, approvals, watchlist,
    per-lead statuses and the set of agent-surfaced URLs (for the "Agent" badge in both tabs)."""
    with _ulock(user_id):
        with _sync_lock:
            unpushed = user_id in _sync_pending or user_id in _sync_failed
            retry = user_id in _sync_failed and user_id not in _sync_pending
        if user_id not in _agentmode_running and not unpushed:
            agent_mode.sync_from_repo(user_id)   # picks up kits written by scheduled runs (never over an unpushed write)
            _agentmode_loaded.add(user_id)
        st = agent_mode.public_state(user_id)
    if retry:
        _agentmode_sync_later(user_id)
    st["running"] = user_id in _agentmode_running
    st["scoring"] = _scoring_info(user_id, (st.get("settings") or {}).get("filters"))
    return st


_SKIP_KEYS = ("exclude_companies", "max_years")


@app.post("/agentmode/settings")
def agentmode_settings(p: AgentModeSettings, user_id: str = Depends(agent_writer)):
    fields = {k: v for k, v in p.model_dump(exclude={"filters", "clear_filters"}).items() if v is not None}
    cur = store.get_agent_settings(user_id)
    cur_f = agent_mode._json_or_none(cur.get("filters")) or {}
    # skip rules (hidden companies, max years) live in the same filters blob but are edited on their own: saving
    # targets or going back to the profile keeps them; a key the request leaves out keeps its saved value
    keep = {k: cur_f[k] for k in _SKIP_KEYS if cur_f.get(k)}
    if p.clear_filters:
        fields["filters"] = json.dumps(keep) if keep else ""
    elif p.filters is not None:
        new_f = {**cur_f, **p.filters.model_dump(exclude_none=True)}
        new_f = {k: v for k, v in new_f.items() if not (k in _SKIP_KEYS and not v)}   # [] / 0 clears a skip rule
        fields["filters"] = json.dumps(new_f) if new_f else ""
    if fields.get("enabled") and not cur["enabled"]:
        fields["next_run"] = ""   # due immediately once switched on
    if fields.get("enabled") is False:
        fields["next_run"] = ""
    # the scheduled workflow has no access to Render's profile table: snapshot the resolved profile now
    snap = agent_mode.rules_for(user_id, fields["filters"] if "filters" in fields else cur.get("filters"))
    snap.pop("researched", None)
    fields["rules_snapshot"] = json.dumps(snap)
    store.save_agent_settings(user_id, **fields)
    pruned = agent_mode.prune_kits(user_id, snap) if "filters" in fields else 0
    if pruned and user_id not in _agentmode_running:
        agent_mode.refill(user_id, min(pruned, 3))   # a stricter rule shouldn't leave Today's picks empty
    _agentmode_sync_later(user_id)
    out = agent_mode.public_state(user_id)["settings"]
    out["pruned"] = pruned
    return out


def _snapshot_rules(user_id: str) -> None:
    snap = agent_mode.rules_for(user_id, store.get_agent_settings(user_id).get("filters"))
    snap.pop("researched", None)
    store.save_agent_settings(user_id, rules_snapshot=json.dumps(snap))


@app.post("/agentmode/kits/skip")
def agentmode_skip(p: KitSkip, user_id: str = Depends(agent_writer)):
    """"Not for me" on a pick: marks it Skipped, removes the kit (with reason "company": every kit from that
    company, which is also hidden from future runs), and returns replacement kits from the saved leads."""
    with _agentmode_lock:
        if user_id in _agentmode_running:
            return JSONResponse({"error": "the agent is running; try again when it finishes"}, status_code=429)
    res = agent_mode.skip_kit(user_id, p.url, p.reason)
    if res is None:
        return JSONResponse({"error": "no such apply kit"}, status_code=404)
    if p.reason == "company":
        _snapshot_rules(user_id)
    _agentmode_sync_later(user_id)
    return res


@app.post("/agentmode/hide-company")
def agentmode_hide_company(p: HideCompany, user_id: str = Depends(agent_writer)):
    """Never pick or suggest this company (Suggestions' "Hide" button). Replacement kits come back like a skip."""
    with _agentmode_lock:
        if user_id in _agentmode_running:
            return JSONResponse({"error": "the agent is running; try again when it finishes"}, status_code=429)
    res = agent_mode.hide_company(user_id, p.companies)
    res["added"] = agent_mode.refill(user_id, min(len(res["removed"]), 3))
    _snapshot_rules(user_id)
    _agentmode_sync_later(user_id)
    return res


@app.post("/agentmode/run")
def agentmode_run(p: AgentModeRun, user_id: str = Depends(agent_writer)):
    """Run one agent cycle now (drafts only; never applies or sends anything)."""
    with _agentmode_lock:
        if user_id in _agentmode_running:
            return JSONResponse({"error": "an agent cycle is already running"}, status_code=429)
        _agentmode_running.add(user_id)

    def work():
        try:
            res = agent_mode.run_cycle(user_id)
            _agentmode_sync_later(user_id)   # queued with any click-time pushes, so two pushes never race
            return res
        finally:
            _agentmode_running.discard(user_id)

    if p.wait:
        try:
            res = work()
            return {"status": "done", "cycle_id": res["cycle_id"], "kits": len(res["picks"]), "decided_by": res["decided_by"]}
        except Exception:
            log.exception("agent cycle failed")
            return JSONResponse({"error": "agent cycle failed (see server log)"}, status_code=500)

    def bg():
        try:
            work()
        except Exception:
            log.exception("agent cycle failed")
    threading.Thread(target=bg, daemon=True).start()
    return {"status": "running"}


@app.post("/agentmode/status")
def agentmode_status(p: LeadStatusParams, user_id: str = Depends(agent_writer)):
    """Per-lead status (New/Applied/Interview/Rejected/Skipped), shared by both tabs. Feeds the same memory as
    thumbs (store.feedback_signal_counts): Applied/Interview count as up, Skipped as down."""
    res = store.set_lead_status(user_id, p.url, p.status, p.company, p.title)
    _agentmode_sync_later(user_id)
    return res


@app.post("/agentmode/approvals/accept-all")
def agentmode_accept_all(user_id: str = Depends(agent_writer)):
    """One click for the whole pending batch (owner decision 8: the first watchlist is approved as one batch)."""
    # watchlist additions only: anything that changes the agent's plan still needs a one-by-one decision
    done = [r for a in store.list_approvals(user_id, "pending")
            if a["kind"] == "watchlist_add" and (r := store.decide_approval(user_id, a["id"], True))]
    if done:
        store.add_activity(user_id, "", "approval", "You accepted adding " +
                           ", ".join(r["payload"].get("company", "?") for r in done) + " to the watchlist.")
        _agentmode_sync_later(user_id)
    return {"accepted": len(done)}


@app.post("/agentmode/approvals/{approval_id}")
def agentmode_decide(approval_id: Annotated[int, Path(ge=1)], p: ApprovalDecision, user_id: str = Depends(agent_writer)):
    res = store.decide_approval(user_id, approval_id, p.decision == "accept")
    if not res:
        return JSONResponse({"error": "no such pending approval"}, status_code=404)
    store.add_activity(user_id, "", "approval", f"You {res['status']} adding {res['payload'].get('company', '?')} to the watchlist.")
    _agentmode_sync_later(user_id)
    return res


@app.post("/agentmode/watchlist")
def agentmode_watch_add(p: WatchlistAdd, user_id: str = Depends(agent_writer)):
    store.add_watchlist(user_id, p.company, p.ats, p.slug, "manual")
    _agentmode_sync_later(user_id)
    return {"watchlist": store.list_watchlist(user_id)}


@app.delete("/agentmode/watchlist/{company}")
def agentmode_watch_remove(company: Annotated[str, Path(min_length=1, max_length=100)], user_id: str = Depends(agent_writer)):
    if not store.remove_watchlist(user_id, company):
        return JSONResponse({"error": "not on your watchlist"}, status_code=404)
    _agentmode_sync_later(user_id)
    return {"watchlist": store.list_watchlist(user_id)}


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
