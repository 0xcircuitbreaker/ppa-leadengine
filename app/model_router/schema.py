"""Normalized owner-centric lead record schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class Address(BaseModel):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    full_address: str | None = None
    is_primary: bool = True


class Person(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    title: str | None = None
    is_primary: bool = False


class BusinessRecord(BaseModel):
    """A normalized business-only record."""

    source_name: str
    source_record_id: str | None = None
    source_url: str | None = None

    canonical_name: str
    website: str | None = None
    industry: str | None = None
    category: str | None = None

    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    addresses: list[Address] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)

    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_name")
    @classmethod
    def _canonical_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("canonical_name is required")
        return v.strip()

    @model_validator(mode="after")
    def _derive_people_full_names(self) -> BusinessRecord:
        for person in self.people:
            if not person.full_name or not person.full_name.strip():
                raise ValueError("person.full_name is required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class OwnerRecord(BaseModel):
    """A business-owner-centric lead record.

    The primary entity is the person (owner/decision-maker). The business they
    own is represented as a nested BusinessRecord. This is the canonical output
    of the owner-focused pipeline.
    """

    source_name: str
    source_record_id: str | None = None
    source_url: str | None = None

    # Owner / decision-maker fields
    owner: Person
    owner_email: str | None = None
    owner_phone: str | None = None
    owner_linkedin: str | None = None

    # Business they own/control
    business: BusinessRecord | None = None

    # Enrichment/validation metadata
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_source: str = "deterministic"  # deterministic, local, frontier
    owner_role: str | None = None  # "owner", "founder", "ceo", "principal", etc.

    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _owner_has_name(self) -> OwnerRecord:
        if not self.owner or not self.owner.full_name or not self.owner.full_name.strip():
            raise ValueError("owner.full_name is required")
        return self

    @model_validator(mode="after")
    def _business_has_name(self) -> OwnerRecord:
        if self.business is not None and not self.business.canonical_name.strip():
            raise ValueError("business.canonical_name is required when business is provided")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def to_csv_row(self) -> dict[str, Any]:
        """Flatten to a single CSV row."""
        owner = self.owner
        business = self.business
        address = (business.addresses[0] if business and business.addresses else Address())
        return {
            "owner_full_name": owner.full_name,
            "owner_first_name": owner.first_name or "",
            "owner_last_name": owner.last_name or "",
            "owner_title": owner.title or "",
            "owner_role": self.owner_role or "",
            "owner_email": self.owner_email or "",
            "owner_phone": self.owner_phone or "",
            "owner_linkedin": self.owner_linkedin or "",
            "business_name": business.canonical_name if business else "",
            "business_website": business.website if business else "",
            "business_industry": business.industry if business else "",
            "business_category": business.category if business else "",
            "business_email": (business.emails[0] if business and business.emails else ""),
            "business_phone": (business.phones[0] if business and business.phones else ""),
            "business_address": address.full_address or "",
            "business_city": address.city or "",
            "business_state": address.state or "",
            "business_postal_code": address.postal_code or "",
            "business_country": address.country or "",
            "source_name": self.source_name,
            "source_url": self.source_url or "",
            "confidence": self.confidence,
            "extraction_source": self.extraction_source,
        }

    @classmethod
    def csv_fieldnames(cls) -> list[str]:
        return [
            "owner_full_name",
            "owner_first_name",
            "owner_last_name",
            "owner_title",
            "owner_role",
            "owner_email",
            "owner_phone",
            "owner_linkedin",
            "business_name",
            "business_website",
            "business_industry",
            "business_category",
            "business_email",
            "business_phone",
            "business_address",
            "business_city",
            "business_state",
            "business_postal_code",
            "business_country",
            "source_name",
            "source_url",
            "confidence",
            "extraction_source",
        ]


def create_minimal_record(
    source_name: str,
    canonical_name: str,
    website: str | None = None,
    industry: str | None = None,
) -> BusinessRecord:
    return BusinessRecord(
        source_name=source_name,
        canonical_name=canonical_name,
        website=website,
        industry=industry,
    )
