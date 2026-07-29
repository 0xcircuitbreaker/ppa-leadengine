#!/usr/bin/env python3
"""ppa inject: operator-controlled injection of refreshed (60-day) leads.

The 60-day cycle NEVER auto-injects: refreshed leads enter the deliverable
pool only when the operator taps the "Add refreshed leads" button and types
a quantity (telegram ForceReply popup). This script backs that flow.

Modes:
  available            print how many refreshed leads are injectable now
  prompt  <chat_id>    send the ForceReply popup and arm the pending-input
                       state consumed by the gateway's script-pending router
  apply   <chat_id> <text>   validate the typed quantity, move that many
                       refreshed leads into inject_allowlist.json, confirm

Selection: oldest-sent first (they've waited longest), allowlist + area-code
screened (ppa_compile.is_blocked), never double-injected. Full records are
pulled from already_sent_db.csv (phone, business_name, city, state).

READ-ONLY on every pool/ledger/sent store; writes only
exports/dedup_reference/inject_allowlist.json and the pending-state file.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppa_compile import is_blocked  # single source of truth: allowlist + area codes

BANK_FILE = ROOT / "exports" / "dedup_reference" / "cycle_bank.json"
SENT_DB = ROOT / "exports" / "already_sent_db.csv"
INJECT_FILE = ROOT / "exports" / "dedup_reference" / "inject_allowlist.json"
SMOKE_FLAG = ROOT / "exports" / "dedup_reference" / ".inject_smoketest"
PENDING_DIR = Path.home() / ".hermes" / "scripts" / "callback-buttons" / "pending"
ARM_TTL = 600  # seconds the popup stays armed


def _norm(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _bot_token() -> str:
    """Prod-bridge override (PPA_TG_BOT_TOKEN) else the default test bot."""
    return os.environ.get("PPA_TG_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN")


def _send(chat_id: str, text: str, force_reply: bool = False) -> None:
    body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if force_reply:
        body["reply_markup"] = {
            "force_reply": True,
            "input_field_placeholder": "e.g. 5000",
            "selective": True,
        }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_bot_token()}/sendMessage",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        raise RuntimeError(f"telegram send failed: {resp}")


def _load_injected() -> dict:
    if INJECT_FILE.exists():
        try:
            return json.load(open(INJECT_FILE))
        except Exception:
            return {}
    return {}


def _smoke_count() -> int:
    """Smoke-test override: presence of .inject_smoketest makes the flow act
    as if N real sent leads were cycle-fresh. Content = N (default 5000).
    0/absent = production behavior. REMOVE after the rehearsal."""
    if not SMOKE_FLAG.exists():
        return 0
    try:
        raw = SMOKE_FLAG.read_text().strip()
        return int(raw) if raw.isdigit() else 5000
    except Exception:
        return 5000


def _fresh_phones() -> list[tuple[str, str]]:
    """(phone, sent_date) for cycle-bank phones whose 60 days have elapsed,
    oldest first, excluding already-injected."""
    injected = set(_load_injected())
    smoke = _smoke_count()
    if smoke:
        phones = []
        with open(SENT_DB, newline="", errors="replace") as f:
            for r in csv.DictReader(f):
                n = _norm(r.get("phone", ""))
                if not n or n in injected:
                    continue
                phones.append((n, "2026-05-01"))
                if len(phones) >= smoke:
                    break
        return phones
    if not BANK_FILE.exists():
        return []
    bank = json.load(open(BANK_FILE))
    cycle = int(bank.get("cycle_days", 60))
    today = dt.date.fromtimestamp(time.time())
    fresh = []
    for p, d in bank.get("phone_dates", {}).items():
        n = _norm(p)
        if not n or n in injected:
            continue
        try:
            sent_day = dt.date.fromisoformat(d[:10])
        except Exception:
            continue
        if (today - sent_day).days >= cycle:
            fresh.append((n, d[:10]))
    fresh.sort(key=lambda t: t[1])  # oldest first
    return fresh


def _record_map(phones: set) -> dict:
    """phone -> {business_name, city, state} from already_sent_db.csv."""
    out = {}
    if not phones:
        return out
    with open(SENT_DB, newline="", errors="replace") as f:
        for r in csv.DictReader(f):
            n = _norm(r.get("phone", ""))
            if n in phones and n not in out:
                out[n] = r
    return out


def _available() -> list[tuple[str, str]]:
    fresh = _fresh_phones()
    records = _record_map({p for p, _ in fresh})
    return [
        (p, d) for p, d in fresh
        if p in records and not is_blocked(
            (records[p].get("state") or "").strip().upper(), p)
    ]


def _first_wave() -> tuple[str, int]:
    """(date, count) of the earliest future fresh wave, for the empty state."""
    if not BANK_FILE.exists():
        return "", 0
    bank = json.load(open(BANK_FILE))
    cycle = int(bank.get("cycle_days", 60))
    today = dt.date.fromtimestamp(time.time())
    waves: dict[str, int] = {}
    for d in bank.get("phone_dates", {}).values():
        try:
            day = (dt.date.fromisoformat(d[:10]) + dt.timedelta(days=cycle)).isoformat()
        except Exception:
            continue
        if (dt.date.fromisoformat(day) - today).days > 0:
            waves[day] = waves.get(day, 0) + 1
    if not waves:
        return "", 0
    first = min(waves)
    return first, waves[first]


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "available"

    if mode == "available":
        print(len(_available()))
        return

    chat_id = sys.argv[2] if len(sys.argv) > 2 else ""
    if not chat_id:
        raise SystemExit("usage: ppa_inject_fresh.py prompt|apply <chat_id> [text]")

    if mode == "prompt":
        avail = len(_available())
        if not avail:
            day, n = _first_wave()
            note = f"first {n:,} turn fresh {dt.date.fromisoformat(day).strftime('%b %-d')}" if day else "none banked yet"
            _send(chat_id, f"♻️ <b>No refreshed leads available yet</b> — {note}.")
            print("no refreshed leads available")
            return
        tag = "🧪 SMOKE TEST — " if _smoke_count() else ""
        _send(
            chat_id,
            f"{tag}♻️ <b>{avail:,} refreshed leads available</b> (60-day cycle).\n\n"
            f"Reply with how many to add to the new-leads export (1–{avail:,}):",
            force_reply=True,
        )
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        # user id: explicit argv (bridge passes the tapper) else first allowed id
        user_id = (sys.argv[3].strip() if len(sys.argv) > 3 and sys.argv[3].strip()
                   else _env("TELEGRAM_ALLOWED_USER_IDS").split(",")[0].strip())
        (PENDING_DIR / f"{chat_id}_{user_id}.json").write_text(json.dumps({
            "verb": "ppa_inject_amount", "armed_at": time.time(),
        }))
        print(f"✓ Popup sent — {avail:,} available")
        return

    if mode == "apply":
        text = (sys.argv[3] if len(sys.argv) > 3 else "").strip().replace(",", "")
        avail = _available()
        if not text.isdigit() or not (1 <= int(text) <= len(avail)):
            _send(
                chat_id,
                f"⚠️ <b>{text or '(empty)'} is not a valid amount.</b>\n"
                f"{len(avail):,} refreshed leads available — tap ♻️ Add refreshed leads and enter 1–{len(avail):,}.",
            )
            print(f"invalid amount: {text!r} (available {len(avail)})")
            sys.exit(1)
        n = int(text)
        picked = avail[:n]
        injected = _load_injected()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for p, _d in picked:
            injected[p] = now
        tmp = INJECT_FILE.with_suffix(".tmp")
        json.dump(injected, open(tmp, "w"))
        tmp.replace(INJECT_FILE)
        tag = "🧪 SMOKE TEST — " if _smoke_count() else ""
        _send(
            chat_id,
            f"{tag}✅ <b>{n:,} refreshed leads added.</b>\n"
            f"They'll be included in your next export (marked <i>cycle</i> priority). "
            f"Total injected to date: {len(injected):,}.",
        )
        print(f"✓ Added {n:,} refreshed leads (total injected {len(injected):,})")
        return

    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
