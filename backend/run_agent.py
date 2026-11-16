# ── CLI RUNNER ───────────────────────────────────────────────────────────────
# Unattended run (cron / GitHub Actions / laptop):
#   python run_agent.py --resume resume.txt --hours 24 [--sheet SHEET_ID] [--track ft|c2c]
# Writes out/leads_<date>.csv + .xlsx; appends to Google Sheet if --sheet and creds are set.

import argparse, os, datetime, json
import pdfplumber
from agent_service import AgentRun
from export_service import to_csv, to_xlsx, append_to_sheet
from rules_service import load_rules


def read_resume(path: str) -> str:
    if not path or not os.path.exists(path):
        return os.getenv("RESUME_TEXT", "")
    if path.lower().endswith(".pdf"):
        with pdfplumber.open(path) as pdf:
            return " ".join(p.extract_text() or "" for p in pdf.pages)
    return open(path).read()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", default=os.getenv("RESUME_PATH", ""))
    ap.add_argument("--hours", type=int, default=0, help="override recency window")
    ap.add_argument("--sheet", default=os.getenv("SHEET_ID", ""))
    ap.add_argument("--min-fit", type=int, default=40)
    ap.add_argument("--goal", default="")
    a = ap.parse_args()

    rules = load_rules()
    if a.hours:
        rules["profile"]["recency_hours"] = a.hours
    res = AgentRun(read_resume(a.resume), rules=rules, min_fit=a.min_fit).run(a.goal)

    os.makedirs("out", exist_ok=True)
    stamp = datetime.date.today().isoformat()
    open(f"out/leads_{stamp}.csv", "w").write(to_csv(res["leads"]))
    open(f"out/leads_{stamp}.xlsx", "wb").write(to_xlsx(res["leads"]))
    if a.sheet and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        try:
            print(append_to_sheet(res["leads"], a.sheet))
        except Exception as e:
            print("sheet append failed:", e)
    print(json.dumps({"summary": res["summary"], "new": res["new_count"], "stats": res["stats"]}, indent=1))
