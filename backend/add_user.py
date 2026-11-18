# ── ADD USER ─────────────────────────────────────────────────────────────────
# Owner-run CLI to create (or reset) a tester's passcode. There is no self-serve signup endpoint on
# purpose (see CLAUDE.md "Multi-user" section) -- this is the only way a user_id/passcode pair is created.
#
# Usage:
#   cd backend && python add_user.py <user_id> <passcode>
#
# <user_id> must match the same pattern the API accepts: letters, digits, "_ . @ -" (e.g. an email or a
# short handle), 1-64 chars. Re-running with an existing user_id rotates their passcode.
#
# This writes to the same SQLite file the API uses (env DB_PATH, default backend/leads.db). On Render,
# run this against the production DB by setting DB_PATH to point at it, or run it once locally against
# a copy and note that -- today -- Render's disk is NOT persistent (see CLAUDE.md), so for a real Render
# deployment prefer creating users via a one-off shell on the Render instance, or wait for the data to
# move to the private-repo-backed store described in docs/MULTI_USER_SETUP.md.

import re
import sys

import store

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python add_user.py <user_id> <passcode>", file=sys.stderr)
        return 2
    user_id, passcode = sys.argv[1], sys.argv[2]
    if not USER_ID_RE.match(user_id):
        print("user_id must be 1-64 chars of letters, digits, '_ . @ -'", file=sys.stderr)
        return 2
    if len(passcode) < 4:
        print("passcode must be at least 4 characters", file=sys.stderr)
        return 2
    existed = store.user_exists(user_id)
    store.create_user(user_id, passcode)
    print(f"{'Reset passcode for' if existed else 'Created'} user '{user_id}'.")
    print("Give the tester: the app URL + this passcode + their user_id (they type both into the login screen).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
