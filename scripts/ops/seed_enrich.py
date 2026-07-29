#!/usr/bin/env python3
"""ppa seed enricher — registry seed (name+city+state) -> Bing -> phone.

Design (matches parent system): per-seed Bing query, result-page extraction,
NAME-TOKEN ATTRIBUTION GUARD (page must contain >=60% of name tokens before
a phone is attributed to the business), domain shortcut (official-site match
gets fetched directly), state area-code validation, junk-domain filter.
Output -> exports/fresh_1m/enrich_<ts>.csv (harvest -> compile chain).

Sharding: SEED_SHARD/SEED_OF env — md5(name|city|state) % OF == SHARD,
zero overlap across machines, restart-safe via exports/seeds/.enrich_state.json.
"""

from __future__ import annotations

import csv, hashlib, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from curl_cffi import requests as cf_requests

ROOT = Path(__file__).resolve().parents[2]
SEEDS = ROOT / "exports" / "seeds" / "seed_pool.csv"
STATE_FILE = ROOT / "exports" / "seeds" / ".enrich_state.json"
OUT = ROOT / "exports" / "fresh_1m"

PROXY_POOL = [
    "147.182.176.5", "157.230.210.205", "147.182.219.185", "159.223.138.235",
    "137.184.202.10", "143.198.160.35", "134.209.75.52",
]

STATE_ACS = {
    "TX": {"210","214","254","281","325","346","361","409","430","432","469","512","682","713","726","737","806","817","830","832","903","915","936","940","945","956","972","979"},
    "AZ": {"480","520","602","623","928"},
    "OH": {"216","330","419","440","513","567","614","740","937","234","380"},
    "OK": {"405","539","580","918"},
    "FL": {"239","305","321","352","386","407","561","689","727","754","772","786","813","850","863","904","941","954"},
    "LA": {"225","318","337","504","985"},
    "WV": {"304","681"},
    "NC": {"252","336","704","743","828","910","919","980","984"},
}

SKIP = {"linkedin","facebook","youtube","instagram","twitter","wikipedia","google","bing",
        "duckduckgo","reddit","pinterest","amazon","yelp","yellowpages","angi","homeadvisor",
        "thumbtack","porch","buildzoom","houzz","manta","superpages","foursquare","expertise",
        "bbb.org","mapquest","whitepages","spokeo","zoominfo","dnb.com","indeed","glassdoor"}
JUNK_CCTLD = (".be",".nl",".de",".fr",".it",".es",".co.uk",".ca",".au",".nz",".ie",".ch",
              ".at",".se",".no",".dk",".pl",".ru",".cn",".jp",".kr",".in",".br",".mx",".za")

PHONE_RE = re.compile(r"\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})")
STOP = {"the","a","an","of","and","llc","inc","co","company","corp","corporation",
        "services","service","group","llp","pllc","pa","pc","dba","&"}

_lock = threading.Lock()
_idx = 0
_found = 0
_done = 0
_fails = 0
DIRECT_AFTER = 100   # sustained proxy failures before going direct (office IP)


def proxy():
    """Fleet proxies round-robin; on sustained failure go DIRECT (office IP)
    until the fleet recovers. Webshare only if WEBSHARE_ACTIVATE=1."""
    global _idx
    with _lock:
        if _fails >= DIRECT_AFTER and os.environ.get("WEBSHARE_ACTIVATE", "") != "1":
            return None   # direct egress
        ip = PROXY_POOL[_idx % len(PROXY_POOL)]
        _idx += 1
    return {"http": f"http://{ip}:8888", "https": f"http://{ip}:8888"}


def tokens(name):
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in STOP and len(t) > 1]


def attr_ok(name, page_text):
    tk = tokens(name)
    if not tk:
        return False
    low = page_text.lower()
    hits = sum(1 for t in tk if t in low)
    return hits / len(tk) >= 0.6


def extract_phones(text, st):
    out = []
    acs = STATE_ACS.get(st, set())
    for m in PHONE_RE.finditer(text):
        d = "".join(m.groups())
        if acs and d[:3] not in acs:
            continue
        if d[:3] == d[3:6] or d.endswith(("0000", "1111")):
            continue
        out.append(d)
    return out


def enrich(seed):
    name, cat, city, st = seed["business_name"], seed["category"], seed["city"], seed["state"]
    q = f'"{name}" {city} {st}'
    global _fails
    try:
        r = cf_requests.get(f"https://www.bing.com/search?q={quote_plus(q)}&count=10",
                            impersonate="chrome124", proxies=proxy(), timeout=20)
        html = r.text
        _fails = max(0, _fails - 2)
    except Exception:
        _fails += 1
        return None
    urls = []
    for m in re.finditer(r'<a href="(https?://([^"/]+))', html):
        url, dom = m.group(1), m.group(2).lower()
        if any(s in dom for s in SKIP) or dom.endswith(JUNK_CCTLD):
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) >= 3:
            break
    for url in urls:
        try:
            site = cf_requests.get(url, impersonate="chrome124", proxies=proxy(), timeout=15).text
        except Exception:
            continue
        if not attr_ok(name, site):
            continue
        phones = extract_phones(re.sub(r"<[^>]+>", " ", site), st)
        if phones:
            dom = url.split("/")[2]
            return {"business_name": name[:80], "phone": phones[0], "phone_type": "website",
                    "category": cat or "business", "city": city, "state": st,
                    "source": "seed_enrich", "discovery_method": "registry_enrichment",
                    "website": url, "found_at": datetime.now(timezone.utc).isoformat()[:19]}
    phones = extract_phones(html, st)
    if phones and attr_ok(name, html):
        return {"business_name": name[:80], "phone": phones[0], "phone_type": "directory",
                "category": cat or "business", "city": city, "state": st,
                "source": "seed_enrich", "discovery_method": "registry_enrichment",
                "website": "", "found_at": datetime.now(timezone.utc).isoformat()[:19]}
    return None


def shard_of(seed):
    h = hashlib.md5(f"{seed['business_name']}|{seed['city']}|{seed['state']}".encode()).hexdigest()
    return int(h, 16)


def main():
    global _found, _done
    threads = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    shard = int(os.environ.get("SEED_SHARD", "0"))
    of = int(os.environ.get("SEED_OF", "1"))
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9

    pause_flag = ROOT / "exports" / "seeds" / ".enrich_paused"
    while pause_flag.exists():
        print("enrichment paused (saved for delivery) - sleeping", flush=True)
        time.sleep(300)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"offset": 0}
    seeds = []
    with open(SEEDS) as f:
        for row in csv.DictReader(f):
            seeds.append(row)
    mine = [s for s in seeds if shard_of(s) % of == shard]
    start = state.get("offset", 0)
    mine = mine[start:start + limit]
    print(f"SEED_ENRICH shard {shard}/{of} | pool {len(seeds):,} | mine {len(mine):,} (from offset {start:,}) | threads {threads}", flush=True)
    if not mine:
        print("DONE: no seeds left in this shard", flush=True)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_f = open(OUT / f"enrich_{ts}.csv", "a", newline="")
    fields = ["business_name","phone","phone_type","category","city","state","source","discovery_method","website","found_at"]
    w = csv.DictWriter(out_f, fieldnames=fields)
    if out_f.tell() == 0:
        w.writeheader()

    t0 = time.time()
    batch_done = 0
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = {ex.submit(enrich, s): s for s in mine}
        for fut in as_completed(futures):
            _done += 1
            batch_done += 1
            try:
                rec = fut.result()
            except Exception:
                rec = None
            if rec:
                _found += 1
                w.writerow(rec)
                if _found % 25 == 0:
                    out_f.flush()
            if batch_done >= 500:
                state["offset"] = start + _done
                STATE_FILE.write_text(json.dumps(state))
                batch_done = 0
            if _done % 500 == 0:
                rate = _done / max(time.time() - t0, 1) * 60
                hit = 100 * _found / max(_done, 1)
                print(f"  {_done:,}/{len(mine):,} | {rate:.0f} seeds/min | found {_found:,} ({hit:.1f}%)", flush=True)
    state["offset"] = start + _done
    STATE_FILE.write_text(json.dumps(state))
    out_f.close()
    print(f"DONE: {_done:,} seeds, {_found:,} phones found ({100*_found/max(_done,1):.1f}%)", flush=True)


if __name__ == "__main__":
    main()
