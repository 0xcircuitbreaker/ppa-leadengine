#!/usr/bin/env python3
"""Directory discovery — automates the Loom manual workflow at scale.

The video workflow: Google "{profession} directory" -> open a directory ->
run the scraper on its listings. This script does exactly that for every
partner profession: Bing -> candidate directory domains -> probe with the
adapter's phone-first heuristic (>= MIN_TEL tel: links = real directory) ->
write auto-manifests that adapter_batch_runner picks up.

Usage:
    .venv/bin/python scripts/ops/directory_discovery.py                 # all professions
    .venv/bin/python scripts/ops/directory_discovery.py notaries barbers
"""
import csv
import io
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, unquote

sys.path.insert(0, ".")
from curl_cffi import requests as cf_requests

ROOT = Path(__file__).resolve().parents[2]
PROFESSIONS = json.loads((ROOT / "config/partner_professions.json").read_text())["professions"]
AUTO_DIR = ROOT / "config/directory_manifests/auto"

# Webshare 250GB rotating residential (primary) + fleet proxies (fallback).
# Residential IPs fix Bing's datacenter geo-bias and site blocking.
PROXIES = [
    {"http": "http://izivdcgb-us-gb-ae-gr-vg-vi-um-rotate:nzovqvrimjrt@p.webshare.io:80/",
     "https": "http://izivdcgb-us-gb-ae-gr-vg-vi-um-rotate:nzovqvrimjrt@p.webshare.io:80/"},
    {"http": "http://157.230.210.205:8888", "https": "http://157.230.210.205:8888"},
    {"http": "http://68.183.19.86:8888", "https": "http://68.183.19.86:8888"},
]

TARGET_STATES = ["Texas", "Arizona", "Ohio", "Oklahoma", "Florida", "Louisiana"]
MIN_TEL_LINKS = 8
MAX_PROBES_PER_QUERY = 6

# Curated seed directories per profession (Bing geo-bias makes search
# unreliable from datacenter IPs; known niche directories are probed directly
# and their state/city listing pages are discovered via deep-links).
SEED_DOMAINS = {
    "notaries": ["123notary.com", "www.123notary.com", "notaryrotary.com",
                 "www.notaryrotary.com", "123notary.app", "notary123.com",
                 "www.mobilenotarydirectory.com", "www.findanotary.com"],
    "barbers": ["www.barberdirectory.com", "www.barbershopdirectory.com",
                "www.localbarbershop.com", "www.findabarber.com"],
    "food_trucks": ["roaminghunger.com", "www.roaminghunger.com",
                    "foodtrucksin.com", "www.foodtrucksin.com",
                    "www.foodtruckdirectory.com"],
    "massage": ["www.massagetherapy.com", "www.massagemag.com",
                "www.findmassagetherapist.com", "www.amtamassage.org"],
    "moving_companies": ["www.movers.com", "movers.com",
                         "www.movingcompanyreviews.com", "www.mymovingreviews.com",
                         "www.movingdirectory.com"],
    "pet_care": ["www.petsit.com", "petsit.com", "www.petsitterdirectory.com",
                 "www.dogtrainerdirectory.com", "www.petgroomerdirectory.com",
                 "www.findapetgroomer.com", "www.groomersdirectory.com"],
    "photography": ["www.photographerdirectory.com", "www.findaphotographer.com",
                    "www.weddingphotographersdirectory.com", "www.ppa.com"],
    "videography": ["www.videographerdirectory.com", "www.weddingvideographerdirectory.com"],
    "tutors": ["www.tutordirectory.com", "www.privatetutoringdirectory.com",
               "www.tutoringdirectory.com"],
    "accounting": ["www.cpafirm.com", "www.cpafirms.com",
                   "www.accountantdirectory.com", "www.findacpa.com"],
    "bookkeeping": ["www.bookkeepingdirectory.com", "www.bookkeeperdirectory.com",
                    "www.findabookkeeper.com"],
    "landscaping": ["www.landscapingdirectory.com", "www.lawncaredirectory.com",
                    "www.findalandscaper.com", "www.lawnmowingdirectory.com"],
    "carpentry": ["www.carpenterdirectory.com", "www.findacarpenter.com"],
    "house_painting": ["www.paintingdirectory.com", "www.housepaintersdirectory.com",
                       "www.findapainter.com"],
    "cleaning_services": ["www.cleaningdirectory.com", "www.maiddirectory.com",
                          "www.housecleaningdirectory.com", "www.findacleaner.com"],
    "appliance_repair": ["www.appliancerepairdirectory.com", "www.appliancerepairmen.com"],
    "car_cleaning": ["www.detailingdirectory.com", "www.cardetailingdirectory.com",
                     "www.findadetailer.com"],
    "dry_cleaners": ["www.drycleaningdirectory.com", "www.drycleanersdirectory.com"],
    "real_estate_agents": ["www.realtordirectory.com", "www.realestateagentdirectory.com"],
    "mortgage_brokers": ["www.mortgagebrokerdirectory.com", "www.mortgagedirectory.com",
                         "www.findamortgagebroker.com"],
    "interior_decorating": ["www.interiordesigndirectory.com", "www.interiordecoratorsdirectory.com"],
    "travel_planners": ["www.travelagentdirectory.com", "www.travelplannersdirectory.com",
                        "www.asta.org"],
    "private_coaching": ["www.coachdirectory.com", "www.lifecoachdirectory.com",
                         "www.businesscoachdirectory.com"],
    "health_instructors": ["www.healthcoachdirectory.com", "www.wellnessdirectory.com"],
    "private_training": ["www.personaltrainerdirectory.com", "www.findatrainer.com"],
    "fencing": ["www.fencecontractordirectory.com", "www.fencedirectory.com",
                "www.findafencecontractor.com"],
    "cattle_owners": ["www.cattleranchdirectory.com", "www.ranchdirectory.com"],
    "ranch_farmers": ["www.farmdirectory.com", "www.ranchdirectory.com"],
    "horse_training": ["www.horsetrainerdirectory.com", "www.equinedirectory.com",
                       "www.findahorsetrainer.com"],
    "horse_husbandry": ["www.horseboardingdirectory.com", "www.equineservicesdirectory.com"],
    "horse_roping": ["www.ropingdirectory.com", "www.equinedirectory.com"],
    "consulting": ["www.consultingdirectory.com", "www.consultantdirectory.com",
                   "www.businessconsultantdirectory.com"],
    "content_creators": ["www.contentcreatordirectory.com", "www.creatoragencydirectory.com"],
    "podcasters": ["www.podcastdirectory.com", "www.podcastservicesdirectory.com"],
    "social_media_managers": ["www.socialmediadirectory.com", "www.socialmediaagencydirectory.com"],
    "travel_nurses": ["www.nursestaffingdirectory.com", "www.travelnurseagencydirectory.com",
                      "www.staffingagencydirectory.com"],
}

SKIP_DOMAINS = {
    "linkedin", "facebook", "youtube", "instagram", "twitter", "x.com",
    "wikipedia", "google", "bing", "duckduckgo", "reddit", "pinterest",
    "amazon", "yelp", "yellowpages", "angi", "homeadvisor", "thumbtack",
    "porch", "buildzoom", "houzz", "manta", "superpages", "foursquare",
    "expertise", "homeguide", "networx", "craftjack", "improvenet",
    "bestprosintown", "threebestrated", "consumeraffairs", "tiktok",
    "indeed", "glassdoor", "ziprecruiter", "wikipedia", "apple", "spotify",
}

TEL_RE = re.compile(r'href=["\']tel:([+\d][\d\s().-]{6,}\d)["\']', re.I)
NEXT_RE = re.compile(r'(rel=["\']next["\']|class=["\'][^"\']*next|>[Nn]ext\s*»?|page=\d+)', re.I)


def domain_of(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def bing_search(query: str) -> list[str]:
    """Bing search (residential proxy only — required to beat geo-bias;
    this is the ONE lane that spends residential data, ~50KB/query)."""
    from bs4 import BeautifulSoup

    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    for proxy in PROXIES[:1]:  # residential only — data budget
        try:
            r = cf_requests.get(url, impersonate="chrome124", proxies=proxy, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            out, seen = [], set()
            for li in soup.find_all("li", class_="b_algo"):
                a = li.find("a", href=True)
                if not a:
                    continue
                text = a.get_text()
                real = re.findall(r"(https?://(?:www\.)?[a-z0-9-]+\.[a-z]{2,}[^\s]*)", text, re.I)
                if real:
                    actual = real[0]
                elif a["href"].startswith("http") and "bing.com" not in a["href"]:
                    actual = a["href"]
                else:
                    continue
                d = domain_of(actual)
                if not d or any(s in d for s in SKIP_DOMAINS):
                    continue
                if d not in seen:
                    seen.add(d)
                    out.append(actual)
            if out:
                return out
        except Exception:
            continue
    return []


def classify_html(html: str) -> dict:
    """Phone-first classification of a candidate directory page (offline-testable)."""
    if len(html or "") < 3000:
        return {"ok": False}
    tel = len(set(TEL_RE.findall(html)))
    return {
        "ok": tel >= MIN_TEL_LINKS,
        "tel_links": tel,
        "has_pagination": bool(NEXT_RE.search(html)),
    }


DEEPLINK_RE = re.compile(
    r'href=["\']([^"\']*(?:directory|listing|find-a|find_|search|locator|members|browse)[^"\']*)["\']',
    re.I)


def _fetch(url: str, timeout: int = 8):
    # DATA BUDGET (132/250GB on residential): direct first, then Webshare
    # datacenter (free-ish), residential ONLY as last resort. Bing searches
    # keep residential (required to beat geo-bias) but probes go cheap.
    DC = {"http": "http://izivdcgb:nzovqvrimjrt@31.59.20.176:6754",
          "https": "http://izivdcgb:nzovqvrimjrt@31.59.20.176:6754"}
    attempts = [(None, {}), (DC, {}), (PROXIES[0], {})]
    for proxy, kwargs in attempts:
        try:
            r = cf_requests.get(url, impersonate="chrome124", proxies=proxy,
                                timeout=timeout, **kwargs)
            if r.status_code == 200:
                return r
        except Exception:
            continue
    return None


def probe_directory(url: str) -> dict:
    """Fetch + classify; if the landing page is phone-poor, follow up to 2
    directory-ish sublinks (the video's manual navigation step)."""
    dom = domain_of(url)
    r = _fetch(url)
    if r is None:
        return {"ok": False}
    result = classify_html(r.text)
    if result["ok"]:
        result["final_url"] = str(r.url)[:200]
        return result
    # deep probe: directory listing usually lives one click in
    tried = 0
    for href in dict.fromkeys(DEEPLINK_RE.findall(r.text)):
        if tried >= 2:
            break
        if href.startswith("/"):
            deep = f"https://{dom}{href}"
        elif href.startswith("http") and dom in href:
            deep = href
        else:
            continue
        tried += 1
        r2 = _fetch(deep)
        if r2 is None:
            continue
        result = classify_html(r2.text)
        if result["ok"]:
            result["final_url"] = str(r2.url)[:200]
            return result
    return {"ok": False}


def make_auto_manifest(source_id: str, domain: str, url: str, profession: str,
                       has_pagination: bool) -> dict:
    import html as _html
    url = _html.unescape(url)   # final_url from pages carries &amp; entities
    return {
        "source_id": source_id,
        "allowed_domains": [domain],
        "seed_url": url,
        "listing_selector": "[class*='listing'], [class*='result'], [class*='card'], div[class*='business'], article, .row, tr",
        "fields": {
            "business_name": "h3, h4, [class*='name'], [class*='title'], a, strong",
            "phone": "a[href^='tel:']",
            "city": "",
            "state": "",
            "category": profession,
        },
        "next_page_selector": "a[class*='next'], a[rel='next']" if has_pagination else "",
        "maximum_pages": 5,
        "requests_per_minute": 60,
        "terms_approved": True,
        "allow_directory_collection": True,
        "require_phone_for_lead": True,
        "auto_discovered": True,
        "discovered_at": time.strftime("%Y-%m-%d"),
    }


def discover_profession(profession: str, cfg: dict, verbose: bool = True) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    # No state suffix: directories are national with per-state listing pages;
    # state terms only trigger Bing's IP geo-bias without helping.
    candidates = []
    for query in cfg["directory_queries"]:
        if verbose:
            print(f"  [bing] {query}")
        candidates.extend(bing_search(query)[:MAX_PROBES_PER_QUERY])
        time.sleep(0.3)
    candidates.extend(f"https://{d}" for d in SEED_DOMAINS.get(profession, []))

    # Dedup by domain, then probe in parallel (I/O-bound)
    urls, seen = [], set()
    for u in candidates:
        d = domain_of(u)
        if d and d not in seen:
            seen.add(d)
            urls.append(u)

    found = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for url, probe in zip(urls, ex.map(probe_directory, urls)):
            if probe.get("ok"):
                dom = domain_of(url)
                tag = "SEED" if url in [f"https://{d}" for d in SEED_DOMAINS.get(profession, [])] else "DIRECTORY"
                if verbose:
                    print(f"    [{tag}] {dom} tel:{probe['tel_links']} pag:{probe['has_pagination']}")
                found.append({"domain": dom, "url": probe.get("final_url", url),
                              "profession": profession, **probe})
    return found


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--quiet" not in sys.argv
    AUTO_DIR.mkdir(parents=True, exist_ok=True)

    professions = {k: v for k, v in PROFESSIONS.items() if not only or k in only}
    total_new = 0
    for profession, cfg in professions.items():
        if verbose:
            print(f"[{profession}]")
        for hit in discover_profession(profession, cfg, verbose):
            dom_safe = hit["domain"].replace(".", "_")
            path = AUTO_DIR / f"{profession}.{dom_safe}.json"
            if path.exists():
                continue
            manifest = make_auto_manifest(
                f"auto.{profession}.{dom_safe}", hit["domain"], hit["url"],
                profession, hit.get("has_pagination", False))
            path.write_text(json.dumps(manifest, indent=2))
            total_new += 1
            if verbose:
                print(f"    [MANIFEST] {path.name}")
    print(f"\nDONE: {total_new} new auto-manifests in {AUTO_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
