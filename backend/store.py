# ── STORE ────────────────────────────────────────────────────────────────────
# SQLite memory so the agent only reports NEW leads across runs, and the user can
# download everything later. user_id column = multi-user (see auth.py for how user_id is derived).
# Also holds `users` (passcode auth) and `profiles` (per-user rules.json override) -- see docs/MULTI_USER_SETUP.md
# and CLAUDE.md "Multi-user" section for why this is a cache/local mirror, not the durable source of truth.

import json, os, sqlite3
from datetime import datetime
from auth import hash_passcode, verify_passcode
import data_repo

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "leads.db"))
USERS_REPO_PATH = "data/_users.json"  # single shared file: {user_id: {salt, hash, created_at}}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS leads (
        user_id TEXT, url TEXT, track TEXT, data TEXT, first_seen TEXT, status TEXT,
        PRIMARY KEY (user_id, url))""")
    c.execute("CREATE TABLE IF NOT EXISTS seen (user_id TEXT, url TEXT, PRIMARY KEY (user_id, url))")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, passcode_salt TEXT, passcode_hash TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS profiles (
        user_id TEXT PRIMARY KEY, rules_json TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback (
        user_id TEXT, url TEXT, company TEXT, title TEXT, signal TEXT, updated_at TEXT,
        PRIMARY KEY (user_id, url))""")
    _agent_tables(c)
    return c


# ── users (passcode auth) ─────────────────────────────────────────────────────
def create_user(user_id: str, passcode: str) -> None:
    """Creates a user or resets their passcode. Idempotent: re-running with a new passcode rotates it.
    Local SQLite (DB_PATH) is a fast-read cache that Render's free plan wipes on every redeploy -- when
    data_repo is configured, this also writes through to the private repo's data/_users.json, which is
    the durable copy. sync_users_from_repo() rehydrates the local cache from it at startup."""
    salt_hex, hash_hex = hash_passcode(passcode)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _conn() as c:
        c.execute(
            "INSERT INTO users (user_id, passcode_salt, passcode_hash, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET passcode_salt=excluded.passcode_salt, passcode_hash=excluded.passcode_hash",
            (user_id, salt_hex, hash_hex, now))
    if data_repo.enabled():
        users = data_repo.read_json(USERS_REPO_PATH) or {}
        users[user_id] = {"salt": salt_hex, "hash": hash_hex, "created_at": now}
        data_repo.write_json(USERS_REPO_PATH, users, f"Add/update user {user_id}")


def sync_users_from_repo() -> int:
    """Pulls data/_users.json from the private repo into the local SQLite cache. Call once at backend
    startup (see main.py) so a freshly-redeployed Render instance has existing testers' accounts before
    anyone logs in. No-op (returns 0) if data_repo isn't configured. Never overwrites a user created
    locally after the last repo write with older repo data -- repo is the source of truth when enabled,
    so this always applies it, matching create_user() always writing through."""
    if not data_repo.enabled():
        return 0
    users = data_repo.read_json(USERS_REPO_PATH) or {}
    with _conn() as c:
        for user_id, rec in users.items():
            c.execute(
                "INSERT INTO users (user_id, passcode_salt, passcode_hash, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET passcode_salt=excluded.passcode_salt, passcode_hash=excluded.passcode_hash",
                (user_id, rec["salt"], rec["hash"], rec.get("created_at", "")))
    return len(users)


def verify_user(user_id: str, passcode: str) -> bool:
    """True if user_id exists and passcode matches. Used by POST /auth/login."""
    with _conn() as c:
        row = c.execute("SELECT passcode_salt, passcode_hash FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return False
    return verify_passcode(passcode, row[0], row[1])


def user_exists(user_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is not None


def list_users() -> list[dict]:
    """All testers, oldest first. Used by the admin UI's user list."""
    with _conn() as c:
        rows = c.execute("SELECT user_id, created_at FROM users ORDER BY created_at").fetchall()
    return [{"user_id": r[0], "created_at": r[1]} for r in rows]


def delete_user(user_id: str) -> bool:
    """Removes a tester's account and all their data (leads, seen, profile) -- both the local cache and,
    when configured, the durable private-repo copies (data/_users.json entry and their data/<user_id>/
    leads.json + seen.json files). Returns False if the user didn't exist, True otherwise."""
    existed = user_exists(user_id)
    with _conn() as c:
        c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM leads WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM seen WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM profiles WHERE user_id=?", (user_id,))
        for t in AGENT_TABLES:
            c.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM feedback WHERE user_id=?", (user_id,))
    if data_repo.enabled():
        users = data_repo.read_json(USERS_REPO_PATH) or {}
        if users.pop(user_id, None) is not None:
            data_repo.write_json(USERS_REPO_PATH, users, f"Remove user {user_id}")
        data_repo.delete_json(f"data/{user_id}/leads.json", f"Remove leads for {user_id}")
        data_repo.delete_json(f"data/{user_id}/seen.json", f"Remove seen for {user_id}")
    return existed


# ── profiles (per-user rules.json override; see rules_service.load_rules) ────
def get_profile(user_id: str) -> dict | None:
    """The user's saved profile (full rules dict), or None if they have none yet (caller falls back
    to rules.example.json)."""
    with _conn() as c:
        row = c.execute("SELECT rules_json FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    return json.loads(row[0]) if row else None


def save_profile(user_id: str, rules: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _conn() as c:
        c.execute(
            "INSERT INTO profiles (user_id, rules_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET rules_json=excluded.rules_json, updated_at=excluded.updated_at",
            (user_id, json.dumps(rules), now))


def mark_seen(urls: list[str], user_id: str = "default") -> set[str]:
    """Returns the subset of urls never seen before, and records them."""
    with _conn() as c:
        old = {r[0] for r in c.execute("SELECT url FROM seen WHERE user_id=?", (user_id,))}
        new = [u for u in urls if u and u not in old]
        c.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(user_id, u) for u in new])
    return set(new)


def save_leads(jobs: list[dict], track: str = "ft", user_id: str = "default") -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _conn() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?)",
                      [(user_id, j["url"], track, json.dumps(j), now, "Not Applied") for j in jobs if j.get("url")])
        return c.total_changes - before


# ── feedback (roadmap item 1: wires thumbs up/down into fit_service.score_lead) ──────────────────────
# Local SQLite only (same durability tier leads/seen have before the private-repo setup in
# docs/MULTI_USER_SETUP.md -- see CLAUDE.md "Multi-user"). Per-user, unlike the legacy global
# feedback_store dict in main.py (kept as-is for the old /search pipeline it still drives).
def save_feedback(user_id: str, url: str, company: str, title: str, signal: str) -> None:
    """signal="" removes the record (the UI toggles a thumb off by re-sending the same signal)."""
    with _conn() as c:
        if signal:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            c.execute(
                "INSERT INTO feedback (user_id, url, company, title, signal, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id, url) DO UPDATE SET company=excluded.company, title=excluded.title, "
                "signal=excluded.signal, updated_at=excluded.updated_at",
                (user_id, url, company, title, signal, now))
        else:
            c.execute("DELETE FROM feedback WHERE user_id=? AND url=?", (user_id, url))


def feedback_signal_counts(user_id: str) -> dict:
    """Aggregated thumbs up/down counts by company and by title pattern, for fit_service.feedback_bias().
    Recomputed from the raw rows on every call -- a handful of rows per tester, no need for a running
    aggregate. {"company": {name_lower: {"up": n, "down": n}}, "title": {pattern: {"up": n, "down": n}}}."""
    from rules_service import _SENIORITY  # local import: avoids a hard import-time dependency on rules_service
    companies: dict[str, dict[str, int]] = {}
    titles: dict[str, dict[str, int]] = {}
    with _conn() as c:
        rows = c.execute("SELECT company, title, signal FROM feedback WHERE user_id=?", (user_id,)).fetchall()
        # Agent v1: a per-lead Status is the same memory as a thumb (Applied/Interview = up, Skipped = down;
        # Rejected-by-employer says nothing about preference). An explicit thumb on the same URL wins.
        rows += [(co, ti, STATUS_SIGNAL[st]) for co, ti, st in c.execute(
            "SELECT company, title, status FROM lead_status WHERE user_id=? AND url NOT IN "
            "(SELECT url FROM feedback WHERE user_id=?)", (user_id, user_id)).fetchall() if st in STATUS_SIGNAL]
    for company, title, signal in rows:
        if signal not in ("up", "down"):
            continue
        co = (company or "").strip().lower()
        if co:
            companies.setdefault(co, {"up": 0, "down": 0})[signal] += 1
        tp = _SENIORITY.sub("", (title or "").strip().lower())
        if tp:
            titles.setdefault(tp, {"up": 0, "down": 0})[signal] += 1
    return {"company": companies, "title": titles}


def list_leads(user_id: str = "default", since: str = "", track: str = "") -> list[dict]:
    q, args = "SELECT data, first_seen, status, track FROM leads WHERE user_id=?", [user_id]
    if since:
        q, args = q + " AND first_seen >= ?", args + [since]
    if track:
        q, args = q + " AND track = ?", args + [track]
    with _conn() as c:
        rows = c.execute(q + " ORDER BY first_seen DESC", args).fetchall()
    out = []
    for data, first_seen, status, trk in rows:
        j = json.loads(data)
        j.update(first_seen=first_seen, status=status, track=trk)
        out.append(j)
    return out


# ── Agent v1 (agent_mode.py): per-user agent state ────────────────────────────────────────────────
# Same per-user, local-SQLite tier as leads/feedback. export_agent_state()/import_agent_state() give
# the whole thing as one JSON document so it can live in the private data repo as
# data/<user_id>/agent.json (see agent_mode.sync_to_repo / data-repo-template/scripts/run_agent_cycle.py).
AGENT_TABLES = ("agent_settings", "lead_status", "watchlist", "approvals", "agent_activity", "apply_kits")
LEAD_STATUSES = ("New", "Applied", "Interview", "Rejected", "Skipped")
STATUS_SIGNAL = {"Applied": "up", "Interview": "up", "Skipped": "down"}  # feeds feedback_signal_counts()


def _now() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent_tables(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS agent_settings (
        user_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, interval_hours INTEGER DEFAULT 24,
        last_run TEXT DEFAULT '', next_run TEXT DEFAULT '', notes TEXT DEFAULT '', resume_text TEXT DEFAULT '',
        rules_snapshot TEXT DEFAULT '', carry_forward TEXT DEFAULT '[]', brief TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS lead_status (
        user_id TEXT, url TEXT, company TEXT, title TEXT, status TEXT, changed_at TEXT, applied_at TEXT DEFAULT '',
        PRIMARY KEY (user_id, url))""")
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        user_id TEXT, company TEXT, ats TEXT, slug TEXT, source TEXT, added_at TEXT,
        PRIMARY KEY (user_id, company))""")
    c.execute("""CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, kind TEXT, key TEXT, payload TEXT, reason TEXT,
        status TEXT, created_at TEXT, decided_at TEXT, UNIQUE (user_id, kind, key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS agent_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, cycle_id TEXT, ts TEXT, kind TEXT, message TEXT, url TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS apply_kits (
        user_id TEXT, url TEXT, cycle_id TEXT, data TEXT, created_at TEXT, PRIMARY KEY (user_id, url))""")


_SETTINGS_COLS = ("enabled", "interval_hours", "last_run", "next_run", "notes", "resume_text", "rules_snapshot",
                  "carry_forward", "brief", "updated_at")
_SETTINGS_DEFAULT = {"enabled": 0, "interval_hours": 24, "last_run": "", "next_run": "", "notes": "", "resume_text": "",
                     "rules_snapshot": "", "carry_forward": "[]", "brief": "", "updated_at": ""}


def get_agent_settings(user_id: str) -> dict:
    with _conn() as c:
        row = c.execute(f"SELECT {', '.join(_SETTINGS_COLS)} FROM agent_settings WHERE user_id=?", (user_id,)).fetchone()
    s = dict(zip(_SETTINGS_COLS, row)) if row else dict(_SETTINGS_DEFAULT)
    s["enabled"] = bool(s["enabled"])
    s["carry_forward"] = json.loads(s["carry_forward"] or "[]")
    s["brief"] = json.loads(s["brief"]) if s["brief"] else None
    return s


def save_agent_settings(user_id: str, **fields) -> dict:
    """Upserts only the given fields (unknown keys are ignored)."""
    cur = get_agent_settings(user_id)
    cur.update({k: v for k, v in fields.items() if k in _SETTINGS_COLS})
    cur["updated_at"] = _now()
    row = dict(cur, enabled=int(bool(cur["enabled"])), carry_forward=json.dumps(cur["carry_forward"] or []),
               brief=json.dumps(cur["brief"]) if cur["brief"] else "")
    with _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO agent_settings (user_id, {', '.join(_SETTINGS_COLS)}) VALUES "
                  f"(?, {', '.join('?' * len(_SETTINGS_COLS))})", (user_id, *[row[k] for k in _SETTINGS_COLS]))
    return get_agent_settings(user_id)


def agent_enabled_users() -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM agent_settings WHERE enabled=1 ORDER BY user_id")]


def set_lead_status(user_id: str, url: str, status: str, company: str = "", title: str = "", now: str = "") -> dict:
    """Any status can move to any other (the user corrects mistakes). changed_at only moves when the
    status actually changes, so "Applied 8 days ago, unchanged" stays measurable for follow-ups. "New"
    clears the record. applied_at is stamped the first time a lead becomes Applied and kept through later
    moves (Interview/Rejected), so time-to-response stays measurable. Also mirrors onto the legacy
    leads.status column when that lead is saved."""
    if status not in LEAD_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    now = now or _now()
    with _conn() as c:
        prev = c.execute("SELECT status, changed_at, company, title, applied_at FROM lead_status WHERE user_id=? AND url=?",
                         (user_id, url)).fetchone()
        applied_at = (prev[4] if prev and prev[4] else "") or (now if status == "Applied" else "")
        if status == "New":
            c.execute("DELETE FROM lead_status WHERE user_id=? AND url=?", (user_id, url))
        elif prev and prev[0] == status:
            now = prev[1]
        else:
            c.execute("INSERT OR REPLACE INTO lead_status VALUES (?,?,?,?,?,?,?)",
                      (user_id, url, company or (prev[2] if prev else ""), title or (prev[3] if prev else ""), status, now,
                       applied_at))
        c.execute("UPDATE leads SET status=? WHERE user_id=? AND url=?",
                  ("Not Applied" if status == "New" else status, user_id, url))
    return {"url": url, "status": status, "changed_at": "" if status == "New" else now,
            "applied_at": "" if status == "New" else applied_at}


def lead_statuses(user_id: str) -> dict:
    """{url: {"status", "changed_at", "company", "title", "applied_at"}}"""
    with _conn() as c:
        rows = c.execute("SELECT url, status, changed_at, company, title, applied_at FROM lead_status WHERE user_id=?",
                         (user_id,)).fetchall()
    return {u: {"status": s, "changed_at": t, "company": co, "title": ti, "applied_at": ap or ""} for u, s, t, co, ti, ap in rows}


def feedback_rows(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT url, company, title, signal, updated_at FROM feedback WHERE user_id=?", (user_id,)).fetchall()
    return [dict(zip(("url", "company", "title", "signal", "updated_at"), r)) for r in rows]


def list_watchlist(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT company, ats, slug, source, added_at FROM watchlist WHERE user_id=? ORDER BY added_at",
                         (user_id,)).fetchall()
    return [dict(zip(("company", "ats", "slug", "source", "added_at"), r)) for r in rows]


def add_watchlist(user_id: str, company: str, ats: str = "", slug: str = "", source: str = "manual") -> None:
    with _conn() as c:
        c.execute("INSERT INTO watchlist VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, company) DO UPDATE SET "
                  "ats=excluded.ats, slug=excluded.slug", (user_id, company.strip().lower(), ats, slug, source, _now()))


def remove_watchlist(user_id: str, company: str) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM watchlist WHERE user_id=? AND company=?",
                         (user_id, company.strip().lower())).rowcount > 0


def propose_approval(user_id: str, kind: str, key: str, payload: dict, reason: str) -> bool:
    """False if this (kind, key) was ever proposed before -- a rejected proposal is never re-proposed."""
    with _conn() as c:
        return c.execute("INSERT OR IGNORE INTO approvals (user_id, kind, key, payload, reason, status, created_at, decided_at) "
                         "VALUES (?,?,?,?,?,'pending',?,'')", (user_id, kind, key, json.dumps(payload), reason, _now())).rowcount > 0


def list_approvals(user_id: str, status: str = "") -> list[dict]:
    q, args = "SELECT id, kind, key, payload, reason, status, created_at, decided_at FROM approvals WHERE user_id=?", [user_id]
    if status:
        q, args = q + " AND status=?", args + [status]
    with _conn() as c:
        rows = c.execute(q + " ORDER BY id DESC", args).fetchall()
    return [{"id": r[0], "kind": r[1], "key": r[2], "payload": json.loads(r[3] or "{}"), "reason": r[4],
             "status": r[5], "created_at": r[6], "decided_at": r[7]} for r in rows]


def decide_approval(user_id: str, approval_id: int, accept: bool) -> dict | None:
    """Scoped by user_id: another user's approval id is indistinguishable from a missing one (None)."""
    with _conn() as c:
        row = c.execute("SELECT kind, payload, status FROM approvals WHERE id=? AND user_id=?", (approval_id, user_id)).fetchone()
        if not row or row[2] != "pending":
            return None
        c.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=? AND user_id=?",
                  ("accepted" if accept else "rejected", _now(), approval_id, user_id))
    payload = json.loads(row[1] or "{}")
    if accept and row[0] == "watchlist_add" and payload.get("company"):
        add_watchlist(user_id, payload["company"], payload.get("ats", ""), payload.get("slug", ""), "approved")
    return {"id": approval_id, "status": "accepted" if accept else "rejected", "kind": row[0], "payload": payload}


def add_activity(user_id: str, cycle_id: str, kind: str, message: str, url: str = "", keep: int = 300) -> None:
    with _conn() as c:
        c.execute("INSERT INTO agent_activity (user_id, cycle_id, ts, kind, message, url) VALUES (?,?,?,?,?,?)",
                  (user_id, cycle_id, _now(), kind, message[:500], (url or "")[:2000]))
        c.execute("DELETE FROM agent_activity WHERE user_id=? AND id NOT IN (SELECT id FROM agent_activity "
                  "WHERE user_id=? ORDER BY id DESC LIMIT ?)", (user_id, user_id, keep))


def list_activity(user_id: str, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id, cycle_id, ts, kind, message, url FROM agent_activity WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(zip(("id", "cycle_id", "ts", "kind", "message", "url"), r)) for r in rows]


def save_kit(user_id: str, url: str, cycle_id: str, kit: dict) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO apply_kits VALUES (?,?,?,?,?)", (user_id, url, cycle_id, json.dumps(kit), _now()))


def list_kits(user_id: str, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT data, created_at FROM apply_kits WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                         (user_id, limit)).fetchall()
    kits = [dict(json.loads(d), created_at=t) for d, t in rows]
    # newest first; within one cycle (same timestamp) best score first
    return sorted(kits, key=lambda k: (k["created_at"], k.get("score") or 0), reverse=True)


def kit_urls(user_id: str) -> set[str]:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT url FROM apply_kits WHERE user_id=?", (user_id,))}


def export_agent_state(user_id: str) -> dict:
    """Everything the agent remembers for one user, as one JSON-able document (data/<user_id>/agent.json)."""
    out = {"version": 1}
    with _conn() as c:
        for t in AGENT_TABLES + ("feedback",):
            cur = c.execute(f"SELECT * FROM {t} WHERE user_id=?", (user_id,))
            cols = [d[0] for d in cur.description]
            out[t] = [{k: v for k, v in zip(cols, r) if k != "user_id"} for r in cur.fetchall()]
    return out


def import_agent_state(user_id: str, state: dict) -> None:
    """Replaces this user's agent tables (and feedback) with `state`. Rows are always written under the
    caller-given user_id, never one read from the document."""
    with _conn() as c:
        for t in AGENT_TABLES + ("feedback",):
            if t not in state:
                continue
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            c.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
            for row in state.get(t) or []:
                vals = {k: row[k] for k in cols if k in row and k not in ("user_id", "id")}
                vals["user_id"] = user_id
                ks = list(vals)
                c.execute(f"INSERT OR REPLACE INTO {t} ({', '.join(ks)}) VALUES ({', '.join('?' * len(ks))})",
                          [vals[k] for k in ks])
