"""Classifies a section's `kind` from its heading and body preview, consulting the
tenant's vocabulary first and falling back to body-shape heuristics.

Uncertain results are surfaced to the review queue by the importer; resolution
flows update the tenant vocabulary so the same heading classifies cleanly next
time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from oms.domain.models import TenantVocabulary
from oms.domain.types import SectionKind


@dataclass
class ClassificationCandidate:
    kind: SectionKind
    confidence: float


@dataclass
class ClassificationResult:
    kind: SectionKind
    confidence: float
    candidates: list[ClassificationCandidate] = field(default_factory=list)
    uncertain: bool = False


@runtime_checkable
class SectionClassifier(Protocol):
    def classify(
        self,
        heading: str,
        body_preview: str,
        parent_skill_id: str,
        parent_kind: SectionKind | None,
        vocabulary: TenantVocabulary,
    ) -> ClassificationResult: ...
