#!/usr/bin/env python3
"""Build the unified 60-day cycle bank.

Merges every send-date store into ONE dated map (deduped by normalized phone,
most-recent date wins), and stamps undated legacy rows from already_sent_db.csv
with the operator-asserted synthetic date (2026-07-20 = "gathered 9 days ago",
fresh at day 51 of the 60-day cycle).

READ-ONLY on all source files (already_sent_db.csv, delivery_ledger.json,
all_sent_phones.json are never modified). Output:
  exports/dedup_reference/cycle_bank.json
    {cycle_days, built_at, synthetic_legacy_date, phone_dates: {phone: iso}}
Idempotent: rebuild any time; report/digest tooling reads this file.
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "exports" / "dedup_reference" / "cycle_bank.json"
SYNTHETIC = "2026-07-20T00:00:00"  # operator assertion: legacy gathered 2026-07-20
CYCLE_DAYS = 60


def _norm(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def main() -> None:
    bank: dict[str, str] = {}
    stats = {"csv_dated": 0, "csv_legacy_synthetic": 0, "ledger_phone_dates": 0, "ledger_bare_keys": 0}

    # 1) already_sent_db.csv — dated rows keep their real date; undated rows
    #    get the synthetic legacy date (operator assertion).
    with open(ROOT / "exports" / "already_sent_db.csv", newline="", errors="replace") as f:
        for row in csv.DictReader(f):
            n = _norm(row.get("phone", ""))
            if not n:
                continue
            d = (row.get("sent_date") or "").strip()
            if len(d) >= 10 and d[4] == "-":
                if n not in bank or d > bank[n]:
                    bank[n] = d
                stats["csv_dated"] += 1
            else:
                bank.setdefault(n, SYNTHETIC)
                stats["csv_legacy_synthetic"] += 1

    # 2) delivery_ledger.json — phone_dates map + bare top-level phone keys.
    ledger = json.load(open(ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"))
    for n, d in (ledger.get("phone_dates") or {}).items():
        n = _norm(n)
        if n and (n not in bank or d > bank[n]):
            bank[n] = d
        stats["ledger_phone_dates"] += 1
    for k, v in ledger.items():
        if k in ("deliveries", "phone_dates"):
            continue
        n = _norm(k)
        if n and isinstance(v, str) and len(v) >= 10 and v[4] == "-":
            if n not in bank or v > bank[n]:
                bank[n] = v
            stats["ledger_bare_keys"] += 1

    bank = {n: d for n, d in bank.items() if n}

    # freshness waves (most-recent date + 60d)
    today = time.strftime("%Y-%m-%d")
    import datetime as dt
    t = dt.date.fromisoformat(today)
    waves: dict[str, int] = {}
    for d in bank.values():
        fresh = dt.date.fromisoformat(d[:10]) + dt.timedelta(days=CYCLE_DAYS)
        waves[fresh.isoformat()] = waves.get(fresh.isoformat(), 0) + 1

    tmp = OUT.with_suffix(".tmp")
    json.dump({
        "cycle_days": CYCLE_DAYS,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "synthetic_legacy_date": SYNTHETIC,
        "phone_dates": bank,
    }, open(tmp, "w"))
    tmp.replace(OUT)

    print(f"cycle bank built: {len(bank):,} phones")
    print(f"  sources: {stats}")
    print("  freshness waves:")
    for day in sorted(waves):
        left = (dt.date.fromisoformat(day) - t).days
        print(f"    {day} ({'now' if left <= 0 else f'in {left}d'}): {waves[day]:,}")


if __name__ == "__main__":
    main()
