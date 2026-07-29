# HERMES PICKUP — finish-the-delivery checklist (2026-07-29)

You are picking up a fully-built, running delivery system. Everything below
is ordered; each item says exactly what to run and what "done" looks like.

## Current state (verified 2026-07-29)

- Machine (this Mac, partner's): office LAN `10.1.10.231`
- Delivery system: `~/ppa-leadengine` (git: github.com/0xcircuitbreaker/ppa-leadengine @ c702a21)
- 8 launchd jobs running: local-scanner (LA), loom-lane, fleet-harvest (hourly),
  daily-delivery (05:45), dashboard (:8080), seed-enrich (PAUSED by flag),
  directory-discovery (04:50), rate-governor (10min)
- Dashboard: `http://10.1.10.231:8080` (LAN-only, no auth)
- Data: already_sent_db.csv (2,413,563), day3batch.zip (150,000, verified clean),
  unsent pool ~70k, seed runway 1,626,479 (ARMED, sleeping)
- Operator's archive: backed up OFF this machine (done). `~/operator_takeout/`
  still needs to be grabbed by the operator.
- Quasar (partner's 2nd machine): went dark 07-28; needs the step in §2.

## 1. Telegram go-live (the only blocked item)

The system has NO live telegram bot yet (token conflict lesson: exactly ONE
poller per token). Steps:

1. Operator creates a TEST bot via BotFather → put token + operator's TG
   user ID into `~/ppa-leadengine/.env` (placeholders are there).
2. Test every command via the Hermes ppa plugin:
   `/ppa-status` `/ppa-report` `/ppa-scan TX FL workers 120`
   `/ppa-compile 1000 PPA_TEST` (small) → verify zip builds →
   `exports/PPA_TEST.zip` arrives as a Telegram document.
   Also test pause/resume: `/ppa-pause` → daily job skips; `/ppa-resume`.
3. When the operator approves: swap `.env` to the PARTNER's archived values
   in `~/ppa-leadengine/archives/telegram_config.json`
   (TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS), restart the gateway:
   `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`
4. Send the partner a welcome message with the command list from README.md.
5. Delete `PPA_TEST*` artifacts and restore sent pools if the test marked
   any phones (check `exports/dedup_reference/all_sent_phones.json` count:
   must be 2,413,563 — restore from git history if a test inflated it).

## 2. Quasar (second machine)

1. Get its IP: on quasar run `ipconfig getifaddr en0; launchctl list | grep ppa`
2. Verify plists landed: `~/Library/LaunchAgents/com.ppa.local-scanner.plist`
   + `com.ppa.seed-enrich.plist` (written 07-28/29 — if missing, copy from
   this Mac's ppa docs or re-create from the same content as this Mac's
   plists, with paths `/Users/quasar/ppa-leadengine`).
3. Its jobs are RunAtLoad — if loaded, the FL scanner + enricher(shard 1/2)
   start at login. Verify: `tail /Users/quasar/ppa-leadengine/exports/ppa_scan.log`
4. Update this Mac's harvest script if quasar's IP changed (it ssh's
   `quasar` — uses mDNS hostname; if that fails, put the IP in
   `~/ppa-leadengine/scripts/ops/harvest_fleet_leads.sh`).

## 3. Enrichment arming for day one

`exports/seeds/.enrich_paused` exists on BOTH machines (intentional: freezes
consumption at 20%). When the partner is ready for their first big day:
`rm exports/seeds/.enrich_paused` (here) + same on quasar.
Lanes auto-resume (KeepAlive, offset-resume). ~40-55k phones expected.

## 4. Final sanitization (only after TG is live + operator grabbed takeout)

Run `~/ppa-leadengine/scripts/ops/prepare_handoff.sh --execute`
(removes operator repos/plists/secrets). Verify afterwards:
`launchctl list | grep hermesleadengine` → empty; `ls ~` → only ppa dirs.

## 5. Operator takeout

`~/operator_takeout/` (florida docs, QUASAR handoff, real-estate sources,
security/) — the operator grabs this directory before §4.

## Commands reference (for the partner)

- Dashboard: http://<this-mac-ip>:8080
- Daily batch: automatic 05:45 → telegram, or `/ppa-compile 167000`
- Report: `/ppa-report`
- Scan control: `/ppa-scan TX FL workers 120`
- Pause/resume: `/ppa-pause` / `/ppa-resume`

## Do NOT

- Do not run two telegram pollers on one token (conflict = both die).
- Do not delete exports/seeds/.enrich_paused before the partner is ready.
- Do not modify already_sent_db.csv (their delivery proof, 2.4M rows).
- Do not give the partner any HermesLeadEngine main-repo tooling (deal scope).
