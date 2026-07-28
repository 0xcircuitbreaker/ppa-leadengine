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
    now = time.time()
    new_today, by_state = {}, {}
    hourly, raw_hourly = {}, {}
    unsent = 0
    raw_total = 0
    raw_24h = 0
    new_24h = 0
    for fn in glob.glob(str(ROOT / "exports" / "fleet_harvest" / "node_*.csv")):
        try:
            with open(fn, errors="replace") as f:
                for r in csv.DictReader(f):
                    n = norm(r.get("phone", ""))
                    if not n:
                        continue
                    raw_total += 1
                    age_h = None
                    try:
                        ts = time.mktime(time.strptime((r.get("found_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
                        age_h = (now - ts) / 3600
                    except Exception:  # noqa: BLE001
                        pass
                    h = (r.get("found_at") or "")[:13]
                    if h:
                        raw_hourly[h] = raw_hourly.get(h, 0) + 1
                    if age_h is not None and age_h <= 24:
                        raw_24h += 1
                    if (r.get("found_at") or "")[:10] == today:
                        pass
                    if n in sent:
                        continue
                    unsent += 1
                    if age_h is not None and age_h <= 24:
                        new_24h += 1
                    st = (r.get("state") or "?").upper()
                    by_state[st] = by_state.get(st, 0) + 1
                    if h:
                        hourly[h] = hourly.get(h, 0) + 1
                    if (r.get("found_at") or "")[:10] == today:
                        new_today[st] = new_today.get(st, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    loom_total = 0
    for fn in glob.glob(str(ROOT / "exports" / "fresh_1m" / "loom_*.csv")):
        try:
            loom_total += sum(1 for _ in open(fn)) - 1
        except Exception:  # noqa: BLE001
            pass
    raw_total += loom_total
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
            "raw_total": raw_total, "raw_24h": raw_24h, "new_24h": new_24h,
            "hourly": dict(sorted(hourly.items())[-18:]),
            "raw_hourly": dict(sorted(raw_hourly.items())[-18:]),
            "zips": zips,
            "disk_free": df[3], "jobs": jobs, "params": params,
            "cycle_days": cycle_days, "fresh_eligible": fresh_n,
            "paused": (ROOT / "config" / "delivery_paused.flag").exists()}


def _page(s: dict) -> str:
    state_rows = "".join(f"<tr><td>{k}</td><td class=num>{v:,}</td><td class=num>{s['new_today'].get(k, 0):,}</td></tr>"
                         for k, v in sorted(s["by_state"].items(), key=lambda x: -x[1]))
    hour_rows = "".join(f"<tr><td class=mono>{h[5:]}h</td><td class=num>{s['raw_hourly'].get(h, 0):,}</td><td class=num>{c:,}</td></tr>"
                        for h, c in s["hourly"].items())
    zip_rows = "".join(f"<tr><td><a href='/z/{z['name']}'>{z['name']}</a></td><td class=num>{z['mb']}MB</td><td class=muted>{z['mtime']}</td></tr>"
                       for z in s["zips"])
    job_rows = "".join(f"<tr><td>{j}</td><td><span class='pill {st}'>{st}</span></td></tr>" for j, st in s["jobs"].items())
    rate_pct = min(100, round(100 * s["raw_24h"] / 167000)) if s["raw_24h"] else 0
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PPA Lead Engine</title>
<style>
:root{{--canvas:#f4f6f8;--panel:#fff;--panel-2:#f8fafb;--ink:#18212f;--heading:#101828;--copy:#344054;
--muted:#667085;--subtle:#98a2b3;--line:#e4e7ec;--line-2:#d0d5dd;--rail:#17202c;--rail2:#202b39;
--accent:#0a6e73;--accent-soft:#e7f5f3;--good:#147a4a;--bad:#c0362c;--blue:#175cd3}}
*{{box-sizing:border-box;margin:0}}body{{font-family:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,sans-serif;
background:var(--canvas);color:var(--copy);font-size:14px}}
.topbar{{background:var(--rail);color:#d9e2ef;display:flex;align-items:center;gap:14px;padding:14px 28px}}
.mark{{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,var(--accent),var(--blue))}}
.brand{{font-weight:650;color:#fff;letter-spacing:.4px}}.sub{{color:#98a2b3;font-size:12px}}
.pill{{margin-left:auto;background:var(--rail2);border:1px solid #2b3c4d;color:#9fd1c9;border-radius:999px;
padding:4px 12px;font-size:11px;font-family:ui-monospace,monospace;text-transform:uppercase;letter-spacing:.6px}}
.wrap{{max-width:1240px;margin:26px auto;padding:0 22px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}}
.kpi.hero{{background:linear-gradient(160deg,#101828,#17202c);border-color:#17202c}}
.kpi .label{{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:6px}}
.kpi.hero .label{{color:#98a2b3}}.kpi .val{{font-size:26px;font-weight:700;color:var(--heading)}}
.kpi.hero .val{{color:#fff;font-size:32px}}.kpi .foot{{font-size:11px;color:var(--subtle);margin-top:4px}}
.bar{{height:5px;background:#2b3c4d;border-radius:3px;margin-top:10px}}
.bar i{{display:block;height:5px;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--blue))}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
.card h3{{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);
padding:13px 16px;border-bottom:1px solid var(--line);background:var(--panel-2)}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:7px 16px;border-bottom:1px solid var(--line);text-align:left}}
tr:last-child td{{border-bottom:none}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:ui-monospace,monospace;font-size:12px}}.muted{{color:var(--subtle)}}
a{{color:var(--blue);text-decoration:none}}.pill.running{{color:var(--good);border-color:#bfe3cd;background:#eefaf2}}
.pill.STOPPED{{color:var(--bad);border-color:#f3c6c2;background:#fdf1f0}}
.pill{{margin-left:0;border:1px solid var(--line-2);background:var(--panel-2);padding:2px 9px}}
</style></head><body>
<div class=topbar><div class=mark></div><div><div class=brand>PPA LEAD ENGINE</div>
<div class=sub>local dashboard · {s['today']}</div></div><div class=pill>LAN · live</div></div>
<div class=wrap>
<div class=kpis>
<div class="kpi hero"><div class=label>24 hr rate</div><div class=val>{s['raw_24h']:,}</div>
<div class=bar><i style="width:{rate_pct}%"></i></div><div class=foot>leads, rolling 24h</div></div>
<div class=kpi><div class=label>New (24h)</div><div class=val>{s['new_24h']:,}</div><div class=foot>never delivered</div></div>
<div class=kpi><div class=label>Unsent pool</div><div class=val>{s['unsent']:,}</div><div class=foot>ready to compile</div></div>
<div class=kpi><div class=label>Fresh cycle ({s['cycle_days']}d)</div><div class=val>{s['fresh_eligible']:,}</div><div class=foot>reusable</div></div>
<div class=kpi><div class=label>Sent all time</div><div class=val>{s['sent_total']:,}</div><div class=foot>delivered</div></div>
<div class=kpi><div class=label>Disk free</div><div class=val>{s['disk_free']}</div><div class=foot>{'DELIVERY PAUSED' if s['paused'] else 'delivery active'}</div></div>
</div>
<div class=grid>
<div class=card><h3>Per hour (UTC) — RAW vs NEW</h3><table><tr><th>hour</th><th class=num>RAW</th><th class=num>NEW</th></tr>{hour_rows}</table></div>
<div class=card><h3>Unsent by state</h3><table><tr><th>state</th><th class=num>unsent</th><th class=num>new today</th></tr>{state_rows}</table></div>
<div class=card><h3>Jobs</h3><table>{job_rows}</table></div>
<div class=card><h3>Batches</h3><table><tr><th>zip</th><th class=num>size</th><th>built</th></tr>{zip_rows}</table></div>
</div></div></body></html>"""


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
