# ── STORE ────────────────────────────────────────────────────────────────────
# SQLite memory so the agent only reports NEW leads across runs, and the user can
# download everything later. user_id column = multi-user (see auth.py for how user_id is derived).
# Also holds `users` (passcode auth) and `profiles` (per-user rules.json override) -- see docs/MULTI_USER_SETUP.md
# and CLAUDE.md "Multi-user" section for why this is a cache/local mirror, not the durable source of truth.

import json, os, sqlite3
from datetime import datetime
from auth import hash_passcode, verify_passcode

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "leads.db"))


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
    return c


# ── users (passcode auth) ─────────────────────────────────────────────────────
def create_user(user_id: str, passcode: str) -> None:
    """Creates a user or resets their passcode. No self-serve endpoint calls this -- only
    backend/add_user.py, run by the owner. Idempotent: re-running with a new passcode rotates it."""
    salt_hex, hash_hex = hash_passcode(passcode)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _conn() as c:
        c.execute(
            "INSERT INTO users (user_id, passcode_salt, passcode_hash, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET passcode_salt=excluded.passcode_salt, passcode_hash=excluded.passcode_hash",
            (user_id, salt_hex, hash_hex, now))


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
