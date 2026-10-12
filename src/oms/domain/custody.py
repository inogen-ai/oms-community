"""Effective custody for legacy examples that also retain contextual parent IDs."""
from oms.domain.models import Example


def effective_example_parent(example: Example) -> tuple[str, str]:
    """Preserve historical section, then rule, then skill attachment priority."""
    for label, identifier in (("Section", example.parent_section_id),
                              ("Rule", example.parent_rule_id),
                              ("Skill", example.parent_skill_id)):
        if identifier is not None:
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Example requires a valid effective parent")
            return label, identifier
    raise ValueError("Example requires an effective parent")
