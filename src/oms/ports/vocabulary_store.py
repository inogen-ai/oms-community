"""Per-tenant vocabulary storage port.

A `TenantVocabulary` node maps the organisation's words (heading text, marker
labels, filenames, directory layouts, frontmatter fields) to the universal
concepts the system reasons against.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from oms.domain.models import TenantVocabulary


@runtime_checkable
class VocabularyStore(Protocol):
    def get_or_seed_default(self, tenant_id: str) -> TenantVocabulary:
        """Return the tenant's vocabulary, seeding from defaults if absent."""

    def get(self, tenant_id: str) -> TenantVocabulary | None:
        """Return the tenant's vocabulary or None if it has not been seeded."""

    def add_pattern(self, tenant_id: str, *, field: str, kind: str, pattern: str) -> None:
        """Append `pattern` to `body[field][kind]` (idempotent); bump version."""

    def replace(self, tenant_id: str, body: dict) -> None:
        """Replace the vocabulary body wholesale; bump version."""
