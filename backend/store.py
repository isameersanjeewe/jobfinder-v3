# ── STORE ────────────────────────────────────────────────────────────────────
# SQLite memory so the agent only reports NEW leads across runs, and the user can
# download everything later. user_id column = ready for multi-user.

import json, os, sqlite3
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "leads.db"))


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS leads (
        user_id TEXT, url TEXT, track TEXT, data TEXT, first_seen TEXT, status TEXT,
        PRIMARY KEY (user_id, url))""")
    c.execute("CREATE TABLE IF NOT EXISTS seen (user_id TEXT, url TEXT, PRIMARY KEY (user_id, url))")
    return c


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
