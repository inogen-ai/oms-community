"""Behavioral contracts for focused storage views, using installed core code."""
from datetime import datetime, timedelta, timezone

import pytest

from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import (
    ContentBlock, Edge, Principal, Publication, ReviewItem, Rule, Section,
    Skill, SkillVersion, Transaction,
)
from oms.domain.types import (
    BlockStatus, EdgeType, Mutability, SectionKind, SignalType, SkillVersionCause,
    SourceRuntime, Verdict,
)
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.service import IngestionService, TenantMismatchError
from oms.ports.catalogue_policy import SingleWorkspaceReadPolicy
from oms.ports.graph_store import SectionMutabilityError
from oms.ports.repositories import (
    CatalogueRepository, CustodyRepository, GraphReader, HistoryCaptureRepository,
    HistoryRepository, IngestionRepository, PublicationRepository, ReviewRepository,
    RuleRepository, SkillRepository, TransactionRepository,
)
from oms.publish.catalogue import SkillCatalogue, SkillNotFound
from oms.publish.publisher import Publisher
from oms.skills.history import SkillHistory
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


WHEN = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def repositories(request):
    if request.param == "memory":
        return InMemoryGraphStore(), InMemoryReviewQueue()
    from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
    from oms.adapters.neo4j.store import Neo4jGraphStore

    # The shared fixture only accepts a fresh container or an explicitly
    # acknowledged disposable database; configured connection failures fail.
    driver = request.getfixturevalue("critic_neo4j_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    store = Neo4jGraphStore(driver)
    store.ensure_schema()
    return store, Neo4jReviewQueue(driver)


def _transaction(identifier, tenant="acme", minute=0):
    return Transaction(
        id=identifier, signal_type=SignalType.EXPLICIT_CORRECTION,
        source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref=f"{identifier}.json",
        timestamp=WHEN + timedelta(minutes=minute), tenant_id=tenant)


def _skill(store, identifier="expenses", tenant="acme"):
    skill = Skill(id=identifier, name="Expenses", description="Expense process",
                  domain="finance", tenant_id=tenant)
    store.upsert_skill(skill)
    return skill


def _only(repository, contract):
    """An adapter facade exposing exactly one protocol, with no fallback."""
    def forward(name):
        def call(_self, *args, **kwargs):
            return getattr(repository, name)(*args, **kwargs)
        return call
    members = {name: forward(name) for name in contract.__protocol_attrs__}
    return type(f"Only{contract.__name__}", (), members)()


@pytest.mark.parametrize("contract", [
    TransactionRepository, SkillRepository, RuleRepository, CustodyRepository,
    PublicationRepository, HistoryRepository, GraphReader, IngestionRepository,
    HistoryCaptureRepository, CatalogueRepository,
])
def test_both_core_graph_adapters_satisfy_the_narrow_views(repositories, contract):
    store, _queue = repositories
    assert isinstance(store, contract)
    assert isinstance(_only(store, contract), contract)


def test_review_repository_preserves_one_decision_and_workspace_history(repositories):
    _store, queue = repositories
    assert isinstance(queue, ReviewRepository)
    view = _only(queue, ReviewRepository)
    for tenant in ("acme", "other"):
        view.enqueue(ReviewItem(
            id=f"review-{tenant}", kind="manual_learning", subject_id=f"txn-{tenant}",
            other_id=None, verdict=Verdict.AMBIGUOUS, reason="Needs a decision",
            tenant_id=tenant))
    decided = view.resolve("review-acme", "approved", decided_by="reviewer-1")
    assert decided.resolved and decided.decided_by == "reviewer-1" and decided.decided_at
    with pytest.raises(ValueError):
        view.resolve("review-acme", "rejected", decided_by="reviewer-2")
    assert view.pending("acme") == []
    assert [item.id for item in view.pending("other")] == ["review-other"]
    assert view.history("other") == []
    history = view.history("acme", subject_id="txn-acme")
    assert [(item.id, item.resolution, item.decided_by) for item in history] == [
        ("review-acme", "approved", "reviewer-1")]


def test_transaction_filter_precedes_limit_and_lineage_is_chronological(repositories):
    store, _queue = repositories
    transactions = _only(store, TransactionRepository)
    rules = _only(store, RuleRepository)
    for identifier, tenant, minute in (("foreign", "other", 20), ("first", "acme", 0),
                                        ("third", "acme", 10), ("second", "acme", 5)):
        transactions.upsert_transaction(_transaction(identifier, tenant, minute))
    assert [row.id for row in transactions.transactions_for_tenant("acme", limit=2)] == [
        "third", "second"]
    assert [row.id for row in transactions.transactions_for_tenant(
        "acme", since=WHEN + timedelta(minutes=5), limit=2)] == ["third", "second"]
    rule = Rule(id="receipt-rule", body="Keep receipts.", tenant_id="acme")
    rules.upsert_rule(rule)
    for identifier in ("third", "first", "second"):
        rules.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id=rule.id, to_id=identifier))
    reader = _only(store, GraphReader)
    assert [row.id for row in reader.lineage(rule.id)] == ["first", "second", "third"]
    assert not hasattr(reader, "run_query")
    assert not hasattr(reader, "upsert_rule")


def test_publication_deletion_cannot_cross_workspace_or_skill(repositories):
    store, _queue = repositories
    view = _only(store, PublicationRepository)
    for identifier, tenant, skill, path in (
        ("a", "acme", "expenses", "expenses/SKILL.md"),
        ("b", "other", "expenses", "expenses/SKILL.md"),
        ("c", "acme", "travel", "travel/SKILL.md"),
    ):
        view.upsert_publication(Publication(
            id=identifier, skill_id=skill, source_ref=path, content_hash=identifier,
            published_at=WHEN, tenant_id=tenant))
    view.delete_publication("acme", "expenses/SKILL.md")
    assert view.get_publication("acme", "expenses/SKILL.md") is None
    assert view.get_publication("other", "expenses/SKILL.md").id == "b"
    view.delete_publications_for_skill("acme", "expenses")
    assert [row.id for row in view.publications_for_skill("other", "expenses")] == ["b"]
    assert [row.id for row in view.publications_for_skill("acme", "travel")] == ["c"]


def test_custody_supersession_keeps_original_evidence_and_rule_placement(repositories):
    store, _queue = repositories
    _skill(store)
    view = _only(store, CustodyRepository)
    section = Section(id="expense-policy", skill_id="expenses", kind=SectionKind.PROSE,
                      heading="Process", order=1, mutability=Mutability.AUTHORIAL_PASSTHROUGH,
                      tenant_id="acme")
    view.upsert_section(section)
    old = ContentBlock(id="before", content_ref="sha256-before", kind=SectionKind.PROSE,
                       tenant_id="acme", source_ref="expenses/SKILL.md", body="Old policy.")
    new = ContentBlock(id="after", content_ref="sha256-after", kind=SectionKind.PROSE,
                       tenant_id="acme", source_ref="expenses/SKILL.md", body="Updated policy.")
    view.upsert_content_block(old)
    view.attach_block(old, section.id)
    view.supersede_block(old.id, new, section.id)
    assert [(block.id, block.body) for block in view.blocks_for_section(section.id)] == [
        ("after", "Updated policy.")]
    assert view.get_content_block(old.id).body == "Old policy."
    assert view.get_content_block(old.id).status is BlockStatus.SUPERSEDED
    assert view.section_for_block(old.id).id == section.id
    rule = Rule(id="receipt-rule", body="Keep receipts.", tenant_id="acme")
    store.upsert_rule(rule)
    with pytest.raises(SectionMutabilityError):
        view.attach_rule(rule, section.id, order=7, group="Evidence")
    rules_section = Section(id="expense-rules", skill_id="expenses", kind=SectionKind.RULES,
                            heading="Rules", order=2, mutability=Mutability.SYSTEM_AGGREGATED,
                            tenant_id="acme")
    view.upsert_section(rules_section)
    view.attach_rule(rule, rules_section.id, order=7, group="Evidence")
    placement = view.rule_placements_for_section(rules_section.id)
    assert [(row.rule_id, row.order, row.group) for row in placement] == [
        (rule.id, 7, "Evidence")]


def test_history_retention_keeps_oldest_and_cannot_trim_another_scope(repositories):
    store, _queue = repositories
    view = _only(store, HistoryRepository)
    for tenant, skill in (("acme", "expenses"), ("other", "expenses"), ("acme", "travel")):
        for index in range(4):
            view.append_skill_version(SkillVersion(
                id=f"{tenant}-{skill}-{index}", skill_id=skill, tenant_id=tenant,
                at=WHEN + timedelta(minutes=index), revision=str(index),
                cause=SkillVersionCause.CONSOLE_EDIT))
    assert view.trim_skill_versions("acme", "expenses", keep=1) == 2
    assert [row.revision for row in view.skill_versions("acme", "expenses")] == ["3", "0"]
    assert view.latest_skill_version("acme", "expenses").revision == "3"
    assert len(view.skill_versions("other", "expenses")) == 4
    assert len(view.skill_versions("acme", "travel")) == 4


def test_ingestion_operates_through_only_its_declared_repository(tmp_path):
    store = InMemoryGraphStore()
    view = _only(store, IngestionRepository)
    service = IngestionService(view, RegexSanitiser(), FilePayloadStore(tmp_path))
    principal = Principal(id="author", tenant_id="acme", scopes=frozenset({INGEST_WRITE}))
    payload = correction_payload(ContributionRequest(
        transaction_id="receipt", correction="Keep receipts for expense claims."),
        principal, SourceRuntime.MCP)
    created = service.ingest(payload, principal)
    assert created == service.ingest(payload, principal)
    assert store.get_transaction("receipt").summary == "Keep receipts for expense claims."
    foreign = Principal(id="foreign", tenant_id="other", scopes=principal.scopes)
    with pytest.raises(TenantMismatchError):
        service.ingest(payload, foreign)
    assert not hasattr(view, "claim_compile")


def test_history_capture_operates_through_only_its_declared_repository():
    store = InMemoryGraphStore()
    _skill(store)
    rule = Rule(id="receipt-rule", body="Keep receipts.", tenant_id="acme")
    store.upsert_rule(rule)
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule.id, to_id="expenses"))
    history = SkillHistory(store=_only(store, HistoryCaptureRepository), publisher=Publisher(store))
    first = history.capture_required("expenses", "acme", cause=SkillVersionCause.CREATED)
    assert first is not None
    rule.body = "Keep itemised receipts."
    store.upsert_rule(rule)
    history.capture_for_rule(rule.id, "acme", cause=SkillVersionCause.RULE_EDIT, actor="reviewer")
    versions = store.skill_versions("acme", "expenses")
    assert len(versions) == 2
    assert versions[0].actor_person_id == "reviewer"
    assert "Keep itemised receipts." in versions[0].rules_json
    assert "Keep itemised receipts." not in versions[1].rules_json


def test_catalogue_operates_through_only_its_declared_repository():
    store = InMemoryGraphStore()
    _skill(store)
    store.upsert_rule(Rule(id="receipt-rule", body="Keep receipts.", tenant_id="acme"))
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="receipt-rule", to_id="expenses"))
    catalogue = SkillCatalogue(_only(store, CatalogueRepository), Publisher(store),
                               read_policy=SingleWorkspaceReadPolicy("acme"))
    assert [skill.id for skill in catalogue.list_skills("acme")] == ["expenses"]
    assert "Keep receipts." in catalogue.query_skill("acme", "expenses").body
    assert len(store.usage_events("acme")) == 1
    assert catalogue.list_skills("other") == []
    with pytest.raises(SkillNotFound):
        catalogue.query_skill("other", "expenses")
    assert len(store.usage_events("acme")) == 1


def test_public_views_exclude_model_scheduling_and_arbitrary_query_operations():
    prohibited = {"run_query", "embed", "semantic_search", "pending_transactions",
                  "claim_compile", "release_compile", "authorial_revision"}
    for contract in (TransactionRepository, SkillRepository, RuleRepository, CustodyRepository,
                     PublicationRepository, ReviewRepository, HistoryRepository, GraphReader):
        assert not (contract.__protocol_attrs__ & prohibited), contract.__name__
