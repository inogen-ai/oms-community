from oms.domain.models import Edge
from oms.domain.types import EdgeType


def test_same_display_tag_replacement_advances_skill_generation(mutation_case):
    case = mutation_case
    case.store.upsert_tag("legacy-tag", "receipts")
    case.store.upsert_tag("replacement-tag", "receipts")
    case.store.attach_edge(Edge(EdgeType.TAGGED_WITH, "expenses", "legacy-tag"), tenant_id="acme")
    before = case.capture_generation()

    def replace(graph, reviews):
        graph.detach_edge(Edge(EdgeType.TAGGED_WITH, "expenses", "legacy-tag"), tenant_id="acme")
        graph.attach_edge(Edge(EdgeType.TAGGED_WITH, "expenses", "replacement-tag"), tenant_id="acme")
    case.repository.atomic("replace-tag-identity", "acme", replace)
    assert case.store.tags_for_skill("expenses", tenant_id="acme") == ["receipts"]
    assert case.capture_generation().content == before.content + 1
