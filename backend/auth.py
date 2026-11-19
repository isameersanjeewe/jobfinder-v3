# ── AUTH ─────────────────────────────────────────────────────────────────────
# Per-user passcode auth, layered on top of the existing API_TOKEN gate in main.py.
# API_TOKEN protects the whole API from the public internet (unchanged); this layer identifies WHICH
# user is calling, so user_id is never trusted from a client-supplied request field (see main.py: every
# endpoint that used to accept `user_id` in the body/query now derives it from the session token instead).
#
# Passcode hashing: PBKDF2-HMAC-SHA256 (stdlib hashlib, no new dependency), random salt per user.
# Session token: "<user_id>:<expiry>" HMAC-SHA256 signed with env SESSION_SECRET. Deliberately not a JWT
# (avoids adding PyJWT): a minimal hand-rolled signed token is enough to prove "this request belongs to a
# user who already proved their passcode to /auth/login", which is all this needs to do.
#
# No self-serve signup: the owner creates passcodes for testers with `python backend/add_user.py <user_id> <passcode>`.

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time

log = logging.getLogger("jobfinder")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
PBKDF2_ITERATIONS = 200_000

# Fail closed: never sign with an empty key (anyone could forge a token for any user). A random
# per-process fallback keeps local dev working; sessions just end whenever the process restarts.
_FALLBACK_SECRET = secrets.token_urlsafe(32)


def _key() -> bytes:
    return (SESSION_SECRET or _FALLBACK_SECRET).encode("utf-8")


if not SESSION_SECRET:
    log.warning("SESSION_SECRET not set: using a random per-process secret, so every restart logs all "
                "users out -- set SESSION_SECRET on Render (see docs/MULTI_USER_SETUP.md)")


def hash_passcode(passcode: str, salt: bytes | None = None) -> tuple[str, str]:
    """Returns (salt_hex, hash_hex) for storing a new/changed passcode."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_passcode(passcode: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time compare against a stored (salt_hex, hash_hex) pair."""
    try:
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hash_hex)


def make_session_token(user_id: str, days: int | None = None) -> str:
    """Issues a signed session token for an already-verified user_id. Expires after `days` (default SESSION_DAYS)."""
    expiry = int(time.time()) + (days or SESSION_DAYS) * 86400
    payload = f"{user_id}:{expiry}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_key(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str) -> str | None:
    """Returns the user_id if the token is well-formed, correctly signed and not expired; else None.
    Never raises: any malformed/tampered input just fails verification."""
    if not token or "." not in token:
        return None
    payload_b64, _, sig = token.partition(".")
    try:
        # payload_b64/sig come straight from client input: a non-ASCII byte (e.g. a raw Latin-1 header
        # byte an ASGI server decoded as-is) would make .encode("ascii") or hmac.compare_digest raise --
        # keep the whole signature check inside this try so garbage input fails closed, never raises.
        expected = hmac.new(_key(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        user_id, expiry_s = payload.rsplit(":", 1)
        expiry = int(expiry_s)
    except Exception:
        return None
    if expiry < int(time.time()) or not user_id:
        return None
    return user_id
