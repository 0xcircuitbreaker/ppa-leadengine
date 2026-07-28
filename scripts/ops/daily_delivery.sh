#!/bin/bash
# ppa daily compile + telegram delivery. Scheduled by launchd.
set -euo pipefail
cd /Users/a2.0/ppa-leadengine

if [ -f config/delivery_paused.flag ]; then
    echo "delivery paused - skipping"
    exit 0
fi

NAME="PPA_$(date +%Y%m%d)"
.venv/bin/python scripts/ops/ppa_compile.py "" "$NAME" >> logs/daily.log 2>&1 || \
  python3 scripts/ops/ppa_compile.py "" "$NAME" >> logs/daily.log 2>&1

ZIP="exports/${NAME}.zip"
if [ -f "$ZIP" ]; then
    set -a; . ./.env; set +a
    REPORT=$(.venv/bin/python scripts/ops/ppa_report.py 2>/dev/null || python3 scripts/ops/ppa_report.py)
    CHAT_ID="${TELEGRAM_ALLOWED_USER_IDS%%,*}"
    SIZE_MB=$(( $(stat -f%z "$ZIP") / 1048576 ))
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        --data-urlencode text="$REPORT" >> logs/daily.log 2>&1
    if [ "$SIZE_MB" -lt 49 ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
            -F chat_id="$CHAT_ID" -F document=@"$ZIP" \
            -F caption="PPA daily batch ${NAME} (${SIZE_MB}MB)" >> logs/daily.log 2>&1
    else
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="$CHAT_ID" \
            -d text="Batch ${NAME} ready (${SIZE_MB}MB) - too large for telegram, it is on this machine at exports/${NAME}.zip" >> logs/daily.log 2>&1
    fi
    echo "delivered: $NAME (${SIZE_MB}MB)"
fi
