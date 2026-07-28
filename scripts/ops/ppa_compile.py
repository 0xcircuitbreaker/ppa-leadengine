#!/usr/bin/env python3
"""ppa batch compiler: dedupe (sent pool) -> validate (area codes, junk,
blocked states) -> state-segregated CSVs -> zip. Slim partner format only:
priority,business_name,phone,phone_type,category,city,state

Usage: ppa_compile.py [volume] [name]
Reads config/scan_params.json for defaults.
"""

from __future__ import annotations

import csv
import glob
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ["priority", "business_name", "phone", "phone_type", "category", "city", "state"]

_blocked = json.load(open(ROOT / "config" / "blocked_states.json"))
BN = set(_blocked["blocked_state_names"])
BA = {a for acs in _blocked["blocked_area_codes"].values() for a in acs}
JUNK = ("vinted", "wikipedia", "amazon", "ebay", "walmart", "target", "bestbuy",
        "nordstrom", "craigslist", "indeed", "glassdoor", "ziprecruiter",
        "nytimes", "cnn.com", "bbc.", "forbes", "webmd", "mayoclinic",
        "britannica", "healthline", "finance.yahoo", "investing.com")
JTLD = (".be", ".nl", ".de", ".fr", ".it", ".es", ".co.uk", ".ca", ".au",
        ".nz", ".ie", ".ch", ".at", ".se", ".no", ".dk", ".pl", ".ru",
        ".cn", ".jp", ".kr", ".in", ".br", ".mx", ".za")

norm = lambda p: re.sub(r"\D", "", p or "")[-10:] if len(re.sub(r"\D", "", p or "")) >= 10 else ""


def fmt(d):
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


def is_blocked(state, phone):
    if (state or "").strip().upper() in BN:
        return True
    d = re.sub(r"\D", "", str(phone or ""))
    return len(d) >= 10 and d[-10:-7] in BA


def is_junk(url):
    if not url:
        return False
    m = re.match(r"https?://(?:www\.)?([^/]+)", url.lower())
    d = m.group(1) if m else ""
    return any(d.endswith(t) for t in JTLD) or any(j in d for j in JUNK)


def load_sent() -> set:
    sent = set()
    for f in ("all_sent_phones.json", "good_phones.json", "sent_baseline_v6.json"):
        fp = ROOT / "exports" / "dedup_reference" / f
        if fp.exists():
            sent |= {norm(p) for p in json.load(open(fp))}
    sent.discard("")
    return sent


def load_acs() -> dict:
    src = open(ROOT / "scripts" / "ops" / "self_proxy_scanner.py").read()
    m = re.search(r"STATE_ACS = \{(.+?)\n\}", src, re.S)
    acs = {}
    for sm in re.finditer(r'"([A-Z]{2})":\{([^}]+)\}', m.group(1)):
        acs[sm.group(1)] = set(re.findall(r'"(\d{3})"', sm.group(2)))
    return acs


def main() -> None:
    params = json.load(open(ROOT / "config" / "scan_params.json"))
    volume = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else int(params["daily_volume_target"])
    name = sys.argv[2] if len(sys.argv) > 2 else "PPA_DAILY"
    valid_states = set(params["states"])
    acs = load_acs()
    sent = load_sent()

    new: dict[str, dict] = {}
    for fn in sorted(glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv"))):
        with open(fn, errors="replace") as f:
            for r in csv.DictReader(f):
                n = norm(r.get("phone", ""))
                st = (r.get("state") or "").strip().upper()
                if not n or n in sent or n in new or st not in valid_states:
                    continue
                if is_blocked(st, n) or is_junk(r.get("website", "")):
                    continue
                if st in acs and acs[st] and n[:3] not in acs[st]:
                    continue
                new[n] = {"business_name": (r.get("business_name") or "")[:80],
                          "phone": fmt(n), "category": (r.get("category") or "business")[:40],
                          "city": (r.get("city") or "").title(), "state": st}

    # ---- 60-day fresh cycle: phones shipped >cycle_days ago become reusable.
    # NEW always wins; fresh fills the rest (priority 'standard').
    cycle_days = int(params.get("cycle_days", 60))
    import time as _t
    cutoff = _t.time() - cycle_days * 86400
    ledger_file = ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"
    phone_dates = {}
    if ledger_file.exists():
        try:
            phone_dates = json.load(open(ledger_file)).get("phone_dates", {})
        except Exception:  # noqa: BLE001
            phone_dates = {}
    eligible = set()
    for p, ts in phone_dates.items():
        n = norm(p)
        if not n or n in new:
            continue
        try:
            shipped_ts = _t.mktime(_t.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:  # noqa: BLE001
            continue
        if shipped_ts <= cutoff:
            eligible.add(n)

    fresh: dict[str, dict] = {}
    if eligible or True:
        # Legacy pool without ledger dates: found_at is the cycle timestamp
        # (phone was shipped after it was found -> slightly conservative).
        for fn in sorted(glob.glob(str(ROOT / "exports" / "standard_pool" / "*.csv"))):
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    st = (r.get("state") or "").strip().upper()
                    if not n or n in fresh or n in new or st not in valid_states:
                        continue
                    if n in eligible:
                        pass
                    else:
                        if n not in sent:
                            continue
                        try:
                            fts = _t.mktime(_t.strptime((r.get("found_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
                        except Exception:  # noqa: BLE001
                            continue
                        if fts > cutoff:
                            continue
                    if is_blocked(st, n) or is_junk(r.get("website", "")):
                        continue
                    if st in acs and acs[st] and n[:3] not in acs[st]:
                        continue
                    fresh[n] = {"business_name": (r.get("business_name") or "")[:80],
                                "phone": fmt(n), "category": (r.get("category") or "business")[:40],
                                "city": (r.get("city") or "").title(), "state": st}

    new_pct = float(params.get("daily_new_pct", 0.30))
    new_budget = min(len(new), round(volume * new_pct))
    new_rows = list(new.values())
    by_state: dict[str, list] = {}
    for r in new_rows:
        by_state.setdefault(r["state"], []).append(r)
    picked_new = []
    total_new = len(new_rows)
    for st in sorted(by_state, key=lambda s: -len(by_state[s])):
        quota = round(new_budget * len(by_state[st]) / max(total_new, 1))
        picked_new.extend(by_state[st][:quota])
    picked_new = picked_new[:new_budget]

    need = volume - len(picked_new)
    fresh_rows = list(fresh.values())
    by_state = {}
    for r in fresh_rows:
        by_state.setdefault(r["state"], []).append(r)
    picked_fresh = []
    total_fresh = len(fresh_rows)
    for st in sorted(by_state, key=lambda s: -len(by_state[s])):
        quota = round(need * len(by_state[st]) / max(total_fresh, 1))
        picked_fresh.extend(by_state[st][:quota])
    picked_fresh = picked_fresh[:need]

    picked = ([{"priority": "high", **r} for r in picked_new] +
              [{"priority": "standard", **r} for r in picked_fresh])
    print(f"NEW {len(picked_new):,} + FRESH({cycle_days}d cycle) {len(picked_fresh):,} = {len(picked):,}")

    out = ROOT / "exports" / name
    out.mkdir(exist_ok=True)
    for f in out.glob("*.csv"):
        f.unlink()
    by_state = {}
    for r in picked:
        by_state.setdefault(r["state"], []).append(r)
    max_rows = int(params.get("max_rows_per_file", 10000))
    zp = ROOT / "exports" / f"{name}.zip"
    if zp.exists():
        zp.unlink()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for st in sorted(by_state):
            chunk_rows = by_state[st]
            b = 1
            for i in range(0, len(chunk_rows), max_rows):
                chunk = chunk_rows[i:i + max_rows]
                fn = out / f"{name}_{st}_{b}_{len(chunk)}.csv"
                with open(fn, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                    w.writeheader()
                    for r in chunk:
                        w.writerow({**r, "phone_type": "mobile"})
                z.write(fn, fn.name)
                b += 1

    # mark shipped phones sent (build = delivery on this machine)
    sent_file = ROOT / "exports" / "dedup_reference" / "all_sent_phones.json"
    all_sent = set(json.load(open(sent_file))) if sent_file.exists() else set()
    shipped = {norm(r["phone"]) for r in picked} - {""}
    all_sent |= shipped
    json.dump(sorted(all_sent), open(sent_file, "w"))
    ledger_file = ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"
    import time as _t
    ledger = json.load(open(ledger_file)) if ledger_file.exists() else {"deliveries": [], "phone_dates": {}}
    today = _t.strftime("%Y-%m-%dT%H:%M:%S")
    ledger.setdefault("deliveries", []).append({"batch": name, "count": len(shipped), "shipped_at": today})
    for p in shipped:
        ledger.setdefault("phone_dates", {})[p] = today
    json.dump(ledger, open(ledger_file, "w"))

    print(f"{name}: {len(picked):,} records | zip {zp.stat().st_size / 1048576:.1f}MB")
    print("states:", dict(Counter(r["state"] for r in picked).most_common()))


if __name__ == "__main__":
    main()
