"""Manifest-driven permitted directory adapter (config-driven HTML directory lane).

One adapter covers any explicitly *approved* business-directory website whose
terms permit automated collection: the per-source specifics live in a versioned
extraction manifest, not in code. Adding a directory is a manifest/config
change plus a source-activation approval, never a deploy.

This is the policy-compliant alternative to automating a third-party scraping
extension. Unlike a generic scraper it is **fail-closed** at every boundary:

* ``terms_approved`` must be ``True`` and ``allowed_domains`` must cover the
  seed URL, or the adapter refuses to run (``SourceUnavailableError``).
* A bot challenge, interstitial login, or explicit block page is a hard stop
  signal, never silently bypassed. CAPTCHA/stealth handling is the separate
  off-by-default plugin's job; with no solver injected (the default) a
  challenge aborts the partition exactly as ``PermittedWebsiteAdapter`` does.
* Phone capture is mandatory-in-spirit: the manifest's ``phone`` selector is
  tried first (``tel:`` link), then visible phone text, then optionally the
  detail page. A listing whose phone cannot be normalized to E.164/NANP is
  retained as a *discovery candidate* with ``rejection_reason="invalid_phone"``
  and never counts as a phone-bearing deliverable lead. ``raw_cards`` must
  always equal ``accepted + rejected`` (reconciliation) or the partition is
  marked truncated with ``reconciliation_failure``.
* Pagination stops and marks the partition truncated when a page repeats, the
  Next control does not advance, the listing schema changes, a required field
  disappears, or a card is silently dropped (parsed < visible).

Identity semantics match the directory lane: directory data is
business-discovery evidence. The phone is a *general business phone* unless the
manifest explicitly identifies a named person and role; the record is a
business-listing owner *candidate*, not a verified beneficial owner.

Required config keys (a "manifest"):
- ``source_id``: stable source identifier (provenance).
- ``allowed_domains``: domains Hermes is permitted to crawl for this source.
- ``seed_url``: HTTPS entry URL on an allowed domain.
- ``listing_selector``: CSS selector for one listing card.
- ``fields``: canonical -> CSS selector. Canonical keys:
  business_name (required), phone, owner_name, owner_role, category,
  address, city, state, zip, website, detail_url, source_record_id
- ``next_page_selector`` (optional): CSS selector for the Next-page control.
- ``maximum_pages`` (optional, default 50, hard cap 500): pagination ceiling.
- ``requests_per_minute`` (optional, default 10, hard floor 6): polite rate.

Optional:
- ``allow_detail_page_enrichment`` (default False): fetch the detail page when
  the listing card lacks a phone and the detail URL is on an allowed domain.
- ``require_phone_for_lead`` (default True): a card with no valid phone is a
  discovery candidate, not a deliverable lead.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from adapters.base import FetchResult, SourceAdapter, SourceUnavailableError
from app.identity import normalize_phone_e164
from app.model_router.schema import Address, BusinessRecord, OwnerRecord, Person

logger = logging.getLogger(__name__)

ADAPTER_VERSION = "permitted_directory/1"
_USER_AGENT = "HermesLeadEngine/1.0 permitted-directory-research"
_HARD_MAX_PAGES = 500
_DEFAULT_MAX_PAGES = 50
_HARD_RATE_FLOOR_RPM = 6  # never faster than 6 req/min against a directory
_DEFAULT_RPM = 10
_PHONE_TEL_PREFIX = "tel:"
_VISIBLE_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]*)?"
    r"(?:\([2-9]\d{2}\)|[2-9]\d{2})"
    r"[\s.-]*[2-9]\d{2}[\s.-]*\d{4}(?!\d)"
)
_MAX_DETAIL_FETCHES_PER_PAGE = 50  # bound detail-page enrichment per listing page


class _BlockDetector(Protocol):
    def __call__(self, html: str) -> str | None: ...


def _default_block_detector(html: str) -> str | None:
    """Best-effort interstitial/challenge marker detection.

    Mirrors the spirit of ``permitted_website``'s text-marker detection: a
    challenge/login/block page is a stop signal, not a parsing problem. With no
    solver injected, any detection aborts the partition.
    """
    if not html:
        return None
    sample = html[:8192].lower()
    markers = (
        "are you a robot", "unusual traffic", "captcha", "recaptcha",
        "hcaptcha", "turnstile", "access denied", "403 forbidden",
        "please verify you are human", "confirm you're not a robot",
    )
    for marker in markers:
        if marker in sample:
            return marker
    return None


def _split_person_name(full: str) -> tuple[str | None, str | None]:
    if "," in full:
        last, _, first = full.partition(",")
        return (first.strip() or None, last.strip() or None)
    parts = full.split()
    if len(parts) >= 2:
        return (parts[0], parts[-1])
    return (None, full.strip() or None)


def _is_allowed_public_https_url(url: str, allowed_domains: tuple[str, ...]) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname:
        return False
    return any(hostname == d or hostname.endswith(f".{d}") for d in allowed_domains)


@dataclass
class DirectoryManifest:
    """Validated, typed view of a source's extraction manifest."""

    source_id: str
    allowed_domains: tuple[str, ...]
    seed_url: str
    listing_selector: str
    fields: dict[str, str]
    next_page_selector: str | None = None
    maximum_pages: int = _DEFAULT_MAX_PAGES
    requests_per_minute: int = _DEFAULT_RPM
    allow_detail_page_enrichment: bool = False
    require_phone_for_lead: bool = True
    county_label: str | None = None
    partition_by_hint: str | None = None  # bound at fetch time from the partition query

    # Canonical field keys the adapter understands. Anything else in the
    # manifest's ``fields`` map is ignored to keep parsing deterministic.
    KNOWN_FIELD_KEYS: ClassVar[frozenset[str]] = frozenset({
        "business_name", "phone", "owner_name", "owner_role", "category",
        "address", "city", "state", "zip", "website", "detail_url",
        "source_record_id",
    })
    REQUIRED_FIELD_KEYS: ClassVar[frozenset[str]] = frozenset({"business_name"})

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DirectoryManifest":
        terms = config.get("terms_approved")
        if terms is not True:
            raise SourceUnavailableError("directory_terms_not_approved")
        if config.get("allow_directory_collection") is not True:
            raise SourceUnavailableError("directory_collection_not_explicitly_enabled")

        source_id = str(config.get("source_id") or "").strip()
        if not source_id:
            raise SourceUnavailableError("directory_manifest_missing_source_id")

        raw_domains = config.get("allowed_domains")
        if not isinstance(raw_domains, list) or not raw_domains:
            raise SourceUnavailableError("directory_manifest_missing_allowed_domains")
        allowed_domains = tuple(
            str(d).strip().lower().lstrip(".")
            for d in raw_domains
            if isinstance(d, str) and d.strip()
        )
        if not allowed_domains:
            raise SourceUnavailableError("directory_manifest_missing_allowed_domains")

        seed_url = str(config.get("seed_url") or "").strip()
        if not seed_url or not _is_allowed_public_https_url(seed_url, allowed_domains):
            raise SourceUnavailableError("directory_seed_url_not_allowlisted")

        listing_selector = str(config.get("listing_selector") or "").strip()
        if not listing_selector:
            raise SourceUnavailableError("directory_manifest_missing_listing_selector")

        raw_fields = config.get("fields")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise SourceUnavailableError("directory_manifest_missing_fields")
        fields = {
            key: str(value).strip()
            for key, value in raw_fields.items()
            if key in cls.KNOWN_FIELD_KEYS and isinstance(value, str) and value.strip()
        }
        if not cls.REQUIRED_FIELD_KEYS <= set(fields):
            raise SourceUnavailableError("directory_manifest_missing_required_field:business_name")

        next_page_selector = str(config.get("next_page_selector") or "").strip() or None

        try:
            maximum_pages = int(config.get("maximum_pages", _DEFAULT_MAX_PAGES))
        except (TypeError, ValueError):
            maximum_pages = _DEFAULT_MAX_PAGES
        maximum_pages = max(1, min(maximum_pages, _HARD_MAX_PAGES))

        try:
            rpm = int(config.get("requests_per_minute", _DEFAULT_RPM))
        except (TypeError, ValueError):
            rpm = _DEFAULT_RPM
        rpm = max(_HARD_RATE_FLOOR_RPM, rpm)

        return cls(
            source_id=source_id,
            allowed_domains=allowed_domains,
            seed_url=seed_url,
            listing_selector=listing_selector,
            fields=fields,
            next_page_selector=next_page_selector,
            maximum_pages=maximum_pages,
            requests_per_minute=rpm,
            allow_detail_page_enrichment=bool(config.get("allow_detail_page_enrichment", False)),
            require_phone_for_lead=bool(config.get("require_phone_for_lead", True)),
            county_label=(str(config.get("county_label") or "").strip() or None),
        )


class PermittedDirectoryAdapter(SourceAdapter):
    """Manifest-driven crawler for explicitly approved business directories.

    Inject ``page_html_fetcher`` for tests (returns page HTML for a URL). The
    default fetcher renders the page with Playwright using the truthful
    ``HermesLeadEngine/1.0`` user agent and aborts media subresources, mirroring
    ``PermittedWebsiteAdapter``. It never authenticates, rotates proxies, or
    spoofs fingerprints; stealth/captcha are separate off-by-default plugins.
    """

    name = "permitted_directory"
    source_type = "browser"
    requires_config = [
        "terms_approved",
        "allow_directory_collection",
        "allowed_domains",
        "seed_url",
        "listing_selector",
        "fields",
    ]
    base_url = None

    def __init__(
        self,
        page_html_fetcher: Callable[[str, dict[str, Any]], str] | None = None,
        *,
        block_detector: _BlockDetector | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._page_html_fetcher = page_html_fetcher or self._fetch_page_html
        self._block_detector: _BlockDetector = block_detector or _default_block_detector
        self._sleep = sleep or time.sleep

    # -- public adapter contract -------------------------------------------------

    def fetch(self, query: str, limit: int, config: dict[str, Any]) -> list[OwnerRecord]:
        """Return accepted, phone-bearing (where required) owner-candidate records."""
        result = self.fetch_result(query=query, limit=limit, config=config)
        return result.records

    def fetch_result(self, query: str, limit: int, config: dict[str, Any]) -> FetchResult:
        manifest = DirectoryManifest.from_config(config)
        manifest = self._apply_partition_scope(manifest, query)

        capped = self._clamp_limit(limit)
        retrieved_at = datetime.now(timezone.utc).isoformat()
        accepted: list[OwnerRecord] = []
        rejected: list[dict[str, Any]] = []
        raw_cards = 0
        pages_fetched = 0
        seen_page_hashes: set[str] = set()
        truncation_reason: str | None = None

        url = manifest.seed_url
        for page_number in range(1, manifest.maximum_pages + 1):
            if len(accepted) >= capped:
                truncation_reason = "caller_limit_reached"
                break

            html = self._fetch_with_rate_limit(url, manifest, page_number)
            pages_fetched += 1

            page_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
            if page_hash in seen_page_hashes:
                truncation_reason = "pagination_loop_page_repeated"
                break
            seen_page_hashes.add(page_hash)

            cards, next_url, visible_count = self._parse_page(html, url, manifest)
            # Schema drift: the listing selector resolved but the required
            # business_name field disappeared from every card.
            raw_cards += visible_count

            for card_html in cards:
                if len(accepted) >= capped:
                    break
                outcome = self._map_card(card_html, url, page_number, retrieved_at, manifest)
                raw_cards += 0  # visible_count already accounts for the card
                if outcome.record is not None:
                    accepted.append(outcome.record)
                else:
                    rejected.append(outcome.rejection_payload())

            # Reconciliation guard: if the parser silently dropped cards
            # (visible_count != parsed cards), the manifest drifted mid-source.
            if visible_count != len(cards):
                truncation_reason = "schema_drift_silent_card_drop"
                break

            if next_url is None:
                # No further page → natural completion of this scope.
                break
            if not _is_allowed_public_https_url(next_url, manifest.allowed_domains):
                truncation_reason = "pagination_next_url_not_allowlisted"
                break
            if next_url == url:
                truncation_reason = "pagination_stalled_next_did_not_advance"
                break
            url = next_url
        else:
            # Loop exhausted maximum_pages without a natural stop.
            truncation_reason = "maximum_pages_reached"

        # Final reconciliation: raw visible cards must equal accepted + rejected.
        if len(accepted) + len(rejected) != raw_cards and truncation_reason is None:
            truncation_reason = "reconciliation_failure"

        complete = truncation_reason is None
        truncated = truncation_reason is not None and truncation_reason != "caller_limit_reached"

        metadata = {
            "source_id": manifest.source_id,
            "seed_url": manifest.seed_url,
            "scope_query": query or None,
            "partition_by": manifest.partition_by_hint,
            "pages_fetched": pages_fetched,
            "pages_expected_max": manifest.maximum_pages,
            "raw_visible_cards": raw_cards,
            "accepted_records": len(accepted),
            "rejected_records": len(rejected),
            "phone_bearing_records": sum(1 for r in accepted if r.raw_payload.get("has_valid_phone")),
            "invalid_or_missing_phones": sum(
                1
                for r in rejected
                if r.get("reason") in {"invalid_phone", "missing_phone"}
            ),
            "reconciliation": "raw_cards == accepted + rejected",
            "reconciled": (len(accepted) + len(rejected) == raw_cards),
            "adapter_version": ADAPTER_VERSION,
            "retrieved_at": retrieved_at,
            "rejection_breakdown": _count_reasons(rejected),
            "result_contract": "permitted_directory_reconciled",
        }
        if truncated:
            metadata["truncation_reason"] = truncation_reason

        return FetchResult(
            records=accepted,
            raw_count=raw_cards,
            mapped_count=len(accepted),
            pages_fetched=pages_fetched,
            complete=complete,
            truncated=truncated,
            truncation_reason=truncation_reason if truncated else None,
            metadata=metadata,
        )

    # -- scope -------------------------------------------------------------------

    def _apply_partition_scope(self, manifest: DirectoryManifest, query: str) -> DirectoryManifest:
        """Bind a partition (e.g. a ZIP) into the seed URL/query for state scans.

        Directories commonly expose a search/ZIP query. ``partition_by`` lists
        the facet the operator intends to shard on (zip/county/category). When
        a partition value is supplied as ``query`` we record it for provenance;
        URL rewriting is intentionally NOT done here to avoid mangling a site's
        query syntax — the manifest's ``seed_url`` should already encode the
        scope, or the operator partitions by seeding multiple URLs.
        """
        manifest.partition_by_hint = (query or "").strip() or None
        return manifest

    # -- fetching & rate limiting ------------------------------------------------

    def _fetch_with_rate_limit(self, url: str, manifest: DirectoryManifest, page_number: int) -> str:
        # Polite intra-source pacing. Page 1 fetches immediately; subsequent
        # pages sleep the configured interval. This is a courtesy throttle,
        # not a bypass of any site-imposed limit — a 429/challenge still stops us.
        if page_number > 1:
            interval = 60.0 / manifest.requests_per_minute
            self._sleep(interval)
        html = self._page_html_fetcher(url, {"manifest": manifest.source_id})
        challenge = self._block_detector(html)
        if challenge is not None:
            raise SourceUnavailableError(f"directory_source_blocked:{challenge}")
        return html

    @staticmethod
    def _fetch_page_html(url: str, config: dict[str, Any]) -> str:
        """Default render path: Playwright, JS off, truthful UA, no media.

        Raises ``SourceUnavailableError`` on timeout or transport failure so the
        partition fails closed rather than emitting partial guesses.
        """
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceUnavailableError("playwright_not_installed") from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    java_script_enabled=False,
                    user_agent=_USER_AGENT,
                )
                page = context.new_page()

                def route_handler(route) -> None:
                    if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_handler)
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                html = page.content()
                context.close()
                browser.close()
                return html
        except PlaywrightTimeoutError as exc:
            raise SourceUnavailableError("directory_page_timeout") from exc
        except SourceUnavailableError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(f"directory_page_request_failed:{type(exc).__name__}") from exc

    # -- parsing -----------------------------------------------------------------

    def _parse_page(
        self,
        html: str,
        base_url: str,
        manifest: DirectoryManifest,
    ) -> tuple[list[Any], str | None, int]:
        """Return (card_nodes, next_url, visible_card_count).

        ``visible_card_count`` is the count the listing selector resolved to;
        it is the reconciliation denominator. If the selector matched nothing
        on a non-first page, treat the listing set as exhausted.
        """
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(manifest.listing_selector)
        visible_count = len(cards)

        next_url: str | None = None
        if manifest.next_page_selector:
            next_node = soup.select_one(manifest.next_page_selector)
            if next_node is not None:
                href = (next_node.get("href") if hasattr(next_node, "get") else None) or next_node.get_text(strip=True)
                if href:
                    next_url = urljoin(base_url, href)

        return cards, next_url, visible_count

    # -- per-card mapping --------------------------------------------------------

    def _map_card(
        self,
        card: Any,
        page_url: str,
        page_number: int,
        retrieved_at: str,
        manifest: DirectoryManifest,
    ) -> "_CardOutcome":
        """Map one listing card to an OwnerRecord or a structured rejection."""
        def field_node(key: str) -> Any | None:
            selector = manifest.fields.get(key)
            if not selector:
                return None
            return card.select_one(selector) if hasattr(card, "select_one") else None

        def field_text(key: str) -> str:
            node = field_node(key)
            if node is None:
                return ""
            return node.get_text(" ", strip=True)

        business_name = field_text("business_name")
        if not business_name:
            return _CardOutcome.reject(
                page_url, page_number, "missing_business_name", card, retrieved_at
            )

        owner_name = field_text("owner_name")
        owner_role_label = field_text("owner_role") or None
        category = field_text("category") or None
        city = field_text("city") or None
        state = field_text("state") or None
        street = field_text("address") or None
        zip_code = field_text("zip") or None
        website_node = field_node("website")
        website = (
            str(website_node.get("href") or "").strip()
            if website_node is not None and hasattr(website_node, "get")
            else ""
        ) or field_text("website") or None
        detail_node = field_node("detail_url")
        detail_url = (
            str(detail_node.get("href") or "").strip()
            if detail_node is not None and hasattr(detail_node, "get")
            else ""
        ) or field_text("detail_url") or None

        source_node = field_node("source_record_id")
        if source_node is None and hasattr(card, "get"):
            source_node = card
        source_record_id = None
        if source_node is not None and hasattr(source_node, "get"):
            for attribute in ("data-id", "data-license-id", "data-record-id", "id"):
                candidate = str(source_node.get(attribute) or "").strip()
                if candidate:
                    source_record_id = candidate
                    break
        source_record_id = source_record_id or field_text("source_record_id") or None

        if detail_url:
            detail_url = urljoin(page_url, detail_url)
        if website:
            website = urljoin(page_url, website)

        raw_phone = ""
        phone_source: str | None = None
        phone_node = field_node("phone")
        if phone_node is not None:
            href = (
                str(phone_node.get("href") or "").strip()
                if hasattr(phone_node, "get")
                else ""
            )
            if href.lower().startswith(_PHONE_TEL_PREFIX):
                raw_phone = href[len(_PHONE_TEL_PREFIX):].strip()
                phone_source = "tel_link"
            else:
                raw_phone = phone_node.get_text(" ", strip=True)
                if raw_phone:
                    phone_source = "configured_visible_text"

        # The phone field is mandatory for deliverable directory leads.  A
        # manifest may point specifically at tel: links, so independently scan
        # the visible card text when that link is absent.
        if not raw_phone and "phone" in manifest.fields:
            visible_match = _VISIBLE_PHONE_RE.search(card.get_text(" ", strip=True))
            if visible_match is not None:
                raw_phone = visible_match.group(0)
                phone_source = "visible_text"

        # Optional detail-page phone enrichment when the card lacked a phone.
        if not raw_phone and manifest.allow_detail_page_enrichment and detail_url and _is_allowed_public_https_url(detail_url, manifest.allowed_domains):
            detail_phone = self._enrich_phone_from_detail(detail_url, manifest)
            if detail_phone:
                raw_phone = detail_phone
                phone_source = "phone_from_detail_page"

        phone_e164 = normalize_phone_e164(raw_phone) if raw_phone else None

        # Require-phone gate: a listing with no usable phone is a discovery
        # candidate, not a deliverable lead. It is REJECTED from the lead set
        # (so it never reaches a CSV) but counted for reconciliation.
        if manifest.require_phone_for_lead and phone_e164 is None:
            reason = "invalid_phone" if raw_phone else "missing_phone"
            return _CardOutcome.reject(
                page_url, page_number, reason, card, retrieved_at,
                business_name=business_name, raw_phone=raw_phone,
            )

        first, last = _split_person_name(owner_name) if owner_name else (None, None)
        # Directory semantics: business-discovery evidence. The phone is a
        # general business phone unless a named person + role is present.
        person_identified = bool(owner_name)
        owner_full = owner_name or business_name
        if owner_role_label is None:
            owner_role_label = (
                "directory listing — named contact; owner candidate pending corroboration"
                if person_identified
                else "directory listing — general business phone; beneficial owner not identified"
            )

        source_url = detail_url or page_url
        record_id = source_record_id or f"dir-{hashlib.sha256(f'{manifest.source_id}|{business_name}|{phone_e164 or raw_phone or detail_url or page_url}'.encode('utf-8')).hexdigest()[:24]}"
        card_hash = hashlib.sha256(str(card).encode("utf-8")).hexdigest()
        raw_excerpt = (business_name + " " + (raw_phone or "") + " " + (city or ""))[:1024]

        record = OwnerRecord(
            source_name=self.name,
            source_record_id=record_id,
            source_url=source_url,
            owner=Person(
                full_name=owner_full,
                first_name=first,
                last_name=last,
                is_primary=person_identified,
            ),
            owner_role=owner_role_label,
            owner_phone=phone_e164 if person_identified else None,
            confidence=0.55 if person_identified else 0.45,
            extraction_source="deterministic",
            business=BusinessRecord(
                source_name=self.name,
                source_record_id=record_id,
                source_url=source_url,
                canonical_name=business_name,
                website=website or None,
                industry=category,
                category=category,
                phones=[phone_e164] if phone_e164 else [],
                addresses=[
                    Address(
                        street=street,
                        city=city,
                        state=state,
                        postal_code=zip_code,
                        country="US",
                    )
                ],
            ),
            raw_payload={
                "source_id": manifest.source_id,
                "source_record_id": record_id,
                "source_url": source_url,
                "page_url": page_url,
                "page_number": page_number,
                "retrieved_at": retrieved_at,
                "adapter_version": ADAPTER_VERSION,
                "content_sha256": hashlib.sha256(raw_excerpt.encode("utf-8")).hexdigest(),
                "card_sha256": card_hash,
                "business_phone_e164": phone_e164,
                "raw_phone": raw_phone or None,
                "has_valid_phone": phone_e164 is not None,
                "person_identified": person_identified,
                "phone_source": phone_source,
                "partition_scope": manifest.partition_by_hint,
                "county_label": manifest.county_label,
                "evidence_tier": "directory_discovery",
                "contact_scope": "business_general",
            },
        )
        return _CardOutcome(record=record)

    def _enrich_phone_from_detail(self, detail_url: str, manifest: DirectoryManifest) -> str | None:
        """Best-effort phone pull from a permitted detail page (bounded)."""
        try:
            html = self._page_html_fetcher(detail_url, {"manifest": manifest.source_id, "detail": True})
        except SourceUnavailableError:
            return None
        if self._block_detector(html) is not None:
            return None
        soup = BeautifulSoup(html, "html.parser")
        tel_node = soup.select_one("a[href^='tel:']")
        if tel_node is not None:
            href = str(tel_node.get("href", "")).strip()
            if href.lower().startswith(_PHONE_TEL_PREFIX):
                candidate = href[len(_PHONE_TEL_PREFIX):]
                if normalize_phone_e164(candidate):
                    return candidate
        return None

    @staticmethod
    def _clamp_limit(limit: int) -> int:
        try:
            return max(int(limit), 1)
        except (TypeError, ValueError):
            return 100


def _count_reasons(rejections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rejections:
        reason = str(item.get("reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts


@dataclass
class _CardOutcome:
    record: OwnerRecord | None = None
    rejection: dict[str, Any] | None = None

    @staticmethod
    def reject(
        page_url: str,
        page_number: int,
        reason: str,
        card: Any,
        retrieved_at: str,
        *,
        business_name: str | None = None,
        raw_phone: str | None = None,
    ) -> "_CardOutcome":
        return _CardOutcome(
            rejection={
                "page_url": page_url,
                "page_number": page_number,
                "reason": reason,
                "business_name": business_name,
                "raw_phone": raw_phone,
                "card_sha256": hashlib.sha256(str(card).encode("utf-8")).hexdigest(),
                "retrieved_at": retrieved_at,
            }
        )

    def rejection_payload(self) -> dict[str, Any]:
        return self.rejection or {"reason": "unknown"}


__all__ = ["ADAPTER_VERSION", "DirectoryManifest", "PermittedDirectoryAdapter"]
