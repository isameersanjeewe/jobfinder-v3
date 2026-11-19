# Owner setup checklist (manual steps only the owner can do)

Each step is tagged with the phase that needs it: **[A]** Agent v1 (owner only), **[A2]** push notifications,
**[B]** multi-user, **[C]**/**[D]** nothing extra beyond B. Do not paste any secret value into a committed file,
issue, or chat. Replace every `<...>` placeholder yourself. Existing basics (data repo creation, `DATA_REPO_TOKEN`,
`SESSION_SECRET`) are in `docs/MULTI_USER_SETUP.md`; this file adds what the Agent needs and links back there.

Verification legend: "Verified" = confirmed against vendor docs this session; "Not verified" = could not confirm, check the
vendor page yourself.

## Phase A

### A-0. Lock down the live API — do this first [A, urgent]
As of 2026-11-18 production reports `"auth": false` on `GET /`: the API is open to the internet. In Render -> backend -> Environment set three **different** random values (generate each with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`):
- `API_TOKEN` — paste it into the app's Connection settings token field (testers get it too, for now).
- `SESSION_SECRET` — signs login sessions. Changing it logs everyone out once.
- `ADMIN_TOKEN` — owner only, never shared. Needed for `/admin/*` (send as header `X-Admin-Token`). Unset = admin endpoints disabled.
Verify: `GET /` on the backend shows `"auth": true`.

### A-1. Confirm the private data repo and DATA_REPO_TOKEN [A]
The Agent tab needs durable storage (Render's disk is wiped on redeploy), so in Phase A these are **required**, not optional.
1. Render dashboard -> `jobfinder-backend` -> Environment. Confirm both `DATA_REPO` and `DATA_REPO_TOKEN` exist (values hidden is fine).
2. Check the token still works and has not expired: GitHub -> Settings -> Developer settings -> Fine-grained tokens -> your token -> check "Expires on" and that Repository access is only the data repo, with Contents and Actions both Read and write.
3. Check it live: open the app, run a search while logged in; if Leads still persist after a Render redeploy, it works. Or from your own terminal: `gh api repos/<owner>/<data-repo>/contents/data --silent && echo OK` (uses your own gh login, not the token).
4. If missing/expired, follow `docs/MULTI_USER_SETUP.md` steps 1-4. Set a calendar reminder 2 weeks before expiry.

### A-2. Confirm RULES_JSON on Render [A]
1. Render -> Environment -> confirm `RULES_JSON` is set (your profile JSON, the contents of your local `backend/rules.json`).
2. If you change your profile locally, paste the new JSON there and redeploy. The scheduled data-repo workflow also needs the profile: see A-5.

### A-3. Pick an LLM provider and set env vars [A]
The Agent works without an LLM (template kits, deterministic plan), but kits and the "which leads deserve a kit" decision need one. Pick ONE.

**Option 1: Groq (recommended for A).**
1. Create an API key at https://console.groq.com/keys (free tier, no card per vendor docs; not re-verified).
2. Render -> Environment, add:
   - `LLM_PROVIDER` = `openai`
   - `LLM_BASE_URL` = `https://api.groq.com/openai/v1`
   - `LLM_MODEL` = `llama-3.3-70b-versatile`
   - `LLM_API_KEY` = your `gsk_...` key
3. Model id `llama-3.3-70b-versatile` is consistent across third-party sources this session but **Groq's own docs were not reachable via search**: check https://console.groq.com/docs/models for the current id, and look for a newer Llama 3.3-class or later model if this one is deprecated.
4. Free-tier limits: sources disagree (some say ~30 requests/min, 14,400 requests/day, 6,000-12,000 tokens/min for this model). **Not verified.** Check your own limits at https://console.groq.com/settings/limits before relying on any number. TPM is the likely constraint when several kits are generated in a row; the app should pace calls.

**Option 2: Gemini (OpenAI-compatible).**
1. Create a key in Google AI Studio (https://aistudio.google.com/apikey).
2. Render env: `LLM_PROVIDER` = `openai`, `LLM_BASE_URL` = `https://generativelanguage.googleapis.com/v1beta/openai/` (Verified against Google's OpenAI-compatibility page), `LLM_API_KEY` = your AI Studio key, `LLM_MODEL` = a current Gemini Flash model id. Model ids change often and the pages seen this session disagreed; pick from https://ai.google.dev/gemini-api/docs/models. (Not verified which id is current.)
3. Free-tier data use: Google may use free-tier prompts to improve products (check the current terms). Your resume text goes in prompts.

4. After saving, Render redeploys. Check `GET /health` or the UI Full Search shows "✦" on fits.
5. **Also add the same four values as Actions secrets in the private data repo** (see A-5), because the scheduled agent loop runs there, not on Render.

### A-4. Optional hardening [A]
Confirm `API_TOKEN`, `ALLOWED_ORIGINS` (your Vercel URL) and `SESSION_SECRET` are set on Render (see `render.yaml`).

### A-5. Data repo workflows and Actions secrets [A]
Private data repo -> Settings -> Secrets and variables -> Actions -> New repository secret. The dev agent will tell you the exact names when the workflows land; expected set for Phase A:
- `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` (same values as A-3)
- `RULES_JSON` (your profile JSON)
Confirm Actions are enabled for the repo (Settings -> Actions -> General -> Allow all actions). Then in the Actions tab, run the new scheduled workflows once manually ("Run workflow") and read the log.
Minutes: GitHub Pro lists 3,000 Actions min/month for private repos (found in GitHub plan docs; the current docs page did not show the number in the snippet reviewed, so confirm at https://github.com/settings/billing). Re-check "Usage this month" after the first week.
Note: scheduled workflows can be delayed or skipped by GitHub under load, and GitHub disables schedules on public repos after 60 days of no activity (not verified for private repos).

## Phase A2: push notifications

### A2-1. Generate VAPID keys [A2]
No external account needed. Two options; the first needs nothing but this repo's venv.

Option 1 (uses the `cryptography` package already installed in `.venv`):
```bash
.venv/bin/python - <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization as s
k = ec.generate_private_key(ec.SECP256R1())
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
print("VAPID_PRIVATE_KEY=" + b(k.private_numbers().private_value.to_bytes(32, "big")))
print("VAPID_PUBLIC_KEY=" + b(k.public_key().public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint)))
PY
```
Option 2: `npx web-push generate-vapid-keys` (needs Node). Not verified against the `pywebpush` version the dev agent chooses; the dev agent will confirm the key format it expects.
1. Add `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` and `VAPID_SUBJECT` (= `mailto:<an email you control>`) as Render env vars and as data-repo Actions secrets (the scheduled job sends the push).
2. Keep the private key out of git; losing it only means users must re-subscribe.
3. iOS: push works only for the site added to the Home Screen as an app (not verified this session).

## Phase B: multi-user

### B-1. Generate ENCRYPTION_KEY [B]
```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
(tested: outputs a 44-character url-safe base64 string). Add as `ENCRYPTION_KEY` on Render **and** as a data-repo Actions secret. Store a copy in your password manager: if lost, every user must reconnect Google and OpenRouter. Never rotate it without the dev agent's migration step.

### B-2. Google Cloud project, consent screen, OAuth client [B]
1. https://console.cloud.google.com -> create a project (e.g. `jobfinder`).
2. APIs & Services -> Library -> enable **Google Drive API**, **Google Sheets API**, **Google Docs API**.
3. APIs & Services -> OAuth consent screen (may appear as "Google Auth Platform" -> Branding/Audience/Data Access):
   - User type: **External**.
   - App name, support email, developer contact email.
   - App home page and **privacy policy URL** (you must host a short privacy page; Google requires one for Production. Not verified which fields are mandatory for non-sensitive-only apps). Say plainly: reads/writes only files the app creates, sends resume text to the user's chosen LLM provider.
   - Authorized domains: the Vercel frontend domain.
   - Scopes: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`, `.../auth/drive.file`. Google docs describe `drive.file` as non-sensitive (verified), which should avoid the sensitive-scope verification review. Do not add the full `drive` scope.
   - **Publishing status: click "Publish app" so status is "In production".** While in "Testing", refresh tokens expire after 7 days (consistently reported in Google forum threads; not confirmed against Google's primary doc this session). Tokens issued while in Testing stay limited, so have every user connect after publishing.
4. Credentials -> Create credentials -> OAuth client ID -> Application type: **Web application**.
   - Authorized JavaScript origins: your Vercel frontend origin, e.g. `https://<your-app>.vercel.app`, plus `http://localhost:8000` or your local dev origin if you test locally.
   - Authorized redirect URIs: only if the dev agent implements the auth-code flow for Drive offline access (needed for scheduled runs to refresh tokens). Google Identity Services sign-in alone needs only the origin. The dev agent will state the exact redirect URI (likely `https://<render-backend>/connect/google/callback`). Add it then.
5. Copy the client ID to Render as `GOOGLE_CLIENT_ID`. If the auth-code flow is used, copy the client secret to `GOOGLE_CLIENT_SECRET` on Render and as a data-repo Actions secret (the scheduled job refreshes tokens). Never commit either.
6. Optionally add yourself and testers under Audience only matters in Testing mode; skip once published.

### B-3. OpenRouter [B]
1. Create an account at https://openrouter.ai. For PKCE you do **not** need to register an app or hold a client secret: the flow redirects the user to `https://openrouter.ai/auth?callback_url=<your callback>&code_challenge=<...>&code_challenge_method=S256` and exchanges the code with `POST https://openrouter.ai/api/v1/auth/keys` (verified against OpenRouter docs; codes expire after 10 minutes).
2. Action for you: decide the callback URL (frontend page, e.g. `https://<your-app>.vercel.app/connect/openrouter`) and tell the dev agent. Check https://openrouter.ai/docs/guides/overview/auth/oauth for whether localhost or https is required for the callback.
3. Free models on OpenRouter have their own daily request limits for accounts without credit (check the docs; not verified here). Each user's key carries their own limits.

### B-4. Seat cap and owner settings [B]
Render env: `MAX_USERS` = `15`, `OWNER_EMAIL` = the Google account that is the admin (you), `ADMIN_USER_ID` if the dev agent asks (your existing passcode user). Keep your passcode login working as a backup: do not run `add_user.py` with a weak passcode.

### B-5. Data repo Actions secrets, Phase B additions [B]
`ENCRYPTION_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (if used), `VAPID_*` (A2). Re-check minutes usage weekly in the admin view and GitHub billing.

### B-6. Fine-grained token permissions [B]
The existing `DATA_REPO_TOKEN` (Contents + Actions read/write on the data repo) should suffice. If the dev agent needs to read Actions usage, it may need "Administration"/billing access that fine-grained tokens do not offer (not verified). Check usage manually at https://github.com/settings/billing instead and enter it in the admin view if no API route exists.

## Quick reference: where each value goes

| Value | Render env | Data-repo Actions secret | Phase |
|---|---|---|---|
| LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY | yes | yes | A |
| RULES_JSON | yes | yes | A |
| DATA_REPO / DATA_REPO_TOKEN | yes | no | A |
| VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT | yes | yes | A2 |
| ENCRYPTION_KEY | yes | yes | B |
| GOOGLE_CLIENT_ID | yes | no | B |
| GOOGLE_CLIENT_SECRET (if auth-code flow) | yes | yes | B |
| MAX_USERS / OWNER_EMAIL | yes | no | B |

Exact variable names for items marked B and A2 are proposals; the dev agent may rename them, and this table should be updated then.
