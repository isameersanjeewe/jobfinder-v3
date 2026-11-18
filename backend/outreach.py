# ── OUTREACH DRAFTING (free; BYO LLM optional) ──────────────────────────────
# Roadmap item 7: drafts a short outreach note for one lead -- GENERATES ONLY, NEVER SENDS. This repo
# has a hard existing rule (CLAUDE.md "How to work in this repo", docs/CONTEXT.md "Guardrails"): never
# submit applications or send messages on the user's behalf. The user copies this draft and sends it
# themselves, e.g. via the LinkedIn contact found with contacts.linkedin_people_url().
#
# BYO LLM key only, same provider config fit_service.llm_gap_fit uses (see agent_service.py's header
# comment): LLM_PROVIDER=openai + LLM_BASE_URL=https://api.groq.com/openai/v1 + LLM_API_KEY + LLM_MODEL
# (Groq free tier + an open-source Llama model recommended). No key configured -> a deterministic
# template draft, zero network calls -- drafting must never require payment or be a hard dependency.

import os
import re

import requests

import fit_service  # module reference (not `from fit_service import ...`): config is read live below,
                     # same as fit_service.llm_configured() itself -- so flipping fit_service's provider
                     # config at runtime (how this project's tests exercise LLM paths; see agent_service.py's
                     # PROVIDER) actually takes effect here too, not a stale snapshot from import time.

OUTREACH_TIMEOUT = int(os.getenv("LLM_OUTREACH_TIMEOUT", "20"))

_TEMPLATE = ("Hi, I saw the {title} opening at {company} and wanted to reach out directly. I have relevant "
             "experience in {highlight} and would love to connect about the role. Happy to share my resume "
             "if that's useful -- thanks for your time!")

_TONE_LABEL = {"recruiter": "recruiter", "hiring_manager": "hiring manager"}


def _keyword_highlight(job: dict) -> str:
    jd = f"{job.get('title','')} {job.get('description','')}".lower()
    hits = [s for s in fit_service.SKILLS if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", jd)]
    return ", ".join(hits[:3]) if hits else "this space"


def _template_draft(job: dict) -> str:
    return _TEMPLATE.format(title=job.get("title") or "this role", company=job.get("company") or "your team",
                             highlight=_keyword_highlight(job))


def draft_outreach(job: dict, resume_text: str = "", tone: str = "recruiter") -> dict:
    """Generates (never sends) a short outreach note for one lead. {"draft": str, "source": "llm"|"template"}.
    LLM draft when a BYO key is configured and the call/parse succeeds; a deterministic template
    otherwise -- always returns something usable, no payment ever required, and never raises."""
    title, company = job.get("title", ""), job.get("company", "")
    if fit_service.llm_configured():
        try:
            prompt = ("Write a short (under 80 words), friendly, professional outreach message from a job "
                      f"applicant to a {_TONE_LABEL.get(tone, 'recruiter')} about the '{title}' role at "
                      f"{company}. Plain text only: no subject line, no bracketed placeholders, no markdown."
                      + (f" Relevant background: {resume_text[:600]}" if resume_text else "")
                      + (f"\n\nJob snippet: {job['description'][:800]}" if (job.get("description") or "").strip() else ""))
            if fit_service.LLM_PROVIDER == "anthropic":
                r = requests.post("https://api.anthropic.com/v1/messages", timeout=OUTREACH_TIMEOUT, headers={
                    "x-api-key": fit_service.ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": fit_service.LLM_MODEL, "max_tokens": 250, "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
                text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
            else:
                r = requests.post(f"{fit_service.LLM_BASE_URL}/chat/completions", timeout=OUTREACH_TIMEOUT,
                                   headers={"Authorization": f"Bearer {fit_service.LLM_API_KEY}"},
                                   json={"model": fit_service.LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4})
                r.raise_for_status()
                text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                return {"draft": text[:2000], "source": "llm"}
        except Exception:
            pass  # fall through to the template -- drafting must never hard-fail
    return {"draft": _template_draft(job), "source": "template"}
