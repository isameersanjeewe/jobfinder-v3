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
