"""Optional enrichment at the core import boundary.

The exact matcher and custody importer operate without an extension. An edition
may select a candidate and enrich a proposed Rule before it is persisted.
"""
from typing import Protocol, runtime_checkable
from oms.domain.models import Rule


@runtime_checkable
class ImportExtension(Protocol):
    def match_rule(self, proposed: Rule, candidates: dict[str, Rule]) -> Rule | None: ...
    def rule_stored(self, rule: Rule) -> None: ...
    def skill_imported(self, skill_id: str, tenant_id: str) -> int: ...
