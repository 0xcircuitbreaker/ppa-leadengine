#!/usr/bin/env python3
"""ppa digest: build + send the executive daily report (with action buttons).

Single source of truth for the report format — called by the 08:00 EST daily
job (scripts/ops/daily_digest.sh) and by the "Request new report NOW" telegram
button (~/.hermes/scripts/callback-buttons/ppa_report_now.sh).

All counts are computed fresh at send time. The unsent pool mirrors
ppa_compile's source set (fleet_harvest + standard_pool, minus sent, deduped).
READ-ONLY: sends a telegram message; never touches pools or sent stores.

Usage: ppa_digest.py <chat_id>
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppa_compile import is_blocked  # single source of truth: allowlist + area codes
try:
    from ppa_workers import workers_line
except Exception:  # digest must never fail on worker-status issues
    workers_line = None


def _norm(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _bot_token() -> str:
    """Prod-bridge override (PPA_TG_BOT_TOKEN) else the default test bot."""
    return os.environ.get("PPA_TG_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN")


def _load_sent() -> set:
    sent: set = set()
    for f in ("all_sent_phones.json", "good_phones.json", "sent_baseline_v6.json"):
        fp = ROOT / "exports" / "dedup_reference" / f
        if fp.exists():
            try:
                sent |= {_norm(p) for p in json.load(open(fp))}
            except Exception:
                continue
    sent.discard("")
    return sent


def _pool_stats(sent: set) -> tuple[int, Counter]:
    """Unsent pool size + new-today-by-state, over the compile source set."""
    today = time.strftime("%Y-%m-%d")
    new_today: Counter = Counter()
    seen: set = set()
    unsent = 0
    pool_files = glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")) + glob.glob(
        str(ROOT / "exports" / "standard_pool" / "*.csv")
    )
    for fn in pool_files:
        try:
            with open(fn, newline="", errors="replace") as f:
                for r in csv.DictReader(f):
                    n = _norm(r.get("phone", ""))
                    if not n or n in sent or n in seen:
                        continue
                    state = (r.get("state") or "").strip().upper()
                    if is_blocked(state, r.get("phone", "")):
                        continue  # strict allowlist + area-code screen (2026-07-29)
                    seen.add(n)
                    unsent += 1
                    if (r.get("found_at") or "")[:10] == today:
                        new_today[(r.get("state") or "?").upper()] += 1
        except Exception:
            continue
    return unsent, new_today


def _staged_rows() -> int:
    total = 0
    for fn in glob.glob(str(ROOT / "exports" / "day3batch" / "*.csv")):
        try:
            with open(fn, errors="replace") as f:
                total += max(0, sum(1 for _ in f) - 1)
        except Exception:
            continue
    return total


def _seed_count() -> int:
    try:
        with open(ROOT / "exports" / "seeds" / "seed_pool.csv", errors="replace") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


def _bank_stats() -> tuple[int, str, int, int]:
    """(bank_total, first_wave_date, first_wave_count, days_until)."""
    fp = ROOT / "exports" / "dedup_reference" / "cycle_bank.json"
    if not fp.exists():
        return 0, "", 0, 0
    bank = json.load(open(fp)).get("phone_dates", {})
    cycle = int(json.load(open(fp)).get("cycle_days", 60))
    today = dt.date.fromtimestamp(time.time())
    waves: Counter = Counter()
    for d in bank.values():
        try:
            waves[(dt.date.fromisoformat(d[:10]) + dt.timedelta(days=cycle)).isoformat()] += 1
        except Exception:
            continue
    future = {d: c for d, c in waves.items() if (dt.date.fromisoformat(d) - today).days > 0}
    if not future:
        return len(bank), "", 0, 0
    first = min(future)
    return len(bank), first, future[first], (dt.date.fromisoformat(first) - today).days


def main() -> None:
    chat_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not chat_id:
        raise SystemExit("usage: ppa_digest.py <chat_id>")
    token = _bot_token()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")

    sent = _load_sent()
    pool, new_today = _pool_stats(sent)
    staged = _staged_rows()
    delivered = len(json.load(open(ROOT / "exports" / "dedup_reference" / "all_sent_phones.json")))
    bank, wave_date, wave_n, wave_days = _bank_stats()
    seeds = _seed_count()

    today_label = time.strftime("%A, %B ") + str(int(time.strftime("%d"))) + time.strftime(", %Y")
    new_total = sum(new_today.values())
    new_states = ", ".join(f"{s} {c:,}" for s, c in new_today.most_common(3)) or "—"
    bank_short = f"{bank / 1e6:.2f}M" if bank >= 1e6 else f"{bank:,}"
    wave_short = f"{wave_n / 1e6:.1f}M" if wave_n >= 1e6 else f"{wave_n:,}"
    wave_date_label = dt.date.fromisoformat(wave_date).strftime("%b %-d") if wave_date else "—"

    headline = f"{pool:,} leads ready."
    if bank and wave_n:
        headline += f" Re-contact bank: {bank_short} — first {wave_short} turn fresh in {wave_days} days."

    try:
        workers = workers_line() if workers_line else "status unavailable"
    except Exception:
        workers = "status unavailable"

    text = (
        f"<b>PPA Lead Engine — Daily Report</b>\n{today_label}\n\n"
        f"<b>{headline}</b>\n\n"
        f"New leads today: {new_total:,} ({new_states})\n"
        f"Ready to send: {pool:,}\n"
        f"Staged batches: {staged:,}\n"
        f"Delivered to date: {delivered:,}\n"
        f"Re-contact bank: {bank:,} — first {wave_short} fresh {wave_date_label} ({wave_days}d)\n"
        f"Seed reserve: {seeds / 1e6:.2f}M untouched\n"
        f"Workers: {workers}\n\n"
        "Nothing is sent automatically — export or refresh below."
    )

    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [
            [{"text": "📥 Export new leads (CSV)", "callback_data": "cb:ppa_export"}],
            [{"text": "🔄 Request new report NOW", "callback_data": "cb:ppa_report_now"}],
            [{"text": "♻️ Add refreshed leads…", "callback_data": "cb:ppa_inject"}],
            [{"text": "🖥 Workers breakdown", "callback_data": "cb:ppa_workers"}],
        ]},
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        print(f"telegram send failed: {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Report sent — {pool:,} ready, bank {bank_short}")


if __name__ == "__main__":
    main()
