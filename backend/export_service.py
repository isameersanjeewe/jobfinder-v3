# ── EXPORT SERVICE ───────────────────────────────────────────────────────────
# Converts job results to downloadable formats.

import csv, io

def to_csv(jobs: list[dict]) -> str:
    """Returns CSV string — streamed as file download by FastAPI."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_rows(jobs))
    return out.getvalue()


# Tracker columns match filters.md "What gets logged" + fit details.
FIELDS = ["scraped", "title", "company", "location", "posted", "company_h1b", "posting_visa",
          "score", "hot_lead", "fit", "missing", "source", "url", "status", "h1b_friendly", "ai_h1b", "match"]


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(v, safe: bool = True):
    """Scraped text is untrusted: a title like '=HYPERLINK(...)' or '=IMPORTXML(...)' would run as a formula
    in Excel/Sheets (CSV/formula injection). Prefix with ' so it stays text."""
    if safe and isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


def _safe_url(u) -> str:
    return u if isinstance(u, str) and u.lower().startswith(("https://", "http://")) else ""


def _rows(jobs: list[dict], safe: bool = True) -> list[dict]:
    rows = []
    for j in jobs:
        r = {k: j.get(k, "") for k in FIELDS}
        r["missing"] = ", ".join(j.get("missing", [])) if isinstance(j.get("missing"), list) else j.get("missing", "")
        r["status"] = j.get("status") or "Not Applied"
        r["url"] = _safe_url(r["url"])
        rows.append({k: _cell(v, safe) for k, v in r.items()})
    return rows


def to_xlsx(jobs: list[dict]) -> bytes:
    """Excel file; opens in Google Sheets via File > Import with formatting intact."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(FIELDS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in _rows(jobs):
        ws.append([r[k] for k in FIELDS])
        url_cell = ws.cell(row=ws.max_row, column=FIELDS.index("url") + 1)
        if url_cell.value:
            url_cell.hyperlink = url_cell.value
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def append_to_sheet(jobs: list[dict], sheet_id: str, tab: str = "Leads") -> dict:
    """
    Appends to an existing Google Sheet (never recreates — search-process.md rule).
    Free setup: Google Cloud service account -> JSON key in env GOOGLE_SERVICE_ACCOUNT_JSON,
    then share the sheet with the service account email as Editor.
    Skips URLs already in the tab.
    """
    import json, os, gspread
    creds = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not creds:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    if not creds.startswith("{"):  # a path to the key file (local dev) instead of the JSON itself
        with open(os.path.expanduser(creds)) as f:
            creds = f.read()
    gc = gspread.service_account_from_dict(json.loads(creds))
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=len(FIELDS))
        ws.append_row(FIELDS)
    header = ws.row_values(1) or FIELDS
    rows = sheet_rows(jobs, header)
    link_col = next((i for i, h in enumerate(header) if _key(h) in _ALIASES["url"]), None)
    if link_col is None:
        raise RuntimeError(f"tab '{tab}' has no job-link column to dedupe on: {header}")
    existing = set(ws.col_values(link_col + 1))
    new_rows = [r for r in rows if r[link_col] and r[link_col] not in existing]
    if new_rows:
        # table_range pins appends to column A so rows never shift right of the existing table
        # RAW: values are stored as-is, never parsed as formulas (scraped text is untrusted)
        ws.append_rows(new_rows, value_input_option="RAW", table_range="A1")
    return {"appended": len(new_rows), "skipped_existing": len(rows) - len(new_rows), "columns": header,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}"}


# Existing tracker tabs have their own headers ("Job Link", "Vendor (posts C2C)", "H1B Signal"...).
# Map each header to a lead field so appended rows line up; unknown columns stay blank.
_ALIASES = {
    "scraped": {"date pulled", "scraped", "date"}, "title": {"role", "title", "job title"},
    "company": {"company", "vendor", "vendor posts c2c", "employer"}, "location": {"location"},
    "posted": {"posted", "date posted"}, "company_h1b": {"h1b signal", "company h1b", "h1b"},
    "posting_visa": {"posting visa", "visa note"}, "score": {"score"}, "hot_lead": {"hot lead"},
    "fit": {"fit"}, "missing": {"missing"}, "source": {"source"}, "url": {"job link", "url", "link"},
    "contact": {"contact"}, "status": {"status"},
}


def _key(h: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in (h or "").lower()).split())


def sheet_rows(jobs: list[dict], header: list[str]) -> list[list]:
    field_for = {_key(h): f for f, names in _ALIASES.items() for h in names}
    out = []
    for r in _rows(jobs, safe=False):  # RAW append: no formula parsing, so no quote prefix needed
        r.setdefault("contact", "")
        out.append([r.get(field_for.get(_key(h)), "") if field_for.get(_key(h)) else "" for h in header])
    return out
