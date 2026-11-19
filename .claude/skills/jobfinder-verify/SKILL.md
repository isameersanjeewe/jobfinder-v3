---
name: jobfinder-verify
description: Verify JobFinder changes before calling anything done. Runs all four offline test suites and a privacy scan of what would be committed (personal profile values, keys, sheet IDs, emails). Use this before every commit, merge of an agent branch, or "it works" claim in the JobFinder repo, and after any edit to backend/, frontend/, tests/ or data-repo-template/, even if the user only says "check it", "is it ready" or "wrap up".
---

# JobFinder verify

The repo is **public** and the app handles a visa holder's job search. Two failure modes matter most: a change that silently breaks gating, auth or isolation, and personal data landing in a commit. This skill catches both in one command, so run it every time instead of picking test files by hand.

## Run it

```bash
bash .claude/skills/jobfinder-verify/scripts/verify.sh            # tests + privacy scan
bash .claude/skills/jobfinder-verify/scripts/verify.sh --tests-only
bash .claude/skills/jobfinder-verify/scripts/verify.sh --privacy-only   # fast, use before staging
```

The full run takes a few minutes, so give the Bash call a timeout of about 10 minutes. It ends with `VERIFY: OK` or `VERIFY: NEEDS ATTENTION` and exits 0 or 1.

## Read the result

- **PASS** means the suite printed `ALL OK` as its last line. Tracebacks mentioning PDF parsing are expected noise from the upload-limit tests.
- **FAIL** blocks the commit. Read the excerpt, find the cause, and fix the code or the test. Don't weaken an assertion just to pass it.
- **known gaps open: N**: a `known_fail(...)` test still documents a real, accepted bug. Mention it to the owner in your summary.
- **newly fixed: N**: a known-fail test now passes. Convert it to a plain call or assert, as the line says, so it guards the fix from now on.
- **Privacy HIT**: the script names the file, the line and the rule, never the value.
  - Remove the value from that file, or move it to a gitignored file (`backend/rules.json`, `backend/.env`, `CLAUDE.local.md`, `docs/*.local.md`).
  - If it's a false positive, add it to the script's allowlist and note why under Lessons.

## When a suite is missing or new
The script runs `tests/test_agent.py`, `test_core_regression.py`, `test_agent_mode.py` and `test_agent_mode_qa.py`. When you add a suite, add it to the `for t in ...` line in `scripts/verify.sh` and to CLAUDE.md's Run/test block in the same change.

## What this does not cover
- Live job sources (`run_live_test.sh`, `smoke_test.py`). Those hit real boards and take minutes, so run them only when source code changed.
- UI behavior. Use the `run` skill or claude-in-chrome for that.
- Production. See `jobfinder-release`.

## Lessons
Add one dated line after each real use: what surprised you, what wasted time, what the script should catch next.
- 2026-11-18: Never put a bare `cat` (or anything else that reads stdin) in a command chain. It hung for 2 minutes. The script feeds tests `</dev/null` for the same reason.
- 2026-11-18: pydantic `Field(pattern=...)` uses Rust regex, so lookahead/lookbehind fail at import time. Use segment patterns like `^[A-Za-z0-9_@-]+(\.[A-Za-z0-9_@-]+)*$`.
- 2026-11-18: A known-fail test can be stricter than the real requirement. One flagged the app's own "Strong H-1B sponsor" database fact as an AI leak. Read what the test asserts before changing product code to satisfy it.
- 2026-11-18: The profile `name` field is a lowercase label that matches ordinary code. The scan skips lowercase single-word values; real names and locations are still checked.
