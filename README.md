# JobFinder v2

## Structure
```
backend/
  main.py            ← FastAPI endpoints
  scraper_service.py ← JSearch, Adzuna, Remotive
  filter_service.py  ← Keywords, H1B, location, recency
  ai_service.py      ← Claude AI H1B + resume match scoring
  export_service.py  ← CSV download
  requirements.txt
frontend/
  index.html         ← Open directly in browser
```

## Run
```bash
cd backend
source ~/jobfinder_env/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open `frontend/index.html` in browser.

## API Keys (all free)

| Key | Where to get | Set in |
|-----|-------------|--------|
| JSearch | rapidapi.com → search "JSearch" → free tier | `scraper_service.py` line 8 |
| Adzuna | adzuna.com/api | `scraper_service.py` lines 9-10 |
| Claude AI | console.anthropic.com → API Keys | `ai_service.py` line 12 |

Remotive works with zero keys — good for testing immediately.

## Layers
- **No keys**: Remotive only (remote PM jobs, instant)
- **+ JSearch**: LinkedIn + Indeed + Glassdoor coverage
- **+ Adzuna**: Broader US market
- **+ Claude AI**: Smart H1B scoring + resume match % per job
