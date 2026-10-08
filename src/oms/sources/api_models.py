"""Request bodies and response views for the source routes.

A body names things by their visible ids and nothing more; who the caller is and
which workspace they act in comes from the edition's admission, never from the body.
"""
from typing import Annotated, Literal
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from oms.domain.models import SkillVersion
from oms.sources.models import CheckResult, DraftChoice, OperationResult, Page, Update
from oms.sources.primitives import DiscoveredPackage, Generation, PlanFingerprint, ResolvedRef

Identifier = Annotated[str, Field(min_length=1, max_length=1000)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoverBody(Body):
    url: str = Field(min_length=1, max_length=4096)
    credential_profile_id: Identifier | None = None
    ref_kind: Literal["branch", "tag", "commit"] | None = None
    ref_name: Identifier | None = None
    package_path: str | None = Field(default=None, max_length=4096)
    discovery_root: str = Field(default="", max_length=4096)


class InstallSelectionBody(Body):
    package_path: str = Field(max_length=4096)
    local_name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=200)


class InstallBody(Body):
    discovery_id: Identifier
    selections: tuple[InstallSelectionBody, ...] = Field(min_length=1, max_length=100)


class CreateSourceBody(Body):
    discovery_id: Identifier


class CheckBody(Body):
    expected_source_generation: Generation
    skill_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=10_000)


class BindingBody(Body):
    expected_content_generation: Generation
    expected_binding_generation: Generation


class LinkBody(BindingBody):
    source_id: Identifier
    package_path: str = Field(max_length=4096)
    ref: ResolvedRef


class RetargetBody(BindingBody):
    ref: ResolvedRef


class AutomationBody(BindingBody):
    enabled: bool = Field(strict=True)


class UpdateBody(Body):
    skill_id: Identifier
    fingerprint: PlanFingerprint


class DraftBody(UpdateBody):
    choices: tuple[DraftChoice, ...] = Field(max_length=10_000)


class ApplyBody(UpdateBody):
    removal_consents: tuple[Identifier, ...] = Field(default=(), max_length=10_000)


class BulkApplyItem(ApplyBody):
    update_id: Identifier


class BulkApplyBody(Body):
    updates: tuple[BulkApplyItem, ...] = Field(min_length=1, max_length=100)


class SkillGenerationBody(Body):
    skill_id: Identifier
    content: Generation
    binding: Generation


class UndoBody(Body):
    undo_id: Identifier
    skill_id: Identifier
    expected_generations: tuple[SkillGenerationBody, ...] = Field(min_length=1, max_length=10_000)
    policy_version: Identifier


class RemoveSourceBody(Body):
    expected_source_generation: Generation
    expected_generations: tuple[SkillGenerationBody, ...] = Field(max_length=10_000)


class RelocateSourceBody(RemoveSourceBody):
    destination_url: str = Field(min_length=1, max_length=4096)


class ScheduleBody(RemoveSourceBody):
    enabled: bool = Field(strict=True)


class SkillOutcomeView(Body):
    skill_id: str
    state: Literal["applied", "awaiting_review", "blocked", "failed", "unchanged"]
    update_id: str | None = None
    code: str | None = None


class OperationView(Body):
    operation_id: str
    state: Literal["fetching", "planning", "awaiting_review", "applying", "complete", "blocked", "failed"]
    committed: bool
    outcomes: tuple[SkillOutcomeView, ...]
    source_id: str | None = None
    discovery_id: str | None = None

    @classmethod
    def from_result(cls, result: OperationResult) -> "OperationView":
        return cls(operation_id=result.operation_id, state=result.state, committed=result.committed,
                   source_id=result.source_id, discovery_id=result.discovery_id,
                   outcomes=tuple(SkillOutcomeView(skill_id=row.skill.skill_id, state=row.state,
                       update_id=row.update_id, code=row.code) for row in result.outcomes))


class DiscoveryView(Body):
    discovery_id: str
    resolved_ref: ResolvedRef
    expires_at: str
    preselected_path: str | None
    packages: Page[DiscoveredPackage]


class FilePreview(Body):
    path: str
    side: Literal["base", "local", "upstream", "discovery"]
    size: int
    binary: bool
    text: str | None
    truncated: bool
    byte_limit: int = 65536
    line_limit: int = 1000
    media_type: Literal["text/plain"] = "text/plain"


class UpdateView(Update):
    part_fingerprints: dict[str, str]
    ownership: dict[str, Literal["source", "shared", "local", "unknown"]]
    stale: bool
    resolved_check: CheckResult = CheckResult(state="unavailable", code="review_not_evaluated")


class UndoView(UndoBody):
    update_id: Identifier
    available: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SourceHistoryEntry(SkillVersion):
    undo: UndoView | None = None
