#!/usr/bin/env python3
"""ppa daily report: scanned/found/NEW/sent counts, per-state, disk.
Prints a compact telegram-ready message. No internals, no sources."""

from __future__ import annotations

import csv
import glob
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppa_compile import is_blocked  # single source of truth: allowlist + area codes
norm = lambda p: re.sub(r"\D", "", p or "")[-10:] if len(re.sub(r"\D", "", p or "")) >= 10 else ""


def load_sent() -> set:
    sent = set()
    for f in ("all_sent_phones.json", "good_phones.json", "sent_baseline_v6.json"):
        fp = ROOT / "exports" / "dedup_reference" / f
        if fp.exists():
            sent |= {norm(p) for p in json.load(open(fp))}
    sent.discard("")
    return sent


def main() -> None:
    sent = load_sent()
    today = time.strftime("%Y-%m-%d")
    new_today: Counter = Counter()
    unsent = 0
    pool_files = glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")) + glob.glob(
        str(ROOT / "exports" / "standard_pool" / "*.csv")
    )
    for fn in pool_files:
        try:
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    if not n or n in sent:
                        continue
                    s = (r.get("state") or "").strip().upper()
                    if is_blocked(s, r.get("phone", "")):
                        continue  # strict allowlist + area-code screen (2026-07-29)
                    unsent += 1
                    if (r.get("found_at") or "")[:10] == today:
                        new_today[(r.get("state") or "?").upper()] += 1
        except Exception:  # noqa: BLE001
            continue
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()[-1].split()
    sent_total = len(sent)
    # fresh-cycle eligible (60d): prefer the unified cycle bank (all date
    # stores merged + synthetic-dated legacy); fall back to the raw ledger.
    params = json.load(open(ROOT / "config" / "scan_params.json"))
    cycle_days = int(params.get("cycle_days", 60))
    cutoff = time.time() - cycle_days * 86400
    fresh_n = 0
    bank_file = ROOT / "exports" / "dedup_reference" / "cycle_bank.json"
    ledger_file = bank_file if bank_file.exists() else ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"
    if ledger_file.exists():
        try:
            for ts in json.load(open(ledger_file)).get("phone_dates", {}).values():
                try:
                    if time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) <= cutoff:
                        fresh_n += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    lines = [
        f"PPA LEAD ENGINE — daily report {today}",
        f"NEW today: {sum(new_today.values()):,}",
        f"  by state: {dict(new_today.most_common(10))}",
        f"unsent pool: {unsent:,}",
        f"fresh-cycle eligible ({cycle_days}d): {fresh_n:,}",
        f"total sent (all time): {sent_total:,}",
        f"disk free: {df[3]}",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
