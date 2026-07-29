#!/usr/bin/env python3
"""Purge pool rows outside the allowed-state allowlist (WV, TX, OH, FL, LA, AZ).

Compliance purge (operator directive 2026-07-29): only WV, TX, OH, FL, LA, AZ
leads may exist in usable pools. Rewrites CSVs in place (atomic tmp+replace),
dropping rows rejected by ppa_compile.is_blocked — i.e. any non-empty state
outside the allowlist, or a blocked (NY/CA/AL/AR) area code even on blank
state. Blank-state rows with clean area codes are kept (counted separately).

Covers: fleet_harvest, standard_pool, seeds, day3batch, fresh_1m.
Does NOT touch sent-proof stores (already_sent_db.csv, dedup_reference/*.json):
already-sent records stay as permanent dedup armor.

Idempotent. Backup lives at archives/purge_backup_20260729/.
"""

from __future__ import annotations

import csv
import glob
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppa_compile import is_blocked  # single source of truth: allowlist + area codes

LOCATIONS = [
    ("fleet_harvest", "exports/fleet_harvest/node_*.csv"),
    ("standard_pool", "exports/standard_pool/*.csv"),
    ("seeds", "exports/seeds/seed_pool.csv"),
    ("day3batch", "exports/day3batch/*.csv"),
    ("fresh_1m", "exports/fresh_1m/*.csv"),
]


def purge_file(fn: str) -> tuple[int, int, Counter, int]:
    """Returns (kept, dropped, dropped_by_state, empty_state_kept)."""
    with open(fn, newline="", errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return 0, 0, Counter(), 0
        try:
            si = next(i for i, h in enumerate(header) if h.strip().lower() == "state")
        except StopIteration:
            return 0, 0, Counter(), 0  # no state column — leave untouched
        try:
            pi = next(i for i, h in enumerate(header) if h.strip().lower() == "phone")
        except StopIteration:
            pi = None
        kept_rows, dropped, dropped_by, empty_kept = [], 0, Counter(), 0
        for row in reader:
            state = (row[si] if si < len(row) else "").strip().upper()
            phone = row[pi] if pi is not None and pi < len(row) else ""
            if is_blocked(state, phone):
                dropped += 1
                dropped_by[state or "AREA-CODE"] += 1
            elif not state:
                empty_kept += 1
                kept_rows.append(row)
            else:
                kept_rows.append(row)
    if dropped:
        tmp = fn + ".purge_tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(kept_rows)
        os.replace(tmp, fn)
    return len(kept_rows), dropped, dropped_by, empty_kept


def main() -> None:
    grand_kept = grand_dropped = 0
    all_dropped: Counter = Counter()
    for name, pattern in LOCATIONS:
        kept = dropped = empty_kept = 0
        by_state: Counter = Counter()
        for fn in sorted(glob.glob(str(ROOT / pattern))):
            k, d, bs, e = purge_file(fn)
            kept += k; dropped += d; by_state += bs; empty_kept += e
        grand_kept += kept; grand_dropped += dropped; all_dropped += by_state
        states = ", ".join(f"{s}:{n:,}" for s, n in by_state.most_common()) or "—"
        print(f"{name:14s} kept={kept:>9,}  dropped={dropped:>7,}  (empty-state kept: {empty_kept:,})  [{states}]")
    print(f"\nTOTAL: kept {grand_kept:,} · dropped {grand_dropped:,}")
    print("dropped states:", ", ".join(sorted(all_dropped)) or "none")


if __name__ == "__main__":
    main()
