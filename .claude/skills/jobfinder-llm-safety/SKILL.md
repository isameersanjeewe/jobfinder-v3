---
name: jobfinder-llm-safety
description: Safety and cost rules for any JobFinder code that sends text to an LLM or uses text that comes back (apply kits, pick/skip reasons, outreach drafts, resume fit, weekly review, OpenRouter/Gemini/Groq BYO keys). Use this before writing or reviewing any prompt, LLM call, or place where model output reaches the UI, a sheet, a doc or the shared database in the JobFinder repo, even for a "small" new field or a prompt tweak.
---

# JobFinder LLM safety

The user is a visa holder applying for real jobs. Model output ends up in applications they submit. Two harms are concrete:
- **invented facts** (a metric or skill they don't have, or a wrong work-authorization answer, which is a legal risk)
- **hostile job descriptions** steering the agent (ranking themselves, planting scam asks or links)

There's also a cost harm: free-tier limits, and the owner's GitHub minutes. Each rule below exists for one of these reasons.

## Inputs
- **Job descriptions and scraped text are untrusted data.** Wrap them in clearly delimited untrusted blocks (see `agent_mode.untrusted()`) and tell the model the block is data, not instructions.
- **Truncate:** JD ~2000 chars, resume ~2500. Never send the full ~3000-posting pool. The LLM only sees a shortlist that already passed deterministic gating (`rules_service.apply_gate`, `fit_service.score_lead`).

## What the model may decide
- **Choose and reorder only.** It picks from candidate ids you gave it; validate every id against that set and drop unknown ones. It never adds leads, changes scores, or writes company signals (`set_company_signal` and `agent_service` tools are off-limits from agent code; a test enforces this).
- **No tools with outbound effect:** nothing that sends, fetches URLs, applies, or writes the shared H1B database. The output is a draft that a human reads.

## Output vetting (every string the user will see)
- **Visa, sponsorship and work-authorization wording comes only from `rules_service.sponsorship_answer()`.** Strip or reject model text that matches `agent_mode._VISA`.
- Run `agent_mode._risky()` on all model text, **including short "reason" strings**, and fall back to a deterministic reason (`_why()`) on a hit. It catches payment/identity asks (wire, gift card, PayPal, passport, SSN…) and visa claims.
- Run `agent_mode.unverified_tokens(text, resume, allowed)` on bullets and cover notes. Drop bullets with numbers, spelled-out quantities or capitalized tools/employers missing from the resume, and show "unverified" flags in the UI. `vet_kit_text()` wraps this.
- Clip lengths, escape in the UI (already done by the frontend's escape helper), and only render `http(s)` links.

## Cost and failure
- **Budget per cycle:** `AGENT_LLM_MAX_CALLS_PER_CYCLE`, `AGENT_CYCLE_TOKEN_BUDGET`, `AGENT_LLM_MIN_GAP_SECONDS`, `AGENT_KITS_PER_CYCLE`. Add a cap for any new loop. A tool loop that resends the history every step (as `agent_service`'s does) costs 50–80k tokens per run, so prefer one batched call.
- **The first 429, timeout or bad reply stops LLM use for the cycle.** Switch to templates, log it in Activity, and carry pending work to the next cycle. Save each artifact as it's made, so a cycle never ends with nothing saved.
- **No key configured means zero network calls.** Check `fit_service.llm_configured()`. A tripwire test counts HTTP attempts.
- Each user's LLM use runs on their own key (BYO). Never route other users through the owner's key.

## Reuse, don't copy
LLM call code already exists in `agent_service.py`, `fit_service.py`, `outreach.py`, `ai_service.py` and `agent_mode.py`, with conflicting defaults. Don't add another copy. Extend `agent_mode`'s budgeted caller, or do the planned `backend/llm.py` refactor (provider allowlist, per-user config) first. Phase B's OpenRouter keys depend on it.

## Tests to add with any LLM change
Add these to `tests/test_agent_mode_qa.py`:
- a hostile JD (self-ranking, planted URL, payment ask, fake skill, citizenship claim)
- an LLM 500/timeout path stopping further calls
- the no-key path making zero calls
- visa text matching `sponsorship_answer()` exactly

Then run **jobfinder-verify**.

## Lessons
Add one dated line after each real use.
- 2026-11-18: QA found pick reasons shown without vetting (`clean()` only) and `_risky` missing "authorized to work", EAD, PayPal and passport. It also found the fact-check missing "tripled" and "forty percent". All fixed.
- 2026-11-18: Still open: invented **lowercase** tool names ("kubernetes", "terraform") pass the fact-check. Tell users to read kits; a skills vocabulary check is the likely fix.
