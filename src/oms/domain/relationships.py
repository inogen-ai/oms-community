"""Allowed endpoint labels for scoped graph relationships."""
from oms.domain.types import EdgeType


EDGE_LABELS: dict[EdgeType, tuple[tuple[str, str], ...]] = {
    EdgeType.BELONGS_TO: (("Rule", "Skill"),),
    EdgeType.TAGGED_WITH: (("Skill", "Tag"), ("Rule", "Tag")),
    EdgeType.DERIVED_FROM: (("Rule", "Transaction"), ("ContentBlock", "Transaction")),
    EdgeType.SUPERSEDES: (("Rule", "Rule"), ("ContentBlock", "ContentBlock")),
    EdgeType.CONFLICTS_WITH: (("Rule", "Constraint"), ("Rule", "Rule")),
    EdgeType.ROUTES_TO: (("Skill", "Skill"),),
    EdgeType.HAS_REFERENCE: (("Skill", "Artefact"),),
    EdgeType.HAS_ARTEFACT: (("Skill", "Artefact"),),
    EdgeType.HAS_SECTION: (("Skill", "Section"),),
    EdgeType.CONTAINS_RULE: (("Section", "Rule"),),
    EdgeType.CONTAINS_BLOCK: (("Section", "ContentBlock"),),
    EdgeType.HAS_EXAMPLE: (("Skill", "Example"), ("Section", "Example"), ("Rule", "Example")),
    EdgeType.OBSERVED_IN: (("Rule", "Transaction"),),
    EdgeType.ILLUSTRATES: (("Example", "Rule"), ("Example", "Skill")),
    **{kind: (("Rule", "Rule"),) for kind in (
        EdgeType.RELATED_TO, EdgeType.DUPLICATE_OF, EdgeType.EQUIVALENT_TO,
        EdgeType.RELATES_TO, EdgeType.CROSS_SKILL_DIVERGENCE,
    )},
}
