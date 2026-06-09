# JobFinder v3 - Setup & Run Guide

## Quick Start (5 minutes)

### Prerequisites
- Python 3.11+ (tested with 3.11)
- macOS/Linux/Windows with bash

### Step 1: Install Backend
```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2: Start Backend
```bash
source venv/bin/activate  # (if not already activated)
uvicorn main:app --host 127.0.0.1 --port 8000
```

You should see:
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### Step 3: Open Frontend
Open `frontend/index.html` in your web browser. The app is ready to use!

---

## Features Working Out of the Box

✅ **Remotive Job Scraping** - Free, no API keys needed
- Remote-only jobs
- Product category focus
- ~50-100 jobs available at any time

✅ **Job Filtering**
- Filter by keywords (flexible matching)
- Exclude keywords (e.g., "intern", "junior")
- Filter by location
- Filter by recency (last hour/day/week)

✅ **H1B Analysis**
- Detects visa sponsorship keywords in job descriptions
- Shows H1B likelihood: Yes/No/Unknown

✅ **Feedback System**
- Mark jobs with 👍 (like) or 👎 (dislike)
- Preferences persist during session
- Re-ranks results based on feedback

✅ **CSV Export**
- Download filtered results as CSV
- Includes all job details and scores

✅ **Resume Parsing**
- Upload PDF resume
- Extracts keywords automatically
- Auto-fills search keywords from resume

✅ **Usage Tracking**
- Monitor daily API call limits
- See remaining calls for each source

---

## API Endpoints

### GET /
Service health check
```bash
curl http://localhost:8000/
```

### GET /health
Health status
```bash
curl http://localhost:8000/health
```

### POST /search
Search for jobs
```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": ["product manager", "data scientist"],
    "exclude": ["intern", "junior"],
    "locations": ["remote", "san francisco"],
    "sources": ["remotive"],
    "h1b_only": false,
    "use_ai": false,
    "hours": 0,
    "resume_text": ""
  }'
```

**Response:**
```json
{
  "count": 10,
  "jobs": [
    {
      "title": "Staff Product Engineer",
      "company": "Example Corp",
      "location": "Remote",
      "posted": "2026-06-08",
      "source": "Remotive",
      "h1b_friendly": "Unknown",
      "url": "https://...",
      ...
    }
  ],
  "usage": {"remotive": 1},
  "limits": {"remotive": 999, ...}
}
```

### POST /export
Export search results as CSV
```bash
curl -X POST http://localhost:8000/export \
  -H "Content-Type: application/json" \
  -d '{...same as /search...}' \
  > jobs.csv
```

### POST /feedback
Give feedback on a job
```bash
curl -X POST http://localhost:8000/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://remotive.com/remote-jobs/...",
    "title": "Staff Product Engineer",
    "company": "Example Corp",
    "signal": "up"
  }'
```

Signals: `"up"` (👍 like) or `"down"` (👎 dislike)

### POST /parse-resume
Extract keywords from PDF resume
```bash
curl -X POST http://localhost:8000/parse-resume \
  -F "file=@resume.pdf"
```

**Response:**
```json
{
  "keywords": ["product manager", "data", "sql", "python"],
  "resume_text": "..."
}
```

### GET /usage
Check daily API usage
```bash
curl http://localhost:8000/usage
```

**Response:**
```json
{
  "date": "2026-06-08",
  "usage": {"remotive": 5, "jsearch": 0, "adzuna": 0, "claude": 0},
  "limits": {"remotive": 999, "jsearch": 6, "adzuna": 16, "claude": 50},
  "remaining": {"remotive": 994, "jsearch": 6, "adzuna": 16, "claude": 50}
}
```

---

## Optional: Enable Paid API Sources

### JSearch (LinkedIn + Indeed + Glassdoor)
1. Go to [RapidAPI.com](https://rapidapi.com)
2. Search for "JSearch"
3. Subscribe to free tier
4. Copy API key
5. Edit `backend/scraper_service.py` line 9
6. Replace `JSEARCH_KEY = "YOUR_JSEARCH_KEY"` with your key
7. Free tier: ~6 calls/day

### Adzuna (US Job Market)
1. Go to [Adzuna API](https://developer.adzuna.com)
2. Sign up for free tier
3. Get your ID and key
4. Edit `backend/scraper_service.py` lines 10-11
5. Free tier: ~16 calls/day

### Claude AI Scoring
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create API key
3. Edit `backend/ai_service.py` line 9
4. Replace `ANTHROPIC_KEY = "YOUR_ANTHROPIC_API_KEY"` with your key
5. Enables:
   - Smart H1B scoring from job descriptions
   - Resume-to-job match percentage (0-100)
6. Free credits: ~$5 (enough for 1000s of scores)

---

## Troubleshooting

### "Backend offline" message in browser
- Check backend is running: `curl http://localhost:8000/health`
- If not running, start it: `cd backend && source venv/bin/activate && uvicorn main:app --host 127.0.0.1 --port 8000`

### No jobs appearing in search
- Remotive sometimes has limited stock; try different keywords
- Try shorter keywords: "product" instead of "product manager"
- Use "Any time" filter instead of "Last 24 hours"

### "ModuleNotFoundError" when starting backend
- Make sure virtual environment is activated: `source venv/bin/activate`
- Make sure requirements.txt was installed: `pip install -r requirements.txt`

### CSV export is empty
- Run a search first to populate results
- Check count shows "X jobs found"

### Resume upload not working
- Make sure file is PDF format
- File should be readable PDF (not image-based)
- Try smaller resume (~5 pages max)

---

## File Structure

```
jobfinder_v3/
├── backend/
│   ├── main.py                 # FastAPI app + endpoints
│   ├── scraper_service.py      # Job scrapers (JSearch, Adzuna, Remotive)
│   ├── filter_service.py       # Job filtering logic
│   ├── ai_service.py           # Claude AI scoring
│   ├── export_service.py       # CSV export
│   ├── requirements.txt        # Python dependencies
│   └── venv/                   # Virtual environment (created by you)
├── frontend/
│   └── index.html              # Single-page web app (open in browser)
└── README.md                   # Original documentation
```

---

## Architecture

```
Frontend (index.html)
    ↓ (HTTP requests)
FastAPI Server (port 8000)
    ├─ Scraper Service (fetches jobs)
    ├─ Filter Service (filters & scores jobs)
    ├─ AI Service (Claude API optional)
    └─ Storage (in-memory)
    ↓
Job APIs:
    ├─ Remotive (free, always available)
    ├─ JSearch (paid, optional)
    └─ Adzuna (paid, optional)
```

---

## Development Notes

### Adding New Job Sources
1. Create `fetch_newsource()` function in `scraper_service.py`
2. Return list of normalized job dicts
3. Add source toggle to frontend if desired
4. Update daily limits in `main.py`

### Modifying Filters
Edit `filter_service.py`:
- `filter_keywords()` - keyword matching logic
- `filter_location()` - location filtering
- `filter_recency()` - time-based filtering
- `score_h1b()` - H1B detection

### Customizing Search Defaults
Edit `frontend/index.html`:
- Line 151: default keywords
- Line 153: default exclusions
- Line 235: default sources

---

## Performance

- **Search time**: 2-5 seconds (depends on Remotive API)
- **CSV export**: <1 second
- **Resume parsing**: 1-3 seconds (depends on file size)
- **AI scoring**: 10-30 seconds (depends on Claude API)

---

## Daily Limits

| Source | Limit | Reset |
|--------|-------|-------|
| Remotive | 999 | N/A (unlimited) |
| JSearch | 6 | Daily (UTC) |
| Adzuna | 16 | Daily (UTC) |
| Claude AI | 50 | Daily (UTC) |

Limits are tracked by date and reset automatically at UTC midnight.

---

## Support

For issues or questions:
1. Check `FIXES_COMPLETED.md` for known resolutions
2. Check error messages in browser console (F12)
3. Check backend logs in terminal
4. Verify all API keys are set if using paid sources
