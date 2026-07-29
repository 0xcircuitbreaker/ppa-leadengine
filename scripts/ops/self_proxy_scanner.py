#!/usr/bin/env python3
"""Self-proxy scanner — uses our own VPS IPs as proxy pool instead of Webshare.
Each VPS uses other VPS nodes as proxies for its requests."""
import sys, re, time, csv, random, json, warnings, threading
import os
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup

PHONE_RE = re.compile(r"(?:\+?1?[-.\s]?)?\(?([2-9]\d{2})\)?[-.\s]?([2-9]\d{2})[-.\s]?(\d{4})(?!\d)")

# SELF-PROXY NETWORK: our own VPS IPs as proxies for each other
# Each VPS uses other VPS IPs as proxies for its requests
VPS_PROXY_POOL = [
    "147.182.176.5", "157.230.210.205", "147.182.219.185", "159.223.138.235", "137.184.202.10", "143.198.160.35", "134.209.75.52",
]

STATE_ACS = {
    "TX":{"210","214","254","281","325","346","361","409","430","432","469","512","682","713","726","737","806","817","830","832","903","915","936","940","945","956","972","979"},
    "AZ":{"480","520","602","623","928"},
    "OH":{"216","330","419","440","513","567","614","740","937","234","380"},
    "OK":{"405","539","580","918"},
    "FL":{"239","305","321","352","386","407","561","689","727","754","772","786","813","850","863","904","941","954"},
    "LA":{"225","318","337","504","985"},
    "WV":{"304","681"},
    "IL":{"217","224","309","312","331","447","464","618","630","708","773","779","815","847","872"},
    "GA":{"229","404","470","478","678","706","762","770","912"},
    "NC":{"252","336","704","743","828","910","919","980","984"},
    "NJ":{"201","551","609","640","732","848","856","862","908","973"},
    "PA":{"215","223","267","272","412","445","484","570","610","717","724","814","835","878"},
}

CITIES = json.loads(Path("config/cities_expanded.json").read_text())
PROFESSIONS = json.loads(Path("config/professions_expanded.json").read_text())

SKIP = {"linkedin","facebook","youtube","instagram","twitter","wikipedia",
        "google","bing","duckduckgo","reddit","pinterest","amazon",
        "yelp","yellowpages","angi","homeadvisor","thumbtack","porch",
        "buildzoom","houzz","manta","superpages","foursquare","expertise",
        "homeguide","networx","craftjack","improvenet","bestprosintown",
        "threebestrated","consumeraffairs",
        # junk domains learned from batch analysis 2026-07-24
        "mypikpak","britannica","healthline","nordstrom","garageclothing",
        "finance.yahoo","investing.com","priva.com","radboudumc","emergency.it","bestbuy",
        "walmart","target.com","homedepot","lowes.com","costco","kroger",
        "cvs.com","walgreens","nytimes","cnn.com","bbc.","forbes.com",
        "businessinsider","webmd","mayoclinic","craigslist","indeed",
        "glassdoor","ziprecruiter","gymone","exterioo","ncsc.nl","kvk.nl"}

JUNK_CCTLD = (".be", ".nl", ".de", ".fr", ".it", ".es", ".co.uk", ".ca",
              ".au", ".nz", ".ie", ".ch", ".at", ".se", ".no", ".dk", ".pl",
              ".ru", ".cn", ".jp", ".kr", ".in", ".br", ".mx", ".za")

FIELDS = ["business_name","phone","phone_type","category","city","state",
          "source","discovery_method","website","is_sole_proprietor","found_at"]

_lock = threading.Lock()
_proxy_idx = 0
_proxy_failures = 0
FALLBACK_AFTER_FAILURES = 200   # ~3s of failures at 120w before failover
FALLBACK_DISABLED = os.environ.get("WEBSHARE_ACTIVATE", "") != "1"  # ppa: webshare OFF unless activated

# Webshare residential = automatic IP rotation per request (no initiation
# needed). Used as IMMEDIATE FALLBACK when the self-proxy network degrades,
# per operator policy 2026-07-25: never stall on a proxy outage.
WEBSHARE_FALLBACK = {
    "http": "http://izivdcgb-us-rotate:nzovqvrimjrt@p.webshare.io:80/",
    "https": "http://izivdcgb-us-rotate:nzovqvrimjrt@p.webshare.io:80/",
}

def get_proxy():
    """Next self-proxy IP. Fallback chain: fleet proxies -> DIRECT (office
    IP, when the fleet dies) -> webshare only if WEBSHARE_ACTIVATE=1.
    Auto-recovers to the fleet after cooldown."""
    global _proxy_idx
    with _lock:
        if _proxy_failures >= FALLBACK_AFTER_FAILURES:
            if not FALLBACK_DISABLED:
                return WEBSHARE_FALLBACK
            return None   # direct egress (office IP) until the fleet recovers
        ip = VPS_PROXY_POOL[_proxy_idx % len(VPS_PROXY_POOL)]
        _proxy_idx += 1
    return {"http": f"http://{ip}:8888", "https": f"http://{ip}:8888"}

def report_proxy_result(ok: bool):
    """Failover bookkeeping: sustained failures -> Webshare; success on
    self-proxy -> decay the failure counter (auto-recovery)."""
    global _proxy_failures
    with _lock:
        if ok:
            _proxy_failures = max(0, _proxy_failures - 2)
        else:
            _proxy_failures += 1


def norm(s):
    d = re.sub(r"\D","",str(s))
    if len(d) >= 10: d = d[-10:]
    if len(d) != 10 or d[0] not in "23456789": return ""
    if d[:3] in ("800","888","877","866","855","844","833"): return ""
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


def is_skip(url):
    dom = re.sub(r'^https?://(?:www\.)?([^/]+).*', r'\1', url.lower())
    if any(s in dom for s in SKIP):
        return True
    if any(dom.endswith(t) for t in JUNK_CCTLD):
        return True   # foreign ccTLD can never be a US local business
    return False


def search_bing(query):
    """Bing search — uses self-proxy network (other VPS as proxies)."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    proxy = get_proxy()
    try:
        r = cf_requests.get(url, impersonate="chrome124", proxies=proxy, timeout=8)
        if r.status_code != 200:
            report_proxy_result(False)
            return []
        report_proxy_result(True)
        soup = BeautifulSoup(r.text, "lxml")
        urls, seen = [], set()
        for li in soup.find_all("li", class_="b_algo"):
            a = li.find("a", href=True)
            if not a: continue
            text = a.get_text()
            real = re.findall(r'(https?://(?:www\.)?[a-z0-9-]+\.[a-z]{2,}[^\s]*)', text, re.I)
            if real:
                actual = real[0]
            elif a["href"].startswith("http") and "bing.com" not in a["href"]:
                actual = a["href"]
            else:
                continue
            d = re.sub(r'^https?://(?:www\.)?([^/]+).*', r'\1', actual)
            if not is_skip(actual) and d not in seen:
                seen.add(d)
                urls.append(actual)
        return urls[:4]
    except:
        report_proxy_result(False)
        return []


def scrape(url, state):
    """Scrape website for phones — uses self-proxy network."""
    vacs = STATE_ACS.get(state, set())
    proxy = get_proxy()
    try:
        r = cf_requests.get(url, impersonate="chrome124", proxies=proxy, timeout=6)
        if r.status_code != 200 or len(r.text) < 2000:
            report_proxy_result(False)
            return []
        report_proxy_result(True)
        phones = []
        for m in PHONE_RE.finditer(r.text):
            phone = norm(m.group())
            if phone:
                d = re.sub(r"\D", "", phone)
                if vacs and d[:3] not in vacs: continue
                if phone not in phones: phones.append(phone)
        return phones[:2]
    except:
        report_proxy_result(False)
        return []


def discover(term, city, state, cat):
    q = f"{term} {city} {state}"
    results = []
    for url in search_bing(q)[:3]:
        for phone in scrape(url, state):
            dom = re.sub(r'^https?://(?:www\.)?([^/]+).*', r'\1', url)
            results.append({
                "business_name": dom.replace("-"," ").replace(".com","").title()[:60],
                "phone": phone, "phone_type": "website",
                "category": cat, "city": city, "state": state,
                "source": f"bing:{dom}",
                "discovery_method": "bing_website",
                "website": url[:100],
                "is_sole_proprietor": "True",
                "found_at": datetime.now(timezone.utc).isoformat()[:19],
            })
    return results


def save(leads, out, prefix="batch"):
    ls = sorted(leads, key=lambda x: (x.get("state",""), x.get("category","")))
    b = 1
    for i in range(0, len(ls), 10000):
        chunk = ls[i:i+10000]
        fn = out / f"{prefix}_{b}_{len(chunk)}_{datetime.now(timezone.utc).strftime('%H%M%S')}.csv"
        with open(fn, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in chunk: w.writerow(r)
        b += 1


def iter_searches(states, chunk_size=250000):
    """Stream search tuples in chunks (bounded RAM — the full mega-space list
    is 15M+ tuples ≈ 2.2GB held in memory; chunked keeps it ~40MB)."""
    buf = []
    for st in states:
        for cat, terms in PROFESSIONS.items():
            for city in CITIES.get(st, []):
                for term in terms:
                    buf.append((term, city, st, cat))
                    if len(buf) >= chunk_size:
                        yield buf
                        buf = []
    if buf:
        yield buf


# ---- deterministic shard assignment (config/worker_registry.json) ----
def resolve_shard(states):
    """Find this machine's shard assignment: WORKER_SHARD/WORKER_OF env wins,
    else registry lookup by WORKER_ID env or this host's public IP.
    Returns (shard, of, tag) or (0, 1, "unsharded")."""
    import hashlib
    import json
    import os
    import urllib.request

    if os.environ.get("WORKER_SHARD") and os.environ.get("WORKER_OF"):
        return int(os.environ["WORKER_SHARD"]), int(os.environ["WORKER_OF"]), "env"
    try:
        reg = json.loads(Path("config/worker_registry.json").read_text())["workers"]
    except Exception:
        return 0, 1, "no-registry"
    wid = os.environ.get("WORKER_ID")
    if wid and wid in reg:
        w = reg[wid]
        return w["shard"], w["of"], wid
    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip()
    except Exception:
        ip = ""
    w = reg.get(ip)
    if w:
        return w["shard"], w["of"], ip
    return 0, 1, "unsharded"


def in_shard(term, city, state, cat, shard, of):
    """Deterministic shard ownership: md5(term|city|state|cat) % of == shard."""
    import hashlib
    if of <= 1:
        return True
    key = f"{term}|{city}|{state}|{cat}".encode()
    return int(hashlib.md5(key).hexdigest(), 16) % of == shard


def main():
    states = sys.argv[1].split(",")
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    target = int(sys.argv[3]) if len(sys.argv) > 3 else 1000000
    out = Path("exports/fresh_1m")
    out.mkdir(parents=True, exist_ok=True)
    shard, of, tag = resolve_shard(states)
    print(f"SELF-PROXY SCANNER — Uses our own VPS IPs as proxy network")
    print(f"States: {states} | Workers: {workers} | chunked list (bounded RAM)")
    print(f"Self-proxy pool: {len(VPS_PROXY_POOL)} unique VPS IPs")
    print(f"SHARD: {shard}/{of} ({tag}) — only scanning 1/{of} of the space, zero overlap with other nodes")
    print("=" * 60)
    seen = set()
    leads = []
    start = time.time()
    bs = workers * 2
    done_searches = 0
    for chunk in iter_searches(states):
        if of > 1:
            chunk = [s for s in chunk if in_shard(s[0], s[1], s[2], s[3], shard, of)]
            if not chunk:
                continue
        random.shuffle(chunk)
        for offset in range(0, len(chunk), bs):
            if len(leads) >= target: break
            batch = chunk[offset:offset+bs]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(discover, t, c, s, cat): (t,c,s,cat) for t,c,s,cat in batch}
            for f in as_completed(futures):
                try:
                    for lead in f.result():
                        d = re.sub(r"\D", "", lead["phone"])
                        if d and d not in seen:
                            seen.add(d)
                            leads.append(lead)
                except: pass
            done_searches += len(batch)
            el = time.time() - start
            if int(el) % 15 == 0 or len(leads) % 1000 < bs:
                rate = len(leads) / max(el, 1) * 60
                eta = (target - len(leads)) / max(rate, 1) / 60
                fb = (" | FALLBACK:webshare" if not FALLBACK_DISABLED else " | FALLBACK:direct") if _proxy_failures >= FALLBACK_AFTER_FAILURES else ""
                print(f"  {len(leads):>7,}/{target:,} | {rate:.0f}/min | ETA:{eta:.0f}h | {done_searches:,} searches{fb}")
            if len(leads) > 0 and len(leads) % 5000 < bs:
                save(leads, out, "checkpoint")
            time.sleep(0.05)
    save(leads, out, "final")
    print(f"DONE: {len(leads):,}")


if __name__ == "__main__":
    main()
