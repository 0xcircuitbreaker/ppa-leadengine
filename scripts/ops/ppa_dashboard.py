#!/usr/bin/env python3
"""ppa LAN dashboard — read-only, NO auth (operator directive), LAN-only.

Binds 0.0.0.0:8080 so other machines on the same WiFi/LAN can view it.
Safety: rejects non-private client IPs (RFC1918 + loopback + link-local),
so even if this machine ever sits on a public IP, internet clients are
refused. No internet exposure is intended or provided.

Auto-run: com.ppa.dashboard launchd (KeepAlive).
"""

from __future__ import annotations

import csv
import glob
import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = 8080
PRIVATE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|169\.254\.|::1|fc|fd|fe80)")

norm = lambda p: re.sub(r"\D", "", p or "")[-10:] if len(re.sub(r"\D", "", p or "")) >= 10 else ""


def _sent() -> set:
    sent = set()
    for f in ("all_sent_phones.json", "good_phones.json", "sent_baseline_v6.json"):
        fp = ROOT / "exports" / "dedup_reference" / f
        if fp.exists():
            sent |= {norm(p) for p in json.load(open(fp))}
    sent.discard("")
    return sent


def _stats() -> dict:
    sent = _sent()
    today = time.strftime("%Y-%m-%d")
    new_today, by_state = {}, {}
    hourly = {}
    unsent = 0
    for fn in glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")):
        try:
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    if not n or n in sent:
                        continue
                    unsent += 1
                    st = (r.get("state") or "?").upper()
                    by_state[st] = by_state.get(st, 0) + 1
                    h = (r.get("found_at") or "")[:13]
                    if h:
                        hourly[h] = hourly.get(h, 0) + 1
                    if (r.get("found_at") or "")[:10] == today:
                        new_today[st] = new_today.get(st, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    zips = []
    for z in sorted(glob.glob(str(ROOT / "exports" / "*.zip")), key=lambda p: -Path(p).stat().st_mtime)[:10]:
        zp = Path(z)
        zips.append({"name": zp.name, "mb": zp.stat().st_size // 1048576,
                     "mtime": time.strftime("%m-%d %H:%M", time.localtime(zp.stat().st_mtime))})
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()[-1].split()
    launchd = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    jobs = {}
    for job in ("ppa.local-scanner", "ppa.fleet-harvest", "ppa.daily-delivery", "ppa.dashboard",
                "hermes.gateway"):
        jobs[job] = "running" if job in launchd else "STOPPED"
    params = json.load(open(ROOT / "config" / "scan_params.json"))
    # fresh-cycle eligible (60d): ledger phones older than cycle_days
    cycle_days = int(params.get("cycle_days", 60))
    cutoff = time.time() - cycle_days * 86400
    fresh_n = 0
    ledger_file = ROOT / "exports" / "dedup_reference" / "delivery_ledger.json"
    if ledger_file.exists():
        try:
            for ts in json.load(open(ledger_file)).get("phone_dates", {}).values():
                try:
                    if time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) <= cutoff:
                        fresh_n += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    return {"today": today, "new_today": new_today, "new_today_total": sum(new_today.values()),
            "unsent": unsent, "by_state": by_state, "sent_total": len(sent),
            "hourly": dict(sorted(hourly.items())[-18:]), "zips": zips,
            "disk_free": df[3], "jobs": jobs, "params": params,
            "cycle_days": cycle_days, "fresh_eligible": fresh_n,
            "paused": (ROOT / "config" / "delivery_paused.flag").exists()}


def _page(s: dict) -> str:
    state_rows = "".join(f"<tr><td>{k}</td><td>{v:,}</td><td>{s['new_today'].get(k, 0):,}</td></tr>"
                         for k, v in sorted(s["by_state"].items(), key=lambda x: -x[1]))
    hour_rows = "".join(f"<tr><td>{h[5:]}h</td><td>{c:,}</td></tr>" for h, c in s["hourly"].items())
    zip_rows = "".join(f"<tr><td><a href='/z/{z['name']}'>{z['name']}</a></td><td>{z['mb']}MB</td><td>{z['mtime']}</td></tr>"
                       for z in s["zips"])
    job_rows = "".join(f"<tr><td>{j}</td><td class='{st}'>{st}</td></tr>" for j, st in s["jobs"].items())
    return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60>
<title>PPA Lead Engine</title>
<style>body{{font-family:-apple-system,monospace;margin:2rem;background:#0d1117;color:#c9d1d9}}
h1{{color:#58a6ff}}table{{border-collapse:collapse;margin:1rem 0}}td,th{{border:1px solid #30363d;padding:4px 12px}}
.running{{color:#3fb950}}.STOPPED{{color:#f85149}}a{{color:#58a6ff}}.grid{{display:flex;gap:3rem;flex-wrap:wrap}}</style>
<h1>PPA Lead Engine — {s['today']}</h1>
<div class=grid>
<div><h2>Today</h2><table>
<tr><th>NEW today</th><td><b>{s['new_today_total']:,}</b></td></tr>
<tr><th>unsent pool</th><td>{s['unsent']:,}</td></tr>
<tr><th>fresh-cycle eligible ({s['cycle_days']}d)</th><td>{s['fresh_eligible']:,}</td></tr>
<tr><th>sent (all time)</th><td>{s['sent_total']:,}</td></tr>
<tr><th>disk free</th><td>{s['disk_free']}</td></tr>
<tr><th>delivery</th><td>{'PAUSED' if s['paused'] else 'active'}</td></tr>
<tr><th>scan states</th><td>{', '.join(s['params']['states'])}</td></tr>
<tr><th>daily target</th><td>{s['params']['daily_volume_target']:,}</td></tr>
</table></div>
<div><h2>Jobs</h2><table>{job_rows}</table></div>
<div><h2>NEW by hour (UTC)</h2><table>{hour_rows}</table></div>
<div><h2>Unsent by state</h2><table><tr><th>state</th><th>unsent</th><th>new today</th></tr>{state_rows}</table></div>
<div><h2>Batches</h2><table><tr><th>zip</th><th>size</th><th>built</th></tr>{zip_rows}</table></div>
</div>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if not PRIVATE.match(self.client_address[0]):
            self.send_error(403, "LAN only")
            return
        if self.path.startswith("/z/"):
            name = Path(self.path[3:]).name
            zp = ROOT / "exports" / name
            if zp.exists() and zp.suffix == ".zip":
                data = zp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)
            return
        try:
            body = _page(_stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
