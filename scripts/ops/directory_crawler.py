#!/usr/bin/env python3
"""Hermes Permitted Directory Crawler — automated directory scraping.

Manifest-driven directory crawler that:
1. Reads JSON manifest defining how to scrape a directory
2. Paginates through all listing pages
3. Extracts phone numbers (tel: links, visible text, detail page fallback)
4. Validates phones (NANP NPA/NXX)
5. Deduplicates by phone + business identity
6. Outputs 5k CSV batches with reconciliation manifests

Usage:
    .venv/bin/python scripts/ops/directory_crawler.py --manifest config/directory_manifests/wv_contractors.json
    .venv/bin/python scripts/ops/directory_crawler.py --all  # crawl all manifests
"""
from __future__ import annotations
import argparse, csv, re, sys, time, json, hashlib, random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, quote_plus
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PHONE_RE = re.compile(r"(?:\+?1?[-.\s]?)?\(?([2-9]\d{2})\)?[-.\s]?([2-9]\d{2})[-.\s]?(\d{4})(?!\d)")

# Valid area codes by state
STATE_ACS = {
    "TX": {"210","214","254","281","325","346","361","409","430","432","469","512","682","713","726","737","806","817","830","832","903","915","936","940","945","956","972","979"},
    "AZ": {"480","520","602","623","928"},
    "OH": {"216","330","419","440","513","567","614","740","937","234","380"},
    "OK": {"405","539","580","918"},
    "LA": {"225","318","337","504","985"},
    "WV": {"304","681"},
    "FL": {"239","305","321","352","386","407","561","689","727","754","772","786","813","850","863","904","941","954"},
}

# Known manufacturer/brand contractor directories (public find-a-contractor pages)
KNOWN_DIRECTORIES = [
    # HVAC manufacturers
    "rightnow.trane.com", "rheem.com/dealer-locator", "americanstandardair.com",
    "carrier.com/dealer-locator", "lennox.com/locator", "goodmanmfg.com/dealer",
    "ruud.com/dealer-locator", " Bryant.com/dealer",
    # Plumbing manufacturers  
    "bradfordwhite.com", "ao-smith.com", "rinnai.us/dealer-locator",
    "kohler.com/dealer-locator", "deltafaucet.com/professional",
    # Roofing manufacturers
    "gaf.com/roofing-contractor", "owenscorning.com/roofing/find-a-contractor",
    "certainteed.com/find-a-pro", "iko.com/roofing-contractor-locator",
    # Government licensing
    "wvclboard.wv.gov", "myfloridalicense.com", "elar.sos.state.tx.us",
    "ohio.gov/elicense", "azroc.gov",
    # Professional directories
    "angi.com", "homeadvisor.com", "thumbtack.com", "houzz.com",
]


def normalize_phone(phone_str: str) -> str:
    """Normalize phone to (XXX) XXX-XXXX format. Returns '' if invalid."""
    digits = re.sub(r"\D", "", phone_str)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    if digits[0] not in "23456789":
        return ""
    if digits[:3] in ("800","888","877","866","855","844","833","900","911"):
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def phone_digits(phone_str: str) -> str:
    n = normalize_phone(phone_str)
    return re.sub(r"\D", "", n) if n else ""


class DirectoryCrawler:
    """Manifest-driven directory crawler with phone-first extraction."""
    
    def __init__(self, manifest_path: str, proxy: dict | None = None):
        self.manifest = json.loads(Path(manifest_path).read_text())
        self.proxy = proxy or {"http": "http://izivdcgb-us-gb-ae-gr-vg-vi-um-rotate:nzovqvrimjrt@p.webshare.io:80/",
                                "https": "http://izivdcgb-us-gb-ae-gr-vg-vi-um-rotate:nzovqvrimjrt@p.webshare.io:80/"}
        self.session = cf_requests.Session(impersonate="chrome124")
        self.source_id = self.manifest.get("source_id", "unknown")
        self.base_url = self.manifest.get("base_url", "")
        self.rate_limit = 1.0 / max(self.manifest.get("requests_per_minute", 10), 1) * 60
        self.stats = {
            "pages_crawled": 0,
            "listings_found": 0,
            "phones_found": 0,
            "phones_valid": 0,
            "phones_invalid": 0,
            "duplicates": 0,
            "pages_skipped": 0,
            "errors": [],
        }
        self._seen_pages = set()  # For loop detection
        self._seen_phones = set()
        self._seen_ids = set()
    
    def _fetch(self, url: str) -> str:
        """Fetch URL with rate limiting."""
        time.sleep(self.rate_limit + random.uniform(0, 0.5))
        try:
            r = self.session.get(url, proxies=self.proxy, timeout=20)
            return r.text
        except Exception as e:
            self.stats["errors"].append(f"fetch_error: {url[:60]}: {str(e)[:50]}")
            return ""
    
    def _extract_phone(self, element, html: str = "") -> str:
        """Multi-strategy phone extraction from a listing element."""
        # Strategy 1: tel: link
        tel_link = element.find("a", href=re.compile(r"^tel:"))
        if tel_link:
            phone = tel_link.get("href", "").replace("tel:", "").strip()
            normalized = normalize_phone(phone)
            if normalized:
                return normalized
        
        # Strategy 2: Visible phone text in element
        text = element.get_text()
        for match in PHONE_RE.finditer(text):
            normalized = normalize_phone(match.group())
            if normalized:
                return normalized
        
        # Strategy 3: Phone in data attributes
        for attr in ["data-phone", "data-tel", "data-phone-number"]:
            val = element.get(attr, "")
            if val:
                normalized = normalize_phone(val)
                if normalized:
                    return normalized
        
        # Strategy 4: Search broader HTML context
        if html:
            for match in PHONE_RE.finditer(html):
                normalized = normalize_phone(match.group())
                if normalized:
                    return normalized
        
        return ""
    
    def _extract_field(self, element, selector: str) -> str:
        """Extract text using CSS selector."""
        if not selector:
            return ""
        try:
            found = element.select_one(selector)
            return found.get_text(strip=True) if found else ""
        except:
            return ""
    
    def crawl(self, max_leads: int = 5000) -> list[dict]:
        """Crawl the directory according to the manifest. Returns validated leads."""
        leads = []
        listing_selector = self.manifest.get("listing_selector", "")
        fields_map = self.manifest.get("fields", {})
        next_page_selector = self.manifest.get("next_page_selector", "")
        max_pages = self.manifest.get("maximum_pages", 500)
        detail_url_selector = fields_map.get("detail_url", "")
        detail_phone_selector = self.manifest.get("detail_phone_selector", "")
        
        # Build initial URL (may have partition parameters)
        partitions = self.manifest.get("partition_by", [])
        partition_values = self.manifest.get("partition_values", [""])
        
        for partition_val in partition_values:
            if len(leads) >= max_leads:
                break
                
            url = self.base_url
            if partition_val and "{partition}" in url:
                url = url.replace("{partition}", quote_plus(str(partition_val)))
            
            page_num = 1
            consecutive_empty = 0
            
            while page_num <= max_pages and len(leads) < max_leads:
                self.stats["pages_crawled"] += 1
                
                # Fetch page
                html = self._fetch(url)
                if not html:
                    self.stats["errors"].append(f"empty_page: {url[:60]} p{page_num}")
                    break
                
                # Page hash for loop detection
                page_hash = hashlib.md5(html[:5000].encode()).hexdigest()
                if page_hash in self._seen_pages:
                    self.stats["errors"].append(f"loop_detected: page {page_num}")
                    break
                self._seen_pages.add(page_hash)
                
                soup = BeautifulSoup(html, "lxml")
                
                # Find all listing cards
                listings = soup.select(listing_selector)
                
                if not listings:
                    self.stats["errors"].append(f"no_listings: page {page_num}")
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                    break  # No more listings
                
                consecutive_empty = 0
                page_leads = 0
                
                for listing in listings:
                    self.stats["listings_found"] += 1
                    
                    # Extract business name
                    name = self._extract_field(listing, fields_map.get("business_name", ""))
                    if not name:
                        name = listing.get_text(strip=True)[:60]
                    
                    # Extract phone (PRIMARY field)
                    phone = self._extract_phone(listing)
                    
                    # If no phone on listing page, try detail page
                    if not phone and detail_url_selector and detail_phone_selector:
                        detail_url_el = listing.select_one(detail_url_selector)
                        if detail_url_el:
                            detail_href = detail_url_el.get("href", "")
                            if detail_href:
                                detail_url = urljoin(url, detail_href)
                                detail_html = self._fetch(detail_url)
                                if detail_html:
                                    detail_soup = BeautifulSoup(detail_html, "lxml")
                                    detail_el = detail_soup.select_one(detail_phone_selector)
                                    if detail_el:
                                        phone = self._extract_phone(detail_el, detail_html)
                    
                    self.stats["phones_found"] += 1 if phone else 0
                    
                    # Validate phone
                    if not phone:
                        self.stats["phones_invalid"] += 1
                        continue
                    
                    digits = phone_digits(phone)
                    if not digits:
                        self.stats["phones_invalid"] += 1
                        continue
                    
                    # Dedup
                    source_id = f"{self.source_id}_{digits}"
                    if digits in self._seen_phones or source_id in self._seen_ids:
                        self.stats["duplicates"] += 1
                        continue
                    
                    self._seen_phones.add(digits)
                    self._seen_ids.add(source_id)
                    self.stats["phones_valid"] += 1
                    
                    # Extract other fields
                    city = self._extract_field(listing, fields_map.get("city", ""))
                    state = self._extract_field(listing, fields_map.get("state", ""))
                    category = self._extract_field(listing, fields_map.get("category", ""))
                    address = self._extract_field(listing, fields_map.get("address", ""))
                    
                    # Determine state from area code if missing
                    if not state:
                        ac = digits[:3]
                        for st, acs in STATE_ACS.items():
                            if ac in acs:
                                state = st
                                break
                    
                    lead = {
                        "business_name": name[:80],
                        "phone": phone,
                        "phone_type": "directory",
                        "category": category or self.manifest.get("default_category", ""),
                        "city": city,
                        "state": state or self.manifest.get("default_state", ""),
                        "postal_code": "",
                        "source": f"directory:{self.source_id}",
                        "discovery_method": "directory_crawler",
                        "source_url": url[:100],
                        "source_page": str(page_num),
                        "source_partition": str(partition_val),
                        "is_sole_proprietor": str(not any(c in name.lower() for c in ["llc","inc","corp","company","co.","ltd"])),
                        "found_at": datetime.now(timezone.utc).isoformat()[:19],
                    }
                    leads.append(lead)
                    page_leads += 1
                    
                    if len(leads) >= max_leads:
                        break
                
                # Find next page
                if next_page_selector:
                    next_link = soup.select_one(next_page_selector)
                    if next_link and next_link.get("href"):
                        next_url = urljoin(url, next_link.get("href"))
                        if next_url == url:
                            break  # Same page — loop
                        url = next_url
                        page_num += 1
                    else:
                        break  # No more pages
                else:
                    break
            
            if partitions:
                print(f"  Partition '{partition_val}': {page_num} pages, leads so far: {len(leads)}")
        
        return leads
    
    def get_manifest_report(self, leads: list[dict]) -> dict:
        """Generate reconciliation manifest."""
        return {
            "source_id": self.source_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "adapter_version": "directory_crawler/1.0",
            "stats": self.stats,
            "leads_exported": len(leads),
            "unique_phones": len(set(phone_digits(l["phone"]) for l in leads if l.get("phone"))),
            "errors": self.stats["errors"][:20],
            "reconciliation": {
                "listings_found": self.stats["listings_found"],
                "phones_found": self.stats["phones_found"],
                "phones_valid": self.stats["phones_valid"],
                "phones_invalid_or_missing": self.stats["phones_invalid"],
                "duplicates_removed": self.stats["duplicates"],
                "pages_crawled": self.stats["pages_crawled"],
            },
        }


def create_manifest(
    source_id: str,
    name: str,
    base_url: str,
    listing_selector: str,
    fields: dict,
    next_page_selector: str = "",
    max_pages: int = 500,
    rate_per_min: int = 10,
    default_state: str = "",
    default_category: str = "",
    partition_by: list = None,
    partition_values: list = None,
) -> dict:
    """Create a directory manifest JSON."""
    return {
        "source_id": source_id,
        "name": name,
        "base_url": base_url,
        "listing_selector": listing_selector,
        "fields": fields,
        "next_page_selector": next_page_selector,
        "maximum_pages": max_pages,
        "requests_per_minute": rate_per_min,
        "default_state": default_state,
        "default_category": default_category,
        "partition_by": partition_by or [],
        "partition_values": partition_values or [""],
    }


def save_leads(leads: list[dict], source_id: str, out_dir: Path):
    """Save leads in 5k batches with manifests."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
    
    fields = ["business_name","phone","phone_type","category","city","state",
              "postal_code","source","discovery_method","source_url","source_page",
              "is_sole_proprietor","found_at"]
    
    # Sort by sole proprietor first, then state
    leads.sort(key=lambda x: (0 if x.get("is_sole_proprietor")=="True" else 1, x.get("state","")))
    
    batch = 1
    for i in range(0, len(leads), 5000):
        chunk = leads[i:i+5000]
        fn = out_dir / f"{source_id}_part-{batch:04d}_{len(chunk)}_{ts}.csv"
        
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in chunk:
                w.writerow(r)
        
        # Per-batch manifest
        manifest_fn = fn.with_suffix(".manifest.json")
        manifest_fn.write_text(json.dumps({
            "file": fn.name,
            "records": len(chunk),
            "generated": ts,
            "source_id": source_id,
        }, indent=2))
        
        batch += 1
    
    print(f"  Saved {len(leads):,} leads in {batch-1} batches (5k each)")
    print(f"  Location: {out_dir}/")


# ============================================
# PRE-BUILT MANIFESTS FOR KNOWN DIRECTORIES
# ============================================

def get_builtin_manifests() -> dict[str, dict]:
    """Pre-configured manifests for known contractor directories."""
    return {
        # GAF Roofing Contractor Directory (public, no login)
        "gaf_roofing": create_manifest(
            source_id="gaf_contractors",
            name="GAF Certified Roofing Contractors",
            base_url="https://www.gaf.com/roofing-contractors/search?zipcode={partition}&radius=100",
            listing_selector="[class*='contractor'], [class*='result'], [class*='listing'], .dealer-result",
            fields={
                "business_name": "[class*='name'], h3, h4, .company-name",
                "phone": "a[href^='tel:'], .phone, [class*='phone']",
                "city": "[class*='city'], .city",
                "category": "[class*='category']",
            },
            next_page_selector="a[class*='next'], a[rel='next']",
            max_pages=50,
            rate_per_min=8,
            default_category="roofing",
            partition_by=["zipcode"],
            partition_values=["73101","85001","43004","73013","70112"],  # OK, AZ, OH, TX, LA zip codes
        ),
        
        # Owens Corning Roofing Directory
        "owens_cornning": create_manifest(
            source_id="owens_corning_contractors",
            name="Owens Corning Preferred Roofing Contractors",
            base_url="https://www.owenscorning.com/en-us/roofing/find-a-contractor?zip={partition}",
            listing_selector="[class*='contractor'], [class*='dealer'], [class*='result']",
            fields={
                "business_name": "h3, h4, [class*='name'], [class*='title']",
                "phone": "a[href^='tel:'], [class*='phone']",
                "city": "[class*='city']",
            },
            next_page_selector="a[class*='next'], a[rel='next']",
            max_pages=30,
            rate_per_min=8,
            default_category="roofing",
            partition_by=["zipcode"],
            partition_values=["73101","85001","43004","73013","70112"],
        ),
        
        # Trane HVAC Dealer locator
        "trane_hvac": create_manifest(
            source_id="trane_dealers",
            name="Trane Comfort Specialists",
            base_url="https://rightnow.trane.com/us/en/dealer-locator?q={partition}",
            listing_selector="[class*='dealer'], [class*='result'], [class*='location']",
            fields={
                "business_name": "[class*='name'], h3, h4",
                "phone": "a[href^='tel:'], [class*='phone']",
                "city": "[class*='city'], [class*='address']",
            },
            next_page_selector="a[class*='next'], a[rel='next']",
            max_pages=20,
            rate_per_min=8,
            default_category="hvac",
            partition_by=["zipcode"],
            partition_values=["73101","85001","43004","73013","70112"],
        ),
        
        # NPIHP provider directory (healthcare)
        "npidp_healthcare": create_manifest(
            source_id="npidp_providers",
            name="NPIDP Healthcare Providers",
            base_url="https://npidb.org/doctors/{partition}/",
            listing_selector=".doctor-card, .provider-card, .listing",
            fields={
                "business_name": ".doctor-name, h3, h4, .name",
                "phone": "a[href^='tel:'], .phone",
                "city": ".city, .address",
                "category": ".specialty, .category",
            },
            next_page_selector="a[class*='next'], a[rel='next']",
            max_pages=50,
            rate_per_min=10,
            partition_by=["state"],
            partition_values=["texas","arizona","ohio","oklahoma","louisiana"],
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Hermes Directory Crawler")
    parser.add_argument("--manifest", help="Path to manifest JSON")
    parser.add_argument("--all", action="store_true", help="Crawl all built-in manifests")
    parser.add_argument("--max-leads", type=int, default=5000, help="Max leads per source")
    parser.add_argument("--out", default="exports/directory_leads")
    args = parser.parse_args()
    
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    if args.manifest:
        manifests = {Path(args.manifest).stem: args.manifest}
    elif args.all:
        # Save built-in manifests to config
        manifest_dir = Path("config/directory_manifests")
        manifest_dir.mkdir(parents=True, exist_ok=True)
        
        builtin = get_builtin_manifests()
        manifests = {}
        for name, data in builtin.items():
            fn = manifest_dir / f"{name}.json"
            fn.write_text(json.dumps(data, indent=2))
            manifests[name] = str(fn)
            print(f"  Created manifest: {fn.name}")
    else:
        print("Use --manifest <path> or --all")
        return
    
    all_leads = {}
    
    for source_name, manifest_path in manifests.items():
        print(f"\n{'='*60}")
        print(f"  Crawling: {source_name}")
        print(f"{'='*60}")
        
        try:
            crawler = DirectoryCrawler(manifest_path)
            leads = crawler.crawl(max_leads=args.max_leads)
            
            # Report
            report = crawler.get_manifest_report(leads)
            print(f"  Pages: {report['stats']['pages_crawled']}")
            print(f"  Listings found: {report['stats']['listings_found']}")
            print(f"  Phones valid: {report['stats']['phones_valid']}")
            print(f"  Phones invalid/missing: {report['stats']['phones_invalid']}")
            print(f"  Duplicates: {report['stats']['duplicates']}")
            if report['errors']:
                print(f"  Errors: {len(report['errors'])}")
                for e in report['errors'][:3]:
                    print(f"    → {e}")
            
            # Save reconciliation manifest
            report_fn = out_dir / f"{source_name}_reconciliation.json"
            report_fn.write_text(json.dumps(report, indent=2))
            
            # Merge leads
            for lead in leads:
                d = phone_digits(lead["phone"])
                if d and d not in all_leads:
                    all_leads[d] = lead
            
            # Save per-source
            if leads:
                save_leads(leads, source_name, out_dir / source_name)
            
        except Exception as e:
            print(f"  ERROR: {e}")
    
    # Save combined
    if all_leads:
        combined = list(all_leads.values())
        print(f"\n{'='*60}")
        print(f"  COMBINED OUTPUT")
        print(f"{'='*60}")
        print(f"  Total unique leads: {len(combined):,}")
        save_leads(combined, "combined", out_dir)


if __name__ == "__main__":
    main()
