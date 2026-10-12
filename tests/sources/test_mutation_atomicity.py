from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from oms.sources.errors import SourceConflict, StaleMutation


def test_editor_invalidates_prepared_write(mutation_case):
    old = mutation_case.capture_generation()
    mutation_case.edit_description("local correction")
    with pytest.raises(StaleMutation, match="content generation changed"):
        mutation_case.apply_prepared(old, "older prepared text")
    assert mutation_case.description() == "local correction"


def test_same_request_replays_without_duplicate_history_or_generation(mutation_case):
    old = mutation_case.capture_generation()
    result = mutation_case.apply_prepared(old, "updated")
    after = mutation_case.snapshot()
    assert mutation_case.apply_prepared(old, "updated") == result
    assert mutation_case.snapshot() == after
    with pytest.raises(SourceConflict, match="idempotency key reused"):
        mutation_case.apply_prepared(old, "another", digest="different")
    assert mutation_case.snapshot() == after


def test_revoked_authority_cannot_recover_a_previously_permitted_result(mutation_case):
    old = mutation_case.capture_generation()
    mutation_case.apply_prepared(old, "updated")
    mutation_case.denied = True
    with pytest.raises(PermissionError):
        mutation_case.apply_prepared(old, "updated")


def test_failed_final_write_rolls_back_content_history_event_and_operation(mutation_case):
    before = mutation_case.snapshot()

    def fail(context):
        raise OSError("Durable audit unavailable")
    with pytest.raises(OSError):
        mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=fail)
    assert mutation_case.snapshot() == before


def test_competing_prepared_changes_cannot_both_commit(mutation_case):
    generation = mutation_case.capture_generation()
    barrier = Barrier(2)

    def apply(number):
        barrier.wait(timeout=5)
        try:
            mutation_case.apply_prepared(generation, f"candidate {number}", operation_id=f"op-{number}")
            return "applied"
        except StaleMutation:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, (1, 2))) == ["applied", "stale"]
    assert mutation_case.capture_generation().content == generation.content + 1


def test_memory_rollback_removes_state_created_by_failed_callback(mutation_case):
    if hasattr(mutation_case.store, "_driver"):
        pytest.skip("Memory object rollback only")

    def fail(graph, queue):
        graph.created_during_failed_write = ["must roll back"]
        raise OSError("Injected failure")
    with pytest.raises(OSError):
        mutation_case.repository.atomic("failed", "acme", fail)
    assert not hasattr(mutation_case.store, "created_during_failed_write")


@pytest.mark.parametrize("collaborator,method", [
    ("graph", "upsert_skill"), ("history", "capture_required"),
    ("sources", "reserve_operation"), ("events", "append_event"),
])
def test_each_bound_write_failure_restores_the_before_state(mutation_case, monkeypatch, collaborator, method):
    before = mutation_case.snapshot()
    factory = mutation_case.factory

    def with_failure(graph, reviews):
        context = factory(graph, reviews)
        target = getattr(context, collaborator)
        original = getattr(target, method)

        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError(f"Injected {collaborator} failure")
        monkeypatch.setattr(target, method, fail_after_write)
        return context
    monkeypatch.setattr(mutation_case, "factory", with_failure)
    with pytest.raises(OSError):
        mutation_case.apply_prepared(mutation_case.capture_generation(), "updated")
    assert mutation_case.snapshot() == before


def test_undeclared_other_skill_mutation_aborts_every_write(mutation_case):
    from oms.domain.models import Skill
    mutation_case.store.upsert_skill(Skill(id="other", name="Other", description="preserve", domain="other", tenant_id="acme"))
    before = mutation_case.snapshot()

    def change_other(context):
        from oms.skills.service import SkillAdminService
        SkillAdminService(store=context.graph).update_metadata("other", "acme", description="unauthorized")
    with pytest.raises(SourceConflict, match="affected skill set changed"):
        mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=change_other)
    assert mutation_case.snapshot() == before
    assert mutation_case.store.get_skill("other", tenant_id="acme").description == "preserve"


def test_equivalent_placement_read_orders_have_identical_fingerprint(mutation_case, monkeypatch):
    from oms.domain.models import Rule, Section
    from oms.domain.types import Mutability, SectionKind
    from oms.sources.mutation import content_digest
    store = mutation_case.store
    store.upsert_section(Section(id="rules", skill_id="expenses", kind=SectionKind.RULES,
        heading="Rules", order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id="acme"))
    for number in range(2):
        rule = Rule(id=f"rule-{number}", body=f"Rule {number}", tenant_id="acme")
        store.upsert_rule(rule)
        store.attach_rule(rule, "rules", order=number, tenant_id="acme")
    expected = content_digest(store, mutation_case.skill)
    original = store.rule_placements_for_section
    monkeypatch.setattr(store, "rule_placements_for_section",
                        lambda *args, **kwargs: list(reversed(original(*args, **kwargs))))
    assert content_digest(store, mutation_case.skill) == expected


def test_adding_another_rule_owner_invalidates_the_original_owner(mutation_case):
    from oms.domain.models import Edge, Rule, Skill
    from oms.domain.types import EdgeType
    store = mutation_case.store
    store.upsert_rule(Rule(id="shared-rule", body="Keep receipts", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "shared-rule", "expenses"), tenant_id="acme")
    store.upsert_skill(Skill(id="other", name="Other", description="preserve", domain="finance", tenant_id="acme"))
    before = mutation_case.capture_generation()
    mutation_case.repository.atomic("attach-owner", "acme", lambda graph, queue:
        graph.attach_edge(Edge(EdgeType.BELONGS_TO, "shared-rule", "other"), tenant_id="acme"))
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(before, "stale ownership decision")
    assert mutation_case.capture_generation().content == before.content + 1


@pytest.mark.parametrize("target", ["graph", "reviews", "sources", "events"])
def test_conditional_callback_cannot_write_another_tenant(mutation_case, target):
    from oms.domain.models import ReviewItem, Skill
    from oms.domain.types import Verdict
    from oms.sources.errors import SourceForbidden
    from oms.sources.models import DurableEvent, Source
    before = mutation_case.snapshot()

    def write_foreign(context):
        if target == "graph":
            context.graph.upsert_skill(Skill(id="foreign", name="Foreign", description="denied",
                domain="finance", tenant_id="other"))
        elif target == "reviews":
            context.reviews.enqueue(ReviewItem(id="foreign", kind="skill_update", subject_id="foreign",
                other_id=None, verdict=Verdict.AMBIGUOUS, reason="denied", tenant_id="other"))
        elif target == "sources":
            context.sources.put_source(Source(source_id="foreign", tenant_id="other",
                                              canonical_url="https://github.com/example/skills"))
        else:
            context.events.append_event(DurableEvent(event_id="foreign", tenant_id="other",
                operation_id="foreign", action="source_update", skills=()))
    with pytest.raises(SourceForbidden):
        mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=write_foreign)
    assert mutation_case.snapshot() == before
    assert mutation_case.store.get_skill("foreign", tenant_id="other") is None
    assert mutation_case.sources.get_source("foreign", tenant_id="other") is None
    assert mutation_case.queue.get("foreign") is None


def test_required_restore_audit_failure_aborts_the_content_and_version(mutation_case):
    from dataclasses import replace
    from oms.domain.types import SkillVersionCause
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory
    from oms.skills.service import SkillAdminService

    class UnavailableEvents:
        def record_admin_event(self, event):
            raise OSError("Audit unavailable")
    before = mutation_case.snapshot()

    def factory(graph, reviews):
        context = mutation_case.factory(graph, reviews)
        history = SkillHistory(store=graph, publisher=Publisher(graph), admin_events=UnavailableEvents())
        return replace(context, history=history, rollback_participants=(history,))

    def apply(context):
        SkillAdminService(store=context.graph).update_metadata("expenses", "acme", description="restore")
        context.history.capture_required("expenses", "acme", cause=SkillVersionCause.RESTORE)
    with pytest.raises(OSError):
        mutation_case.repository.atomic_skill_change(mutation_case.request(mutation_case.capture_generation()), factory, apply)
    assert mutation_case.snapshot() == before


def test_callback_cannot_link_foreign_rules_or_rename_a_shared_tag(mutation_case):
    from oms.domain.models import Rule
    from oms.domain.types import EdgeType
    from oms.sources.errors import SourceForbidden
    for identifier in ("foreign-a", "foreign-b"):
        mutation_case.store.upsert_rule(Rule(id=identifier, body=identifier, tenant_id="other"))
    mutation_case.store.upsert_tag("shared", "Original")
    before = mutation_case.snapshot()

    def link(context):
        context.graph.upsert_cross_skill_edge(EdgeType.RELATED_TO, "foreign-a", "foreign-b", 1.0)

    def rename(context):
        context.graph.upsert_tag("shared", "Changed")
    for forbidden in (link, rename):
        with pytest.raises(SourceForbidden):
            mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=forbidden)
        assert mutation_case.snapshot() == before
    assert mutation_case.store.tag_name("shared") == "Original"


def test_callback_cannot_reassign_review_tenant_through_mutable_alias(mutation_case):
    from oms.domain.models import ReviewItem
    from oms.domain.types import Verdict

    def edit_aliases(context):
        item = ReviewItem(id="local-review", kind="skill_update", subject_id="expenses",
                         other_id=None, verdict=Verdict.AMBIGUOUS, reason="local", tenant_id="acme")
        context.reviews.enqueue(item)
        item.tenant_id = "other"
        loaded = context.reviews.get("local-review")
        loaded.tenant_id = "other"
    mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=edit_aliases)
    assert mutation_case.queue.get("local-review").tenant_id == "acme"


@pytest.mark.parametrize("method", ["enqueue", "enqueue_once"])
def test_callback_cannot_take_over_a_foreign_review_identity(mutation_case, method):
    from dataclasses import replace
    from oms.domain.models import ReviewItem
    from oms.domain.types import Verdict
    from oms.sources.errors import SourceForbidden
    foreign = ReviewItem(id="foreign-review", kind="skill_update", subject_id="other",
                         other_id=None, verdict=Verdict.AMBIGUOUS, reason="foreign", tenant_id="other")
    mutation_case.queue.enqueue(foreign)

    def collide(context):
        getattr(context.reviews, method)(replace(foreign, tenant_id="acme", reason="local"))
    with pytest.raises(SourceForbidden):
        mutation_case.apply_prepared(mutation_case.capture_generation(), "updated", fault=collide)
    assert mutation_case.queue.get("foreign-review") == foreign


def test_ownership_only_transaction_invalidates_prepared_content_once(mutation_case):
    from oms.sources.models import PartOwnership
    from oms.sources.mutation import source_repository
    old = mutation_case.capture_generation()

    def own(graph, reviews):
        repository = source_repository(graph)
        for name in ("description", "tags"):
            repository.put_ownership(PartOwnership(skill=mutation_case.skill, part_id="field:" + name,
                origin_ids=("local-origin",), entity_ids=("expenses",), proof_digest="proof-" + name))
    mutation_case.repository.atomic("ownership-change", "acme", own)
    assert mutation_case.capture_generation().content == old.content + 1
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(old, "stale owner plan")


def test_receipt_names_exact_committed_generations_and_tombstones(mutation_case):
    from oms.skills.service import SkillAdminService

    def edit(graph, reviews):
        SkillAdminService(store=graph).update_metadata("expenses", "acme", description="first stage")
        return "stage complete"
    result, generations, skills = mutation_case.repository.atomic_with_receipt("stage", "acme", edit)
    assert result == "stage complete" and skills == (mutation_case.skill,)
    assert generations == (mutation_case.capture_generation(),)
    mutation_case.edit_description("concurrent edit")
    assert generations[0].content < mutation_case.capture_generation().content
    with pytest.raises(StaleMutation):
        mutation_case.repository.atomic_with_receipt("next-stage", "acme", lambda graph, queue: None,
                                                     expected_generations=generations)
    _, tombstones, remaining = mutation_case.repository.atomic_with_receipt("delete", "acme",
        lambda graph, queue: SkillAdminService(store=graph).delete_skill("expenses", "acme"))
    assert remaining == ()
    assert tombstones[0].skill == mutation_case.skill and tombstones[0].content > generations[0].content
