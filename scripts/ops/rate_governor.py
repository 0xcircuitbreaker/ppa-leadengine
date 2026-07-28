#!/usr/bin/env python3
"""ppa rate governor — caps system throughput at scan_params.daily_rate_cap
(default 167,000 unique phones per rolling 24h).

Every 10 min (launchd StartInterval): computes the current 24h unique-phone
rate across harvest + fresh_1m (same method as the dashboard). If the rate
exceeds the cap, SIGSTOP the producer lanes (seed_enrich, self_proxy_scanner,
loom_constant) — they freeze in place, resume-safe. When the rate decays
below 85% of the cap, SIGCONT them. Hysteresis prevents flapping.

This is a THROUGHPUT CAP, not a slowdown knob: lanes run full speed or
sleep, so per-thread behavior stays efficient and logs stay clean.
"""

from __future__ import annotations

import csv, glob, json, os, re, signal, subprocess, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "logs" / ".rate_governor_state.json"
norm = lambda p: re.sub(r"\D", "", p or "")[-10:] if len(re.sub(r"\D", "", p or "")) >= 10 else ""

LANE_PATTERNS = ["seed_enrich.py", "self_proxy_scanner.py", "loom_constant.py"]


def rate_24h() -> int:
    now = time.time()
    phones = set()
    for fn in glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")) + \
              glob.glob(str(ROOT / "exports" / "fresh_1m" / "*.csv")):
        if fn.endswith("node_local.csv"):
            continue
        try:
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    if not n:
                        continue
                    try:
                        ts = time.mktime(time.strptime((r.get("found_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:  # noqa: BLE001
                        continue
                    if (now - ts) / 3600 <= 24:
                        phones.add(n)
        except Exception:  # noqa: BLE001
            pass
    return len(phones)


def lane_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", "|".join(LANE_PATTERNS)],
                         capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if p.strip().isdigit()]


def main() -> None:
    params = json.load(open(ROOT / "config" / "scan_params.json"))
    cap = int(params.get("daily_rate_cap", 167000))
    resume_at = int(cap * 0.85)
    rate = rate_24h()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"stopped": False}
    pids = lane_pids()
    action = "none"
    if rate >= cap and not state["stopped"]:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        state["stopped"] = True
        action = f"SIGSTOP {len(pids)} lanes"
    elif rate < resume_at and state["stopped"]:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        state["stopped"] = False
        action = f"SIGCONT {len(pids)} lanes"
    STATE.write_text(json.dumps(state))
    print(f"[{time.strftime('%H:%M')}] 24h unique rate {rate:,} / cap {cap:,} | stopped={state['stopped']} | {action}", flush=True)


if __name__ == "__main__":
    main()
