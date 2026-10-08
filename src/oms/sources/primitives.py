"""The small immutable types the rest of the feature is built from.

Evidence with its three kinds (known, absent, unknown), origin and snapshot references,
manifest entries, generations, the acquisition requests and results, and the exact
digest and canonical content values that retained snapshots and live projections
share so they compare like for like.
"""
from enum import StrEnum
from hashlib import sha256
import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from oms.domain.identity import SkillRef
from oms.domain.types import Mutability

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
NonEmpty = Annotated[str, Field(min_length=1)]
Generation = Annotated[int, Field(ge=0, strict=True)]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    format_version: Literal[1] = 1


class _FrozenList(list):
    def __deepcopy__(self, memo):
        return self

    def _refuse(self, *args, **kwargs):
        raise TypeError("Source evidence is immutable")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = _refuse
    reverse = sort = __iadd__ = __imul__ = _refuse


class _FrozenDict(dict):
    def __deepcopy__(self, memo):
        return self

    def _refuse(self, *args, **kwargs):
        raise TypeError("Source evidence is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _refuse


def _freeze(value: JSONValue) -> JSONValue:
    if isinstance(value, dict):
        return _FrozenDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


class Evidence(Record):
    kind: Literal["known", "absent", "unknown"]
    value: JSONValue = None
    source_digest: NonEmpty | None = None
    policy_version: NonEmpty

    @field_validator("value")
    @classmethod
    def immutable_value(cls, value: JSONValue) -> JSONValue:
        return _freeze(value)

    @model_validator(mode="after")
    def certainty(self) -> Self:
        if self.kind == "known" and "value" not in self.model_fields_set:
            raise ValueError("Known evidence requires an explicit value, including null")
        if self.kind != "known" and self.value is not None:
            raise ValueError("Absent or unknown evidence cannot carry a value")
        return self


class OriginRef(Record):
    skill: SkillRef
    origin_id: NonEmpty
    kind: Literal["github", "local"]
    generation: Generation


class SnapshotRef(Record):
    snapshot_id: NonEmpty
    origin: OriginRef

    def is_base_for(self, current: OriginRef) -> bool:
        prior = self.origin
        return (prior.skill == current.skill and prior.origin_id == current.origin_id
                and prior.kind == current.kind and prior.generation <= current.generation)


class ManifestEntry(Record):
    path: NonEmpty
    blob_ref: NonEmpty
    digest: NonEmpty
    size: Annotated[int, Field(ge=0)]
    mode: Literal[0o100644, 0o100755] | None
    published: bool = True
    exclusion_reason: str | None = None

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (value.startswith("/") or "\\" in value or "\x00" in value
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("Manifest path must be a safe relative path")
        return value


class PartKind(StrEnum):
    FIELD = "field"
    SECTION = "section"
    RULE = "rule"
    EXAMPLE = "example"
    FILE = "file"
    PLACEMENT = "placement"


class Generations(Record):
    skill: SkillRef
    content: Generation
    binding: Generation


class PlanFingerprint(Record):
    content_generation: Generation
    binding_generation: Generation
    update_generation: Generation
    policy_version: NonEmpty
    local_digest: NonEmpty
    candidate_digest: NonEmpty
    policy_digest: NonEmpty | None = None


def exact_digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def section_value(section, body: str) -> dict:
    return {"heading": section.heading, "kind": section.kind.value,
            "body": "" if section.mutability is Mutability.SYSTEM_AGGREGATED else body,
            "order": section.order, "mutability": section.mutability.value}


def rule_value(rule, section_id: str | None, order: int | None, group: str | None) -> dict:
    return {"body": rule.body, "polarity": rule.polarity.value,
            "reference_only": rule.reference_only, "section_id": section_id,
            "order": order, "group": group,
            "status": getattr(getattr(rule, "status", None), "value", "active"),
            "corroboration_count": getattr(rule, "corroboration_count", 1)}


def example_value(example, *, rule_id=None, section_id=None, skill_id=None, order=None) -> dict:
    return {"body": example.body, "kind": example.kind.value, "name": example.name,
            "original_label": example.original_label, "parent_rule_id": rule_id,
            "parent_section_id": section_id, "parent_skill_id": skill_id, "order": order}


class RefRequest(Record):
    tenant_id: NonEmpty
    url: NonEmpty
    credential_profile_id: NonEmpty | None = None
    ref_kind: Literal["branch", "tag", "commit"] | None = None
    ref_name: NonEmpty | None = None
    package_path: str | None = None


class ResolvedRef(Record):
    canonical_url: NonEmpty
    kind: Literal["branch", "tag", "commit"]
    name: NonEmpty
    package_path: str = ""
    commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class PackageRequest(Record):
    tenant_id: NonEmpty
    resolved_ref: ResolvedRef
    package_path: str
    credential_profile_id: NonEmpty | None = None
    baseline_commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")] | None = None


class AcquiredPackage(Record):
    history_evidence: Literal["proven_ancestor", "rewritten", "unproven", "initial"]
    resolved_ref: ResolvedRef
    package_path: str
    manifest: tuple[ManifestEntry, ...]
    raw_frontmatter: Evidence
    warnings: tuple[str, ...] = ()


class LocalPackage(Record):
    """Retained local bytes have a package digest, without a repository or Git ref."""
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    package_path: str
    manifest: tuple[ManifestEntry, ...]
    raw_frontmatter: Evidence


class DiscoveryRequest(Record):
    ref: RefRequest
    actor_id: NonEmpty
    discovery_root: str = ""


class DiscoveredPackage(Record):
    path: str
    valid: bool
    upstream_name: str | None = None
    description: str | None = None
    file_count: Annotated[int, Field(ge=0)] = 0
    total_bytes: Annotated[int, Field(ge=0)] = 0
    reasons: tuple[str, ...] = ()
    unsupported_metadata: tuple[str, ...] = ()


class DiscoveryResult(Record):
    discovery_id: NonEmpty
    tenant_id: NonEmpty
    actor_id: NonEmpty
    resolved_ref: ResolvedRef
    credential_profile_id: NonEmpty | None = None
    expires_at: AwareDatetime
    packages: tuple[DiscoveredPackage, ...]
    acquired_packages: tuple[AcquiredPackage, ...] = ()
    preselected_path: str | None = None
