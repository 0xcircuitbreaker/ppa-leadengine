"""ABC and shared utilities for lead source adapters.

An adapter converts raw source data into a normalized OwnerRecord.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.model_router.schema import OwnerRecord


class SourceUnavailableError(RuntimeError):
    """A source is reachable but unavailable for legitimate automated collection."""


@dataclass
class FetchResult:
    """Auditable adapter outcome, distinct from its mapped record list."""

    records: list[OwnerRecord] = field(default_factory=list)
    raw_count: int = 0
    mapped_count: int = 0
    pages_fetched: int = 0
    complete: bool = False
    truncated: bool = False
    truncation_reason: str | None = None
    next_cursor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mapped_count == 0 and self.records:
            self.mapped_count = len(self.records)
        if self.mapped_count != len(self.records):
            raise ValueError("fetch_result_mapped_count_mismatch")
        if self.raw_count < self.mapped_count:
            raise ValueError("fetch_result_raw_count_below_mapped_count")
        if self.complete and self.truncated:
            raise ValueError("fetch_result_cannot_be_complete_and_truncated")
        if self.truncated and not self.truncation_reason:
            raise ValueError("fetch_result_truncation_reason_required")


def synthetic_data_enabled(config: dict[str, Any]) -> bool:
    """Allow fixtures only when a caller explicitly labels a controlled test."""
    return config.get("allow_synthetic_data") is True


class SourceAdapter(ABC):
    """Base class for all owner-focused lead source adapters."""

    name: str = "abstract"
    source_type: str = "web"
    requires_config: list[str] = []

    @abstractmethod
    def fetch(self, query: str, limit: int, config: dict[str, Any]) -> list[OwnerRecord]:
        """Fetch raw owner records from the source and return normalized records."""
        ...

    def fetch_result(self, query: str, limit: int, config: dict[str, Any]) -> FetchResult:
        """Compatibility wrapper for adapters not yet exposing raw-page detail."""

        records = self.fetch(query=query, limit=limit, config=config)
        requested = max(int(limit), 1)
        reached_limit = len(records) >= requested
        return FetchResult(
            records=records,
            raw_count=len(records),
            mapped_count=len(records),
            pages_fetched=1,
            complete=not reached_limit,
            truncated=reached_limit,
            truncation_reason="caller_limit_reached" if reached_limit else None,
            metadata={"result_contract": "compatibility_wrapper"},
        )

    def validate_config(self, config: dict[str, Any]) -> None:
        missing = [key for key in self.requires_config if not config.get(key)]
        if missing:
            raise ValueError(f"Adapter {self.name} missing config: {', '.join(missing)}")


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> SourceAdapter | None:
        return self._adapters.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._adapters.keys())


__all__ = ["FetchResult", "Registry", "SourceAdapter", "SourceUnavailableError", "synthetic_data_enabled"]
