"""An injection hold on a copy is a hold on a rule the transaction created."""
from datetime import datetime, timezone

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.copies import learning_copy_id, split_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import Edge, ReviewItem, Rule, Transaction
from oms.domain.types import EdgeType, SignalType, SourceRuntime, Verdict
from oms.sources.safety import _created_by_injection


def _item(subject_id: str) -> ReviewItem:
    return ReviewItem(id=f"injection-{subject_id}-t1", kind="injection", subject_id=subject_id,
                      other_id=None, verdict=Verdict.AMBIGUOUS, reason="", tenant_id="acme",
                      transaction_id="t1")


def test_every_copy_the_transaction_created_counts_as_created():
    graph = InMemoryGraphStore()
    for rule_id in ("rule-t1", learning_copy_id("t1", SkillRef("acme", "blog")),
                    split_rule_id("rule-t1", SkillRef("acme", "email"))):
        assert _created_by_injection(graph, _item(rule_id)), rule_id
    assert not _created_by_injection(graph, _item("rule-survivor"))


def test_a_hold_without_its_transaction_reads_the_one_compiled_lineage() -> None:
    # An older hold names no transaction and has an id of another shape, so
    # the rule's lineage decides: its one compiled transaction created it.
    graph = InMemoryGraphStore()
    graph.upsert_transaction(Transaction(id="t1", signal_type=SignalType.EXPLICIT_CORRECTION,
                                         source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref="t1.json",
                                         timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc), tenant_id="acme"))
    copy_id = learning_copy_id("t1", SkillRef("acme", "blog"))
    for rule_id in (copy_id, "rule-survivor"):
        graph.upsert_rule(Rule(id=rule_id, body="Use formal language.", tenant_id="acme"))
        graph.attach_edge(Edge(EdgeType.DERIVED_FROM, rule_id, "t1"), tenant_id="acme")

    def held(rule_id: str) -> ReviewItem:
        return ReviewItem(id=f"legacy-hold-{rule_id}", kind="injection", subject_id=rule_id, other_id=None,
                          verdict=Verdict.AMBIGUOUS, reason="", tenant_id="acme")

    assert _created_by_injection(graph, held(copy_id))
    assert not _created_by_injection(graph, held("rule-survivor"))
