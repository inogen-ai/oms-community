"""The typed requests the service methods take. None of them carries authority; that comes from the action
context the edition's admission builds."""
from typing import Literal, Self

from pydantic import Field, model_validator

from oms.domain.identity import SkillRef
from oms.sources.primitives import Generation, Generations, LocalPackage, NonEmpty, PlanFingerprint, Record, ResolvedRef


class DraftChoice(Record):
    part_id: NonEmpty
    choice: Literal["keep_oms", "use_upstream", "merged_text"]
    merged_text: str | None = None
    part_fingerprint: NonEmpty

    @model_validator(mode="after")
    def merged_choice(self) -> Self:
        if (self.choice == "merged_text") != (self.merged_text is not None):
            raise ValueError("Only a merged-text choice carries replacement text")
        return self


class InstallSelection(Record):
    package_path: str
    local_name: NonEmpty
    domain: NonEmpty


class InstallRequest(Record):
    discovery_id: NonEmpty
    selections: tuple[InstallSelection, ...]


class CreateSourceRequest(Record):
    discovery_id: NonEmpty


class CheckRequest(Record):
    source_id: NonEmpty
    expected_source_generation: Generation
    skills: tuple[SkillRef, ...]


class LinkRequest(Record):
    skill: SkillRef
    source_id: NonEmpty
    package_path: str
    ref: ResolvedRef
    expected_content_generation: Generation
    expected_binding_generation: Generation


class RetargetRequest(Record):
    skill: SkillRef
    ref: ResolvedRef
    expected_content_generation: Generation
    expected_binding_generation: Generation


class UpdateRequest(Record):
    update_id: NonEmpty
    skill: SkillRef
    fingerprint: PlanFingerprint


class SaveDraftRequest(UpdateRequest):
    choices: tuple[DraftChoice, ...]


class ApplyRequest(UpdateRequest):
    removal_consents: tuple[str, ...] = ()


class UndoRequest(Record):
    undo_id: NonEmpty
    skill: SkillRef
    expected_generations: tuple[Generations, ...]
    policy_version: NonEmpty


class BulkApplyRequest(Record):
    updates: tuple[ApplyRequest, ...]


class UnlinkRequest(Record):
    skill: SkillRef
    expected_content_generation: Generation
    expected_binding_generation: Generation


class RemoveSourceRequest(Record):
    source_id: NonEmpty
    expected_source_generation: Generation
    expected_generations: tuple[Generations, ...]


class RelocateSourceRequest(RemoveSourceRequest):
    destination_url: NonEmpty


class AutomationRequest(UnlinkRequest):
    enabled: bool


class ScheduleRequest(RemoveSourceRequest):
    enabled: bool


class LocalImportRequest(Record):
    skill: SkillRef
    package: LocalPackage
    expected_content_generation: Generation
    expected_binding_generation: Generation
    create: bool = False
    local_name: str | None = None
    domain: str | None = None
    transport: Literal["zip", "folder", "cli"]
    filename: str | None = None
    approve: bool = False
    removal_consents: tuple[str, ...] = ()

    @model_validator(mode="after")
    def new_identity(self) -> Self:
        supplied = bool(self.local_name and self.local_name.strip()) and bool(self.domain and self.domain.strip())
        if self.create != supplied or not self.create and (self.local_name is not None or self.domain is not None):
            raise ValueError("Only a new local skill supplies its name and domain")
        return self


class LocalBatchRequest(Record):
    requests: tuple[LocalImportRequest, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_targets(self) -> Self:
        if len({item.skill for item in self.requests}) != len(self.requests):
            raise ValueError("Each local batch target must be selected once")
        return self
