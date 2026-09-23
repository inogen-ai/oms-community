"""Default SectionClassifier: vocabulary -> body-shape heuristics -> uncertain."""
from __future__ import annotations

import re

from oms.domain.models import TenantVocabulary
from oms.domain.types import SectionKind
from oms.ports.section_classifier import (
    ClassificationCandidate, ClassificationResult,
)


# Body-shape patterns used by the heuristic ladder.
_BULLET           = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)
_NUMBERED         = re.compile(r"^\s*\d+\.\s+\S", re.MULTILINE)
_TABLE_ROW        = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_CODE_FENCE       = re.compile(r"^\s*```", re.MULTILINE)
_GOOD_BAD_MARKER  = re.compile(
    r"\*\*\s*(good|bad|before|after|strong|weak)\s*:?\s*\*\*", re.IGNORECASE)
_SKILL_LINK       = re.compile(r"\*\*[a-z][a-z0-9-]+\*\*\s*[—:\-]")
_IMPERATIVE_LEAD  = re.compile(
    r"^\s*[-*]\s+(Always|Never|Don[''’]?t|Do not|Avoid|Use|Prefer|Lead|Ensure|"
    r"Write|Keep|Make|Provide|Include)\b",
    re.MULTILINE,
)


class DefaultSectionClassifier:
    """Vocabulary match (longest-pattern wins) -> body heuristics -> uncertain (prose)."""

    def classify(
        self,
        heading: str,
        body_preview: str,
        parent_skill_id: str,
        parent_kind: SectionKind | None,
        vocabulary: TenantVocabulary,
    ) -> ClassificationResult:
        # 1. Vocabulary match.
        vocab_kind = self._vocab_match(heading, vocabulary)
        if vocab_kind is not None:
            return ClassificationResult(
                kind=vocab_kind, confidence=0.95,
                candidates=[ClassificationCandidate(vocab_kind, 0.95)],
                uncertain=False,
            )

        # 2. Body-shape heuristics.
        scores: list[tuple[SectionKind, float]] = []
        if _SKILL_LINK.search(body_preview):
            scores.append((SectionKind.ROUTING, 0.7))
        if _CODE_FENCE.search(body_preview) and _GOOD_BAD_MARKER.search(body_preview):
            scores.append((SectionKind.EXAMPLES, 0.75))
        if _TABLE_ROW.search(body_preview) and not _IMPERATIVE_LEAD.search(body_preview):
            scores.append((SectionKind.TEMPLATE, 0.7))
        if _IMPERATIVE_LEAD.search(body_preview):
            scores.append((SectionKind.RULES, 0.7))
        if _NUMBERED.search(body_preview) and "?" in body_preview:
            scores.append((SectionKind.CHECKLIST, 0.7))

        threshold = vocabulary.body.get("classifier_uncertainty_threshold", 0.55)

        if scores:
            scores.sort(key=lambda s: s[1], reverse=True)
            top_kind, top_conf = scores[0]
            uncertain = top_conf < threshold
            return ClassificationResult(
                kind=top_kind if not uncertain else SectionKind.PROSE,
                confidence=top_conf,
                candidates=[ClassificationCandidate(k, c) for k, c in scores[:3]],
                uncertain=uncertain,
            )

        # 3. Uncertain fallback.
        return ClassificationResult(
            kind=SectionKind.PROSE,
            confidence=0.0,
            candidates=[ClassificationCandidate(SectionKind.PROSE, 0.0)],
            uncertain=True,
        )

    def _vocab_match(self, heading: str, vocab: TenantVocabulary) -> SectionKind | None:
        h = heading.lower().strip()
        if not h:
            return None
        section_headings: dict[str, list[str]] = vocab.body.get("section_headings", {})
        best: tuple[SectionKind, int] | None = None
        for kind_value, patterns in section_headings.items():
            for p in patterns:
                p_low = p.lower()
                if p_low and p_low in h:
                    if best is None or len(p_low) > best[1]:
                        best = (SectionKind(kind_value), len(p_low))
        return best[0] if best else None
