#!/usr/bin/env python3
"""ppa_workers.py — worker status for this Mac + Quasar + the proxy fleet.

Modes:
  line                  one-line summary for embedding in the daily digest
  breakdown <chat_id>   full HTML report sent to Telegram (🖥 button action)

Sources: launchctl list (local + over ssh on Quasar), fleet_harvest node
files for fleet liveness, pause flags for enrichment/delivery state.
Token: PPA_TG_BOT_TOKEN override (prod bridge) else .env TELEGRAM_BOT_TOKEN.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/Users/a2.0/ppa-leadengine")

LOCAL_DAEMONS = ["local-scanner", "loom-lane", "dashboard", "seed-enrich", "prod-bot-bridge"]
LOCAL_PERIODICS = ["fleet-harvest", "rate-governor", "directory-discovery", "daily-digest"]
QUASAR_JOBS = ["local-scanner", "seed-enrich"]
QUASAR_SSH = "quasar"  # ssh alias → quasar@10.1.10.243


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _bot_token() -> str:
    return os.environ.get("PPA_TG_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN")


def _launchd_local() -> dict[str, tuple[bool, str]]:
    """label-suffix → (running, last_exit) for com.ppa.* jobs on this Mac."""
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    jobs: dict[str, tuple[bool, str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("com.ppa."):
            name = parts[2][len("com.ppa."):]
            jobs[name] = (parts[0].isdigit(), parts[1])
    return jobs


def _quasar() -> tuple[dict[str, tuple[bool, str]], bool, bool]:
    """(jobs, reachable, enrich_paused) from Quasar over a single ssh call."""
    cmd = (
        f"ssh -o ConnectTimeout=5 -o BatchMode=yes {QUASAR_SSH} "
        "'launchctl list; test -f ~/ppa-leadengine/exports/seeds/.enrich_paused && echo ENRICH_PAUSED'"
    )
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return {}, False, False
    if not out.strip():
        return {}, False, False
    jobs: dict[str, tuple[bool, str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("com.ppa."):
            name = parts[2][len("com.ppa."):]
            jobs[name] = (parts[0].isdigit(), parts[1])
    return jobs, True, "ENRICH_PAUSED" in out


def _fleet() -> tuple[int, str]:
    """(node count, newest harvest mtime HH:MM) from fleet_harvest node files."""
    files = glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv"))
    if not files:
        return 0, "—"
    newest = max(os.path.getmtime(f) for f in files)
    return len(files), time.strftime("%H:%M", time.localtime(newest))


def collect() -> dict:
    local = _launchd_local()
    qjobs, qok, qpaused = _quasar()
    nodes, last_harvest = _fleet()
    return {
        "local": local,
        "quasar": qjobs,
        "quasar_ok": qok,
        "quasar_enrich_paused": qpaused,
        "fleet_nodes": nodes,
        "last_harvest": last_harvest,
        "local_enrich_paused": (ROOT / "exports" / "seeds" / ".enrich_paused").exists(),
    }


def _up_down(jobs: dict[str, tuple[bool, str]], wanted: list[str]) -> tuple[list[str], list[str]]:
    up = [w for w in wanted if jobs.get(w, (False, ""))[0]]
    down = [w for w in wanted if not jobs.get(w, (False, ""))[0]]
    return up, down


def workers_line(st: dict | None = None) -> str:
    """Compact one-liner for the digest."""
    st = st or collect()
    up_d, down_d = _up_down(st["local"], LOCAL_DAEMONS)
    per_ok = sum(1 for p in LOCAL_PERIODICS if p in st["local"])
    if st["quasar_ok"]:
        up_q, down_q = _up_down(st["quasar"], QUASAR_JOBS)
        q_part = f"Quasar {len(up_q)}/{len(QUASAR_JOBS)}"
        if down_q:
            q_part += f" (⚠️ {', '.join(down_q)} down)"
    else:
        q_part = "Quasar ⚠️ unreachable"
    mac_part = f"Mac {len(up_d)}/{len(LOCAL_DAEMONS)} daemons"
    if down_d:
        mac_part += f" (⚠️ {', '.join(down_d)} down)"
    flags = []
    if st["local_enrich_paused"]:
        flags.append("enrich PAUSED")
    tail = f" · {' · '.join(flags)}" if flags else ""
    return f"{mac_part} · {q_part} · fleet {st['fleet_nodes']} nodes ({st['last_harvest']}){tail}"


def breakdown_text(st: dict) -> str:
    now = time.strftime("%a %b ") + str(int(time.strftime("%d"))) + time.strftime(", %H:%M")
    lines = [f"🖥 <b>Workers — {now}</b>", ""]

    up_d, down_d = _up_down(st["local"], LOCAL_DAEMONS)
    lines.append(f"<b>This Mac</b> — {len(up_d)}/{len(LOCAL_DAEMONS)} daemons up")
    if up_d:
        lines.append("✅ " + " · ".join(up_d))
    if down_d:
        lines.append("❌ <b>DOWN:</b> " + " · ".join(down_d))
    per_ok = [p for p in LOCAL_PERIODICS if p in st["local"]]
    per_missing = [p for p in LOCAL_PERIODICS if p not in st["local"]]
    ptxt = "⏱ periodics: " + (", ".join(per_ok) if per_ok else "none loaded")
    if per_missing:
        ptxt += f" (missing: {', '.join(per_missing)})"
    lines.append(ptxt)
    lines.append("")

    if st["quasar_ok"]:
        up_q, down_q = _up_down(st["quasar"], QUASAR_JOBS)
        lines.append(f"<b>Quasar</b> — {len(up_q)}/{len(QUASAR_JOBS)} up")
        if up_q:
            labels = [f"{w} (FL)" if w == "local-scanner" else w for w in up_q]
            lines.append("✅ " + " · ".join(labels))
        if down_q:
            lines.append("❌ <b>DOWN:</b> " + " · ".join(down_q))
    else:
        lines.append("<b>Quasar</b> — ⚠️ <b>unreachable</b> (ssh failed)")
    lines.append("")

    lines.append(f"<b>Fleet</b>: {st['fleet_nodes']} nodes reporting, latest harvest {st['last_harvest']}")
    enrich = "PAUSED" if st["local_enrich_paused"] else "active"
    if st["quasar_ok"]:
        enrich += " local/" + ("PAUSED" if st["quasar_enrich_paused"] else "active") + " quasar"
    lines.append(f"<b>Enrichment</b>: {enrich} · <b>Delivery</b>: manual-only (buttons)")
    return "\n".join(lines)


def _send(chat_id: str, text: str) -> None:
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_bot_token()}/sendMessage",
        data=body, headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        raise RuntimeError(f"telegram send failed: {resp}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "line"
    if mode == "line":
        print(workers_line())
        return
    if mode == "breakdown":
        chat_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not chat_id:
            raise SystemExit("usage: ppa_workers.py breakdown <chat_id>")
        st = collect()
        _send(chat_id, breakdown_text(st))
        up_d, down_d = _up_down(st["local"], LOCAL_DAEMONS)
        up_q, down_q = _up_down(st["quasar"], QUASAR_JOBS) if st["quasar_ok"] else ([], QUASAR_JOBS)
        total_up, total = len(up_d) + len(up_q), len(LOCAL_DAEMONS) + len(QUASAR_JOBS)
        warn = f" — ⚠️ down: {', '.join(down_d + down_q)}" if (down_d or down_q) else ""
        print(f"✓ Worker report sent — {total_up}/{total} daemons up{warn}")
        return
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
