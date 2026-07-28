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
    volume = int(sys.argv[1]) if len(sys.argv) > 1 else int(params["daily_volume_target"])
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

    rows = list(new.values())
    by_state: dict[str, list] = {}
    for r in rows:
        by_state.setdefault(r["state"], []).append(r)
    total = len(rows)
    picked = []
    for st in sorted(by_state, key=lambda s: -len(by_state[s])):
        quota = round(volume * len(by_state[st]) / max(total, 1))
        picked.extend(by_state[st][:quota])
    picked = picked[:volume]

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
                        w.writerow({"priority": "high", **r, "phone_type": "mobile"})
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
