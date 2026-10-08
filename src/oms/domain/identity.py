"""Tenant-qualified identity without changing the visible skill name."""
from dataclasses import dataclass
import json


@dataclass(frozen=True, order=True)
class SkillRef:
    tenant_id: str
    skill_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.tenant_id, self.skill_id)):
            raise ValueError("Skill identity requires a tenant and skill ID")

    @property
    def storage_key(self) -> str:
        return json.dumps([self.tenant_id, self.skill_id], ensure_ascii=False,
                          separators=(",", ":"))

    @classmethod
    def from_storage_key(cls, key: str) -> "SkillRef":
        values = json.loads(key)
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("Invalid skill storage key")
        return cls(*values)
