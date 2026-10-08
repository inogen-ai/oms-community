"""Explicit, revision-checked placement of a manually created rule."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from oms.domain.types import Mutability, SectionKind
from oms.publish.render import publishable


class RuleInsertion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    skill_id: str = Field(min_length=1, max_length=1000)
    revision: str = Field(min_length=1, max_length=100)
    section_id: str = Field(min_length=1, max_length=1000)
    position: Literal["start", "end", "after"] = "end"
    after_rule_id: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def after_target(self):
        if (self.position == "after") != (self.after_rule_id is not None):
            raise ValueError("choose an existing rule only for an after position")
        return self


def section_rules(store, section_id, *, tenant_id, include_inactive=False):
    """Use the renderer's placed-then-unplaced order, retaining subheadings."""
    placements = {p.rule_id: p for p in store.rule_placements_for_section(section_id, tenant_id=tenant_id)
                  if p.order is not None}
    rules = [rule for rule in store.rules_for_section(section_id, tenant_id=tenant_id)
             if include_inactive or (publishable(rule) and not rule.reference_only)]
    placed = sorted((rule for rule in rules if rule.id in placements), key=lambda rule: placements[rule.id].order)
    unplaced = sorted((rule for rule in rules if rule.id not in placements),
                      key=lambda rule: rule.corroboration_count, reverse=True)
    return [(rule, placements[rule.id].group if rule.id in placements else None)
            for rule in placed + unplaced]


def insertion_sections(store, skill_id, *, tenant_id):
    return [{"id": section.id, "heading": section.heading, "rules": [
        {"id": rule.id, "body": rule.body, "group": group}
        for rule, group in section_rules(store, section.id, tenant_id=tenant_id)]}
        for section in sorted(store.sections_for_skill(skill_id, tenant_id=tenant_id), key=lambda section: section.order)
        if section.kind is SectionKind.RULES and section.mutability is Mutability.SYSTEM_AGGREGATED]


def insert_rule(store, rule, insertion):
    visible = section_rules(store, insertion.section_id, tenant_id=rule.tenant_id)
    if insertion.position == "after" and insertion.after_rule_id not in {existing.id for existing, _ in visible}:
        raise ValueError("The selected rule is no longer in this section. Reload the document.")
    # Retired guidance retains an unambiguous place if the user restores it.
    rows = section_rules(store, insertion.section_id, tenant_id=rule.tenant_id, include_inactive=True)
    if insertion.position == "after":
        index = next((i + 1 for i, (existing, _) in enumerate(rows) if existing.id == insertion.after_rule_id), None)
        if index is None:
            raise ValueError("The selected rule is no longer in this section. Reload the document.")
    else:
        index = 0 if insertion.position == "start" else len(rows)
    # Keep insertion within its neighbouring authorial subgroup. Re-numbering
    # preserves existing order without fractional ranks or accidental ties.
    group = (next(group for existing, group in visible if existing.id == insertion.after_rule_id)
             if insertion.position == "after" else visible[0 if insertion.position == "start" else -1][1] if visible else None)
    rows.insert(index, (rule, group))
    for order, (existing, group) in enumerate(rows):
        store.attach_rule(existing, insertion.section_id, order=order, group=group, tenant_id=rule.tenant_id)
