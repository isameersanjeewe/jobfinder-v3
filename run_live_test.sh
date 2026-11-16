#!/bin/bash
# Live test on your Mac (real network). Usage: bash run_live_test.sh
set -e
cd "$(dirname "$0")"
mkdir -p logs
python3 -m venv .venv
.venv/bin/pip install -q -r backend/requirements.txt -r tests/requirements-dev.txt
echo "== offline tests ==" | tee logs/10_live_offline.log
.venv/bin/python tests/test_agent.py 2>&1 | tee -a logs/10_live_offline.log
echo "== live smoke test (real job boards) ==" | tee logs/11_live_smoke.log
cd backend && DB_PATH=/tmp/live_leads.db ../.venv/bin/python smoke_test.py 2>&1 | tee -a ../logs/11_live_smoke.log
echo; echo "DONE. Logs in $(cd .. && pwd)/logs — tell Claude 'done'."
