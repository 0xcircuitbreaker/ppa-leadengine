"""Conservative identity normalization for entity matching and lead delivery.

Entity identity and delivery identity are deliberately separate:

* entity matching may combine observations only when several business signals
  agree;
* delivery matching suppresses repeated contact targets across CSV packages
  without deleting or merging either business.

Only SHA-256 keys leave this module.  The keys are deterministic so package
history can be compared without persisting another copy of contact PII.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

import phonenumbers


_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}$",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_text(value: Any) -> str:
    """Return a punctuation-insensitive, Unicode-stable comparison value."""

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return _NON_ALNUM_RE.sub("", normalized)


def normalize_email(value: Any) -> str | None:
    """Return a conservative lowercase email, or ``None`` when malformed."""

    if value is None:
        return None
    candidate = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    if len(candidate) > 254 or not _EMAIL_RE.fullmatch(candidate):
        return None
    local, domain = candidate.rsplit("@", 1)
    if len(local) > 64 or ".." in local or ".." in domain:
        return None
    return candidate


def normalize_phone_e164(value: Any, *, region: str = "US") -> str | None:
    """Return a possible NANP phone in E.164 form, or ``None`` when unusable.

    ``is_possible_number`` is intentional here.  Public-record fixtures and
    newly assigned exchanges can be structurally valid before metadata labels
    them reachable.  Line-type verification remains a later, separate stage.
    """

    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if parsed.country_code != 1 or not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _fingerprint(namespace: str, *parts: str) -> str:
    payload = "\x1f".join((namespace, *parts)).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def fingerprint_parts(namespace: str, *parts: str) -> str:
    """Public domain-separated SHA-256 helper for non-PII identity indexes."""

    return _fingerprint(namespace, *parts)


def lead_identity_fingerprint(row: Mapping[str, Any]) -> str:
    """Fingerprint the complete visible lead identity for package comparison."""

    return _fingerprint(
        "lead-identity/v1",
        normalize_text(row.get("business_name")),
        normalize_text(row.get("owner_name")),
        normalize_phone_e164(row.get("phone")) or "",
        normalize_email(row.get("email")) or "",
        normalize_text(row.get("street")),
        normalize_text(row.get("city")),
        normalize_text(row.get("state")),
        normalize_text(row.get("postal_code")),
    )


def delivery_contact_key(row: Mapping[str, Any]) -> str:
    """Return the contact target used to suppress repeat package delivery.

    Phone is the primary outreach target, then email.  With no valid contact,
    the complete lead identity is used.  A repeated contact is suppressed only
    from another package; it does not merge or remove business entities.
    """

    phone = normalize_phone_e164(row.get("phone"))
    if phone:
        return _fingerprint("delivery-phone/v1", phone)
    email = normalize_email(row.get("email"))
    if email:
        return _fingerprint("delivery-email/v1", email)
    return _fingerprint("delivery-lead/v1", lead_identity_fingerprint(row))


__all__ = [
    "delivery_contact_key",
    "fingerprint_parts",
    "lead_identity_fingerprint",
    "normalize_email",
    "normalize_phone_e164",
    "normalize_text",
]
