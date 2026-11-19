#!/usr/bin/env bash
# JobFinder pre-commit verification: all offline test suites + a privacy scan of what would be committed.
# Usage (from anywhere inside the repo):  bash .claude/skills/jobfinder-verify/scripts/verify.sh [--tests-only|--privacy-only]
# Exit code: 0 = all suites ALL OK and privacy scan clean; 1 = something needs attention.
# Never prints secret values: privacy hits are reported as file:line + the rule that matched.
set -u
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not inside a git repo"; exit 1; }
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing .venv -- see CLAUDE.md Run/test"; exit 1; }
MODE="${1:-all}"
FAIL=0

if [ "$MODE" != "--privacy-only" ]; then
  echo "== offline test suites"
  for t in tests/test_agent.py tests/test_core_regression.py tests/test_agent_mode.py tests/test_agent_mode_qa.py; do
    [ -f "$t" ] || { echo "  SKIP  $t (not present)"; continue; }
    out="$("$PY" "$t" 2>&1 </dev/null)"; rc=$?
    last="$(printf '%s\n' "$out" | tail -1)"
    known="$(printf '%s\n' "$out" | grep -c '^KNOWN FAILURE')"
    fixed="$(printf '%s\n' "$out" | grep -c '^FIXED:')"
    if [ $rc -eq 0 ] && [ "$last" = "ALL OK" ]; then
      echo "  PASS  $t  (known gaps open: $known, newly fixed: $fixed)"
    else
      echo "  FAIL  $t  (exit $rc)"; FAIL=1
      # the first real failure, skipping the expected PDF-parse noise from upload tests
      printf '%s\n' "$out" | grep -v -i 'pdf' | grep -E 'AssertionError|Error|assert ' | head -5 | sed 's/^/        /'
    fi
    [ "$fixed" -gt 0 ] && printf '%s\n' "$out" | grep '^FIXED:' | sed 's/^/        -> convert to plain assert: /'
  done
fi

if [ "$MODE" != "--tests-only" ]; then
  echo "== privacy scan (changed + new non-ignored files)"
  files="$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u )"
  # personal files that must stay gitignored
  for f in backend/rules.json backend/rules.local.json backend/.env CLAUDE.local.md; do
    if [ -e "$f" ] && ! git check-ignore -q "$f"; then echo "  HIT   $f is NOT gitignored"; FAIL=1; fi
  done
  if git ls-files docs | grep -q '\.local\.md$'; then echo "  HIT   a docs/*.local.md file is tracked"; FAIL=1; fi
  # personal values from the owner's profile (name, locations) -- read here, never printed
  personal="$("$PY" - <<'EOF' 2>/dev/null
import json
try:
    p = json.load(open("backend/rules.json")).get("profile", {})
except Exception:
    raise SystemExit
vals = [p.get("name", "")] + list(p.get("locations", []))
# skip lowercase single-word labels (profile slugs like a role name): they match ordinary code everywhere
print("\n".join(v for v in vals if isinstance(v, str) and len(v) >= 4 and not (v.islower() and v.isalpha())))
EOF
)"
  hits=0
  for f in $files; do
    [ -f "$f" ] || continue
    case "$f" in *.local.md|.claude/worktrees/*) continue;; esac
    # secrets / identifiers by shape (rule name printed, value never printed)
    for rule in 'gsk_[A-Za-z0-9]{20,}:groq-key' 'sk-[A-Za-z0-9_-]{20,}:openai-style-key' 'AIza[0-9A-Za-z_-]{30,}:google-api-key' \
                'gh[pousr]_[A-Za-z0-9]{30,}:github-token' 'github_pat_[A-Za-z0-9_]{30,}:github-pat' \
                'BEGIN [A-Z ]*PRIVATE KEY:private-key' 'docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{25,}:sheet-url' \
                '"private_key_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{20,}:service-account-json'; do
      re="${rule%:*}"; name="${rule##*:}"
      grep -nE -- "$re" "$f" 2>/dev/null | cut -d: -f1 | while read -r ln; do echo "  HIT   $f:$ln  ($name)"; done
      grep -qE -- "$re" "$f" 2>/dev/null && hits=1
    done
    if [ -n "$personal" ]; then
      while IFS= read -r v; do
        [ -n "$v" ] || continue
        ln="$(grep -nF -- "$v" "$f" 2>/dev/null | head -1 | cut -d: -f1)"
        [ -n "$ln" ] && { echo "  HIT   $f:$ln  (value from your private profile)"; hits=1; }
      done <<< "$personal"
    fi
  done
  # emails other than noreply/placeholder addresses
  for f in $files; do
    [ -f "$f" ] || continue
    case "$f" in *.local.md|.claude/worktrees/*) continue;; esac
    lines="$(grep -nEo '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$f" 2>/dev/null \
      | grep -viE 'noreply|example\.(com|org)|@users\.noreply|anthropic\.com' | cut -d: -f1 | sort -u)"
    for ln in $lines; do echo "  HIT   $f:$ln  (email address)"; hits=1; done
  done
  [ $hits -eq 0 ] && echo "  clean (shape + profile-value checks)" || FAIL=1
fi

[ $FAIL -eq 0 ] && echo "VERIFY: OK" || echo "VERIFY: NEEDS ATTENTION"
exit $FAIL
