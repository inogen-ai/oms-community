"""Read selection supplied to the catalogue by the composition root."""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oms.domain.models import Skill


@dataclass(frozen=True)
class CatalogueSelection:
    tenant_id: str
    allowed: bool = True
    domains: frozenset[str] | None = None
    omitted_listing_ids: frozenset[str] = frozenset()

    def can_read(self, skill: Skill) -> bool:
        return (self.allowed and skill.tenant_id == self.tenant_id
                and (self.domains is None or not skill.domain
                     or skill.domain in self.domains))

    def can_list(self, skill: Skill) -> bool:
        return self.can_read(skill) and skill.id not in self.omitted_listing_ids


@runtime_checkable
class CatalogueReadPolicy(Protocol):
    def for_reader(self, tenant_id: str, person_id: str | None) -> CatalogueSelection: ...


class SingleWorkspaceReadPolicy:
    """Local readers see all content in their configured workspace.

    An omitted workspace supports callers that already bind the tenant at
    their transport edge. A Community composition root passes its workspace
    explicitly, which also refuses attempts to select another one.
    """

    def __init__(self, workspace_id: str | None = None):
        self.workspace_id = workspace_id

    def for_reader(self, tenant_id: str, person_id: str | None) -> CatalogueSelection:
        return CatalogueSelection(
            tenant_id, allowed=self.workspace_id is None or self.workspace_id == tenant_id)
