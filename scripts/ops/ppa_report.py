#!/usr/bin/env python3
"""ppa daily report: scanned/found/NEW/sent counts, per-state, disk.
Prints a compact telegram-ready message. No internals, no sources."""

from __future__ import annotations

import csv
import glob
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
    for fn in glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")):
        try:
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    if not n or n in sent:
                        continue
                    unsent += 1
                    if (r.get("found_at") or "")[:10] == today:
                        new_today[(r.get("state") or "?").upper()] += 1
        except Exception:  # noqa: BLE001
            continue
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()[-1].split()
    sent_total = len(sent)
    lines = [
        f"PPA LEAD ENGINE — daily report {today}",
        f"NEW today: {sum(new_today.values()):,}",
        f"  by state: {dict(new_today.most_common(10))}",
        f"unsent pool: {unsent:,}",
        f"total sent (all time): {sent_total:,}",
        f"disk free: {df[3]}",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
