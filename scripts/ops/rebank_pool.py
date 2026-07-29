#!/usr/bin/env python3
"""ppa re-bank pool builder — maintains rebank_pool.json: the sent leads
that become re-deliverable after scan_params.cycle_days (default 60).

Source: exports/already_sent_db.csv (rows WITH full lead data) +
dedup_reference/delivery_ledger.json (authoritative sent dates where present).
Output: exports/dedup_reference/rebank_pool.json
  { "cycle_days": 60, "built_at": ..., "total_tracked": N,
    "eligible_now": N, "eligible_by_date": {"2026-09-25": 12345, ...},
    "rows": [ {phone, business_name, category, city, state, sent_date,
               eligible_at, eligible_now} ... only eligible_now=true ] }

The telegram button flow (inject_allowlist.json) draws from "rows" — the
currently-eligible set. Leads NEVER auto-inject without operator approval.
Runs daily via launchd (com.ppa.rebank-pool).
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SENT_DB = ROOT / "exports" / "already_sent_db.csv"
LEDGER = ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"
OUT = ROOT / "exports" / "dedup_reference" / "rebank_pool.json"


def main() -> None:
    params = json.load(open(ROOT / "config" / "scan_params.json"))
    cycle_days = int(params.get("cycle_days", 60))
    now = time.time()
    pdates = {}
    if LEDGER.exists():
        try:
            pdates = json.load(open(LEDGER)).get("phone_dates", {})
        except Exception:  # noqa: BLE001
            pdates = {}
    # cycle_bank.json (button-flow phone->date map, incl. synthetic legacy dates)
    # supplements the ledger: its dates win where the ledger has none.
    cycle_bank = ROOT / "exports" / "dedup_reference" / "cycle_bank.json"
    if cycle_bank.exists():
        try:
            cb = json.load(open(cycle_bank)).get("phone_dates", {})
            for p, ts in cb.items():
                pdates.setdefault(p, ts)
        except Exception:  # noqa: BLE001
            pass

    tracked = 0
    eligible_rows = []
    by_date = Counter()
    for r in csv.DictReader(open(SENT_DB, errors="replace")):
        name = (r.get("business_name") or "").strip()
        if not name:
            continue  # phone-only legacy rows can't re-bank as full leads
        phone = r.get("phone", "")
        n = "".join(c for c in phone if c.isdigit())[-10:]
        if not n:
            continue
        sent_date = pdates.get(n) or (r.get("sent_date") or "")
        if not sent_date:
            continue
        try:
            ts = time.mktime(time.strptime(sent_date[:10], "%Y-%m-%d"))
        except Exception:  # noqa: BLE001
            continue
        tracked += 1
        elig = ts + cycle_days * 86400
        elig_day = time.strftime("%Y-%m-%d", time.localtime(elig))
        by_date[elig_day] += 1
        if elig <= now:
            eligible_rows.append({
                "phone": phone, "business_name": name[:80],
                "category": (r.get("category") or "")[:40],
                "city": r.get("city", ""), "state": r.get("state", ""),
                "sent_date": sent_date[:10], "eligible_at": elig_day,
            })

    out = {
        "cycle_days": cycle_days,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_tracked": tracked,
        "eligible_now": len(eligible_rows),
        "eligible_by_date": dict(sorted(by_date.items())),
        "rows": eligible_rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT, "w"))
    print(f"rebank pool: {tracked:,} tracked | eligible now {len(eligible_rows):,} | "
          f"next: {min(by_date) if by_date else '-'} (+{by_date[min(by_date)]:,})" if by_date else "rebank pool: 0")


if __name__ == "__main__":
    main()
