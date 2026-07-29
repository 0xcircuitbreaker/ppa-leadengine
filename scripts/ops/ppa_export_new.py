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


def _norm(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _load_env_token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")


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
    for fn in glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")):
        try:
            with open(fn, newline="", errors="replace") as f:
                for r in csv.DictReader(f):
                    n = _norm(r.get("phone", ""))
                    if not n or n in sent or n in seen:
                        continue
                    seen.add(n)
                    priority = "high" if (r.get("phone_type") or "").strip().lower() == "mobile" else "standard"
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

    rows.sort(key=lambda x: (x[0] != "high", x[4], x[3]))
    stamp = time.strftime("%Y-%m-%d")
    out = Path("/tmp") / f"PPA_New_Leads_{stamp}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["priority", "name", "number", "city", "state"])
        w.writerows(rows)

    token = _load_env_token()
    boundary = "----ppaexport"
    file_data = out.read_bytes()
    caption = (f"New leads export — {len(rows):,} leads.\n"
               "Columns: priority, name, number, city, state.\n"
               "Export only — these leads have NOT been marked as sent.")
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
    print(f"✓ Lead file sent — {len(rows):,} new leads (not marked sent)")


if __name__ == "__main__":
    main()
