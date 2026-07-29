#!/bin/bash
# Daily 08:00 EST executive digest -> telegram (with Export + Report-Now buttons).
# Sends the report ONLY — never compiles, never marks sent (delivery stays
# button-driven). Reporting is not delivery: config/delivery_paused.flag gates
# lead sending, NOT this report.
set -u
cd /Users/a2.0/ppa-leadengine
set -a; . ./.env; set +a
CHAT_ID="${TELEGRAM_ALLOWED_USER_IDS%%,*}"
[ -n "$CHAT_ID" ] || { echo "no TELEGRAM_ALLOWED_USER_IDS in .env" >&2; exit 1; }
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
"$PY" scripts/ops/build_cycle_bank.py   # refresh the 60-day cycle view
"$PY" scripts/ops/ppa_digest.py "$CHAT_ID"
