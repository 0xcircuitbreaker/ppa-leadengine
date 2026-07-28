# PPA Lead Engine (delivery fork)

Scans new areas, compiles state-segregated CSV batches, delivers via Telegram.

## Daily operation (automatic)
- Fleet scanners run 24/7 (sharded, zero overlap)
- Harvest every 3h: pulls node output into `exports/fleet_harvest/`
- 05:45 daily: compile `PPA_YYYYMMDD.zip` (167k target) → Telegram report + document

## Control (via Hermes bot)
- Change scan states/workers/volume: edit `config/scan_params.json`
  (Hermes does this when you ask, then restarts the scanners)
- Pause/resume daily delivery: `config/delivery_paused.flag` (Hermes toggles)
- Manual compile: `python3 scripts/ops/ppa_compile.py 167000 PPA_MANUAL`
- Manual report: `python3 scripts/ops/ppa_report.py`

## Output format
`priority,business_name,phone,phone_type,category,city,state` — one state per
file, state in filename, 10k rows max per file, zipped.

## Rules baked in (do not change)
- NY/CA hard-blocked (state names + area codes)
- Sent-pool dedup (never re-delivers a shipped phone)
- Junk-domain and area-code validation

## Setup (one time)
1. `cp .env.example .env` and fill TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS
2. Bootstrap launchd jobs (harvest, daily-delivery)
3. Done — daily batches arrive in Telegram
