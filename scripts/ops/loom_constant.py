#!/usr/bin/env python3
"""Constant directory lane — the Loom lane as a 24/7 loop, not a daily batch.

Cycles all directory manifests (hardcoded + auto-discovered) continuously.
Residential rotating endpoint = fresh IP per request (data-budget: only for
directories, ~50KB/page; Bing stays off residential).
Output: exports/fresh_1m/loom_<ts>.csv (harvested by the 3h mirror).
"""
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters.permitted_directory import PermittedDirectoryAdapter
from curl_cffi import requests as cf_requests

ROOT = Path(__file__).resolve().parents[2]
# ppa: proxies OFF by default (webshare account dead). Set LOOM_PROXY_URL to
# activate any rotating endpoint later (e.g. http://user:pass@host:port).
import os
_p = os.environ.get("LOOM_PROXY_URL", "")
RES = {"http": _p, "https": _p} if _p else None
DC = None

def fetch(url, config):
    for proxy in (RES, DC, None):
        try:
            r = cf_requests.get(url, impersonate="chrome124", proxies=proxy, timeout=15)
            if r.status_code == 200:
                return r.text
        except Exception:
            continue
    raise RuntimeError("fetch failed")

def make_manifest(source_id, domain, seed_url, category, state, city=""):
    return {
        "source_id": source_id,
        "allowed_domains": [domain],
        "seed_url": seed_url,
        "listing_selector": "[class*='listing'], [class*='result'], [class*='card'], div[class*='business'], article, .row, tr",
        "fields": {
            "business_name": "h3, h4, [class*='name'], [class*='title'], a, strong",
            "phone": "a[href^='tel:']",
            "city": city, "state": state, "category": category,
        },
        "next_page_selector": "a[class*='next'], a[rel='next']",
        "maximum_pages": 3,
        "requests_per_minute": 60,
        "terms_approved": True,
        "allow_directory_collection": True,
        "require_phone_for_lead": True,
    }

def load_sources():
    sources = []
    for mf in sorted((ROOT / "config/directory_manifests").glob("*.json")) + \
              sorted((ROOT / "config/directory_manifests/auto").glob("*.json")):
        try:
            sources.append(json.loads(mf.read_text()))
        except Exception:
            continue
    return sources

def main():
    adapter = PermittedDirectoryAdapter(page_html_fetcher=fetch)
    out_dir = ROOT / "exports/fresh_1m"
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    cycle = 0
    while True:
        cycle += 1
        sources = load_sources()          # reload picks up newly discovered manifests
        cycle_leads = 0
        for cfg in sources:
            try:
                result = adapter.fetch_result(query=cfg["source_id"], limit=300, config=cfg)
                for record in result.records:
                    if not record.raw_payload:
                        continue
                    phone = record.raw_payload.get("business_phone_e164", "")
                    name = record.raw_payload.get("business_name", "")
                    d = re.sub(r"\D", "", phone)
                    if not d or d in seen:
                        continue
                    seen.add(d)
                    cycle_leads += 1
                    row = {"business_name": name[:80], "phone": phone, "category": cfg["fields"].get("category", ""),
                           "city": cfg["fields"].get("city", ""), "state": cfg["fields"].get("state", ""),
                           "source": f"loom:{cfg['source_id'][:30]}", "found_at": datetime.now(timezone.utc).isoformat()[:19]}
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
                    with open(out_dir / f"loom_{ts}.csv", "a", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=list(row.keys()))
                        if f.tell() == 0:
                            w.writeheader()
                        w.writerow(row)
            except Exception:
                continue
        print(f"[{datetime.now(timezone.utc).isoformat()[:16]}] cycle {cycle}: +{cycle_leads} leads (pool {len(seen):,})", flush=True)
        time.sleep(300)   # 5-min breather between full cycles

if __name__ == "__main__":
    main()
