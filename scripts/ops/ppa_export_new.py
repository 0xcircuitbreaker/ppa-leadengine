#!/usr/bin/env python3
"""ppa export: current unsent lead pool -> trimmed CSV -> telegram document.

READ-ONLY export: nothing is marked sent, no pools are modified.
Output columns match the agreed client format: priority,name,number,city,state
(business_name -> name, phone -> number; phone_type/category dropped).
Priority is derived: mobile -> high, anything else -> standard.

Usage: ppa_export_new.py <chat_id>
Prints a single toast line on stdout (last line is shown by the gateway
script-button dispatch). Reads TELEGRAM_BOT_TOKEN from the repo .env.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORT_LIMIT = 50_000  # max leads per export batch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppa_compile import is_blocked  # single source of truth: allowlist + area codes


def _norm(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _load_env_token() -> str:
    if os.environ.get("PPA_TG_BOT_TOKEN"):  # prod-bridge override
        return os.environ["PPA_TG_BOT_TOKEN"]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def _mark_sent(rows: list[list[str]]) -> bool:
    """PRODUCTION ONLY (PPA_EXPORT_MARK_SENT=1, set at the client-bot swap):
    record exported phones as sent — dedup authority + delivery ledger with
    timestamps — and clear injected leads that just shipped (their 60-day
    cycle restarts). Inert in test mode. Returns True when marking ran."""
    if _env("PPA_EXPORT_MARK_SENT") != "1":
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    phones = [p for p in (_norm(r[2]) for r in rows) if p]
    if not phones:
        return False
    ded = ROOT / "exports" / "dedup_reference"
    all_sent_file = ded / "all_sent_phones.json"
    try:
        all_sent = set(json.load(open(all_sent_file))) if all_sent_file.exists() else set()
        all_sent.update(phones)
        tmp = all_sent_file.with_suffix(".tmp")
        json.dump(sorted(all_sent), open(tmp, "w"))
        tmp.replace(all_sent_file)
    except Exception as exc:
        print(f"mark-sent warning (all_sent): {exc}", file=sys.stderr)
    ledger_file = ded / "delivery_ledger.json"
    try:
        ledger = json.load(open(ledger_file)) if ledger_file.exists() else {}
        pdates = ledger.setdefault("phone_dates", {})
        for p in phones:
            pdates[p] = now
        ledger.setdefault("deliveries", []).append({
            "batch": f"tg-export-{time.strftime('%Y-%m-%d')}",
            "count": len(phones), "shipped_at": now,
            "format": "priority,name,number,city,state", "channel": "telegram-export",
        })
        tmp = ledger_file.with_suffix(".tmp")
        json.dump(ledger, open(tmp, "w"))
        tmp.replace(ledger_file)
    except Exception as exc:
        print(f"mark-sent warning (ledger): {exc}", file=sys.stderr)
    inject_file = ded / "inject_allowlist.json"
    if inject_file.exists():
        try:
            injected = json.load(open(inject_file))
            for p in phones:
                injected.pop(p, None)
            tmp = inject_file.with_suffix(".tmp")
            json.dump(injected, open(tmp, "w"))
            tmp.replace(inject_file)
        except Exception as exc:
            print(f"mark-sent warning (inject clear): {exc}", file=sys.stderr)
    return True


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


def main() -> None:
    chat_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not chat_id:
        raise SystemExit("usage: ppa_export_new.py <chat_id>")

    sent = _load_sent()
    seen: set = set()
    rows: list[list[str]] = []
    pool_files = (
        glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv"))
        + glob.glob(str(ROOT / "exports" / "standard_pool" / "*.csv"))
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
                    priority = (r.get("priority") or "").strip() or (
                        "high" if (r.get("phone_type") or "").strip().lower() == "mobile" else "standard"
                    )
                    rows.append([
                        priority,
                        (r.get("business_name") or "").strip(),
                        (r.get("phone") or "").strip(),
                        (r.get("city") or "").strip(),
                        (r.get("state") or "").strip(),
                    ])
        except Exception:
            continue

    if not rows:
        print("No new leads right now — pool is empty.")
        return

    # Operator-injected refreshed leads (button flow): sent leads explicitly
    # cleared for re-contact — included with priority "cycle".
    inject_file = ROOT / "exports" / "dedup_reference" / "inject_allowlist.json"
    if inject_file.exists():
        try:
            injected = {_norm(p) for p in json.load(open(inject_file))}
        except Exception:
            injected = set()
        injected.discard("")
        injected -= seen
        if injected:
            try:
                with open(ROOT / "exports" / "already_sent_db.csv", newline="", errors="replace") as f:
                    for r in csv.DictReader(f):
                        n = _norm(r.get("phone", ""))
                        if n not in injected or n in seen:
                            continue
                        st = (r.get("state") or "").strip().upper()
                        if is_blocked(st, n):
                            continue
                        seen.add(n)
                        rows.append([
                            "cycle",
                            (r.get("business_name") or "").strip(),
                            (r.get("phone") or "").strip(),
                            (r.get("city") or "").strip(),
                            st,
                        ])
            except Exception as exc:
                print(f"inject include warning: {exc}", file=sys.stderr)

    rows.sort(key=lambda x: (x[0] != "high", x[4], x[3]))
    rows = rows[:EXPORT_LIMIT]
    stamp = time.strftime("%Y-%m-%d")
    out = Path("/tmp") / f"PPA_New_Leads_{stamp}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["priority", "name", "number", "city", "state"])
        w.writerows(rows)

    token = _load_env_token()
    marking = _env("PPA_EXPORT_MARK_SENT") == "1"
    boundary = "----ppaexport"
    file_data = out.read_bytes()
    caption = (f"New leads export — {len(rows):,} leads (batch limit {EXPORT_LIMIT:,}).\n"
               "Columns: priority, name, number, city, state.\n"
               + ("These leads are now marked as sent." if marking
                  else "Export only — these leads have NOT been marked as sent."))
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{out.name}"\r\n'
        "Content-Type: text/csv\r\n\r\n".encode() + file_data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=60))
    if not resp.get("ok"):
        print(f"telegram send failed: {resp}", file=sys.stderr)
        sys.exit(1)
    marked = _mark_sent(rows)  # inert unless PPA_EXPORT_MARK_SENT=1
    print(f"✓ Lead file sent — {len(rows):,} new leads ({'marked sent' if marked else 'not marked sent'})")


if __name__ == "__main__":
    main()
