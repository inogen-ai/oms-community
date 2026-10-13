"""A new local package that may not go live as it is waits on a review card (spec 2.1).

The upload creates the skill empty and marked held, stores the local stream with no
baseline, and stages a card, as a held repository install does. Applying the card
makes the skill live; deleting the skill closes the card and a fresh upload works.
"""
import pytest

from oms.domain.identity import SkillRef
from oms.sources.errors import SourceConflict
from oms.sources.models import ApplyRequest, DraftChoice, LocalBatchRequest, SaveDraftRequest, UpdateRequest
from oms.sources.review import part_fingerprint
from tests.sources.test_community_safety import community_world  # noqa: F401
from tests.sources.test_local_imports import local_request


ANOTHER = SkillRef("acme", "another")


def _request(update):
    return UpdateRequest(update_id=update.update_id, skill=update.plan.skill, fingerprint=update.plan.fingerprint)


def _held_upload(world, key="held-upload", **options):
    world.policy.held = True
    return world.service.submit_local(world.context, local_request(world, create=True, **options),
                                      idempotency_key=key)


def _choose_every_conflict(world, update, key, kept=frozenset()):
    """Take upstream wherever the card allows it; keep the empty skill's value otherwise and for `kept`."""
    changes = {change.part_id: change for change in update.plan.changes}
    choices = []
    for conflict in update.plan.conflicts:
        allowed = conflict.allowed_choices
        if conflict.part_id in kept:
            choice = "keep_oms"
        elif "use_upstream" in allowed and changes[conflict.part_id].incoming.kind == "known":
            choice = "use_upstream"
        else:
            choice = "keep_oms"
        choices.append(DraftChoice(part_id=conflict.part_id, choice=choice,
                                   part_fingerprint=part_fingerprint(update, conflict.part_id)))
    world.service.save_draft(world.context, SaveDraftRequest(**_request(update).model_dump(), choices=tuple(choices)),
                             idempotency_key=key)
    return world.sources.get_update(update.update_id, tenant_id="acme")


def _apply(world, update, key="apply-held-local"):
    return world.service.apply(world.context, ApplyRequest(**_request(update).model_dump()), idempotency_key=key)


def _release_and_apply(world, suffix):
    """Take everything on the open card, release the hold, recheck and apply."""
    update = _choose_every_conflict(world, world.sources.open_update(world.skill), "draft-" + suffix)
    world.policy.held = False
    world.service.recheck(world.context, _request(update), idempotency_key="recheck-" + suffix)
    return _apply(world, world.sources.open_update(world.skill), key="apply-" + suffix)


def test_a_held_new_local_package_becomes_a_held_skill_with_a_card(source_world):
    world = source_world
    result = _held_upload(world)
    assert result.state == "awaiting_review" and result.committed
    update = world.sources.open_update(world.skill)
    assert update is not None
    outcome, = result.outcomes
    assert (outcome.state, outcome.code) == ("awaiting_review", "safety_review_required")
    assert outcome.detail.startswith("Held")
    assert outcome.update_id == update.update_id
    skill = world.store.get_skill("expenses", tenant_id="acme")
    assert skill.status.value == "held"
    assert (skill.name, skill.domain, skill.description) == ("expenses", "finance", "")
    assert not world.store.rules_for_skill("expenses", tenant_id="acme")
    assert not world.store.sections_for_skill("expenses", tenant_id="acme")
    stream = world.sources.get_local_stream(world.skill)
    assert stream is not None and stream.baseline is None
    assert (stream.transport, stream.filename) == ("zip", "expenses.zip")
    # The skill records its upload origin, as a directly created local skill does.
    assert skill.import_source_ref == "source:" + stream.origin.origin_id
    assert world.sources.get_binding(world.skill) is None
    assert update.plan.base is None
    assert update.plan.origin == stream.origin
    # As a held repository install does, the held skill is recorded in history and
    # in the operation's event.
    versions = world.store.skill_versions("acme", "expenses")
    assert [row.source_operation_id for row in versions] == [result.operation_id]
    event = world.sources.get_event(result.operation_id + ":local-installed", tenant_id="acme")
    assert event is not None and event.action == "local_import" and event.skills == (world.skill,)


def test_the_held_local_card_fingerprint_matches_the_skill_as_committed(source_world):
    # The card must be applicable, so its fingerprint pins the held skill as the
    # commit left it, not the absent skill the upload saw.
    from oms.sources.apply import capture_apply
    from oms.sources.local_projection import project_local
    world = source_world
    _held_upload(world)
    update = world.sources.open_update(world.skill)
    generations = world.sources.get_generations(world.skill)
    assert update.plan.fingerprint.content_generation == generations.content
    assert update.plan.fingerprint.binding_generation == generations.binding
    local = project_local(world.store, world.skill, baseline=None, content_generation=generations.content,
        ownership=world.sources.ownership_for_skill(world.skill), policy_version=world.service.builder.policy_version)
    assert update.plan.fingerprint.local_digest == local.digest
    request = ApplyRequest(update_id=update.update_id, skill=world.skill, fingerprint=update.plan.fingerprint)
    captured = world.service._read(world.context, "apply", (world.skill,),
                                   lambda bound: capture_apply(world.service, world.context, request, bound))
    assert captured[0] == update


def test_applying_the_held_local_card_makes_the_skill_live_with_a_baseline(source_world):
    world = source_world
    _held_upload(world)
    update = _choose_every_conflict(world, world.sources.open_update(world.skill), "draft-held-local")
    world.policy.held = False
    world.service.recheck(world.context, _request(update), idempotency_key="recheck-held-local")
    update = world.sources.open_update(world.skill)
    assert not [conflict for conflict in update.plan.conflicts
                if conflict.part_id not in {choice.part_id for choice in update.drafts}]
    result = _apply(world, update)
    assert result.state == "complete" and result.committed
    skill = world.store.get_skill("expenses", tenant_id="acme")
    assert skill.status.value == "active"
    assert skill.description == "Local description"
    assert [rule.body for rule in world.store.rules_for_skill("expenses", tenant_id="acme")] == ["Keep receipts."]
    stream = world.sources.get_local_stream(world.skill)
    assert stream.baseline is not None
    assert skill.import_source_ref == "source:" + stream.origin.origin_id
    assert world.sources.get_binding(world.skill) is None
    assert world.sources.open_update(world.skill) is None
    assert not [item for item in world.queue.pending("acme") if item.subject_id == update.update_id]
    versions = world.store.skill_versions("acme", "expenses")
    assert [row.source_operation_id for row in versions].count(result.operation_id) == 1


def test_undoing_the_apply_of_a_held_local_install_holds_the_skill_again(source_world):
    from oms.publish.catalogue import SkillCatalogue
    from oms.publish.publisher import Publisher
    from oms.sources.models import UndoRequest
    world = source_world
    _held_upload(world)
    # The document mode is kept: the empty held skill has none, so its prior
    # value is unknown, and undo refuses to restore a write it cannot screen
    # (`write_evidence_unavailable`), whatever the status.
    update = _choose_every_conflict(world, world.sources.open_update(world.skill), "draft-held-local",
                                    kept={"field:document_mode"})
    world.policy.held = False
    world.service.recheck(world.context, _request(update), idempotency_key="recheck-held-local")
    result = _apply(world, world.sources.open_update(world.skill))
    assert result.state == "complete" and result.committed
    receipt = world.sources.get_undo(result.operation_id + ":undo", tenant_id="acme")
    assert receipt.previous_skill_status.value == "held"
    publisher = Publisher(world.store)
    assert "expenses" in [skill.id for skill, _ in publisher.publishable_skills("acme")]
    request = UndoRequest(undo_id=receipt.undo_id, skill=world.skill, expected_generations=receipt.expected_generations,
                          policy_version=receipt.policy_version)
    assert world.service.undo_status(world.context, receipt.undo_id).available
    undone = world.service.undo(world.context, request, idempotency_key="undo-held-local")
    assert undone.committed
    skill = world.store.get_skill("expenses", tenant_id="acme")
    assert skill.status.value == "held"
    assert not world.store.rules_for_skill("expenses", tenant_id="acme")
    assert "expenses" not in [skill.id for skill, _ in publisher.publishable_skills("acme")]
    assert "expenses" not in [row.id for row in SkillCatalogue(world.store, publisher).list_skills("acme")]
    stream = world.sources.get_local_stream(world.skill)
    assert stream is not None and stream.baseline is None
    assert world.sources.get_binding(world.skill) is None
    # Undo does not reopen the card it reverted; the person can delete the
    # skill and upload it again.
    assert world.sources.open_update(world.skill) is None


@pytest.mark.parametrize("held_again", [False, True], ids=["passes", "held-again"])
def test_deleting_a_held_local_skill_and_uploading_again_works(source_world, held_again):
    from oms.skills.service import SkillAdminService
    world = source_world
    _held_upload(world)
    update = world.sources.open_update(world.skill)
    SkillAdminService(store=world.store, repository=world.repository).delete_skill("expenses", "acme",
                                                                                     actor="reviewer")
    assert world.store.get_skill("expenses", tenant_id="acme") is None
    assert world.sources.open_update(world.skill) is None
    assert world.sources.get_update(update.update_id, tenant_id="acme").status == "closed"
    assert not [item for item in world.queue.pending("acme") if item.subject_id == update.update_id]
    world.policy.held = held_again
    result = world.service.submit_local(world.context, local_request(world, create=True),
                                        idempotency_key="upload-again")
    skill = world.store.get_skill("expenses", tenant_id="acme")
    stream = world.sources.get_local_stream(world.skill)
    if held_again:
        assert result.state == "awaiting_review" and result.committed
        assert result.outcomes[0].state == "awaiting_review"
        assert skill.status.value == "held"
        card = world.sources.open_update(world.skill)
        assert card is not None and card.update_id != update.update_id
        assert result.outcomes[0].update_id == card.update_id
        assert stream.baseline is None and card.plan.origin == stream.origin
        # The new card can be applied like the first.
        applied = _release_and_apply(world, "held-again")
        assert applied.state == "complete" and applied.committed
        assert world.store.get_skill("expenses", tenant_id="acme").status.value == "active"
        assert world.sources.get_local_stream(world.skill).baseline is not None
        assert world.sources.open_update(world.skill) is None
    else:
        assert result.state == "complete" and result.committed
        assert result.outcomes[0].state == "applied"
        assert skill.status.value == "active"
        assert world.store.rules_for_skill("expenses", tenant_id="acme")
        assert world.sources.open_update(world.skill) is None
        assert stream.baseline is not None


def test_a_second_upload_while_the_card_is_open_is_refused_and_changes_nothing(source_world):
    world = source_world
    _held_upload(world)
    card = world.sources.open_update(world.skill)
    stream = world.sources.get_local_stream(world.skill)
    before = world.snapshot()
    # Into the held skill: the existing collision refusal.
    result = world.service.submit_local(world.context, local_request(world, description="Second upload"),
                                        idempotency_key="second-upload")
    assert result.state == "blocked" and not result.committed
    assert (result.outcomes[0].code, result.outcomes[0].update_id) == ("another_update_open", card.update_id)
    assert world.snapshot() == before
    # As a new skill again: the name is taken by the held skill.
    with pytest.raises(SourceConflict) as refused:
        world.service.submit_local(world.context, local_request(world, create=True, description="Second upload"),
                                   idempotency_key="second-create")
    assert refused.value.code == "explicit_local_target_required"
    assert world.snapshot() == before
    assert world.sources.open_update(world.skill) == card
    assert world.sources.get_local_stream(world.skill) == stream


def test_replaying_a_held_create_returns_the_same_result_and_changes_nothing(source_world):
    world = source_world
    world.policy.held = True
    request = local_request(world, create=True)
    first = world.service.submit_local(world.context, request, idempotency_key="held-replay")
    assert first.state == "awaiting_review"
    card = world.sources.open_update(world.skill)
    stream = world.sources.get_local_stream(world.skill)
    before = world.snapshot()
    assert world.service.submit_local(world.context, request, idempotency_key="held-replay") == first
    assert world.snapshot() == before
    assert world.sources.open_update(world.skill) == card
    assert world.sources.get_local_stream(world.skill) == stream


@pytest.mark.parametrize("route", ["direct", "held-then-applied"])
def test_a_new_local_skill_can_be_linked_to_a_repository(source_world, route):
    # A first link needs the skill's single import origin, which a held local
    # skill records when it is created, as a directly created one does.
    from oms.sources.models import LinkRequest, Source
    world = source_world
    if route == "direct":
        created = world.service.submit_local(world.context, local_request(world, create=True),
                                             idempotency_key="direct-create")
        assert created.state == "complete"
    else:
        _held_upload(world)
        assert _release_and_apply(world, "before-link").state == "complete"
    world.sources.put_source(Source(source_id="target", tenant_id="acme",
                                    canonical_url=world.incoming.resolved_ref.canonical_url))
    guard = world.sources.get_generations(world.skill)
    result = world.service.link(world.context, LinkRequest(skill=world.skill, source_id="target", package_path="",
        ref=world.incoming.resolved_ref, expected_content_generation=guard.content,
        expected_binding_generation=guard.binding), idempotency_key="link")
    assert result.state == "awaiting_review"
    binding = world.sources.get_binding(world.skill)
    assert binding is not None and binding.active and binding.baseline is None and binding.first_reconciliation


def test_an_unapproved_held_create_still_writes_nothing(source_world):
    world = source_world
    world.policy.held = True
    result = world.service.submit_local(world.context, local_request(world, create=True, approve=False),
                                        idempotency_key="unapproved")
    assert result.state == "blocked" and not result.committed
    outcome, = result.outcomes
    assert (outcome.state, outcome.code) == ("blocked", "approval_or_checks_required")
    assert world.store.get_skill("expenses", tenant_id="acme") is None
    assert world.sources.get_local_stream(world.skill) is None
    assert world.sources.open_update(world.skill) is None
    assert world.store.skill_versions("acme", "expenses") == []


def test_a_batch_holds_one_new_package_and_installs_the_other(source_world):
    world = source_world
    first = local_request(world, create=True)
    second = first.model_copy(update={"skill": ANOTHER, "local_name": "another"})
    world.policy.held_skills = {"another"}
    result = world.service.submit_local_batch(world.context, LocalBatchRequest(requests=(first, second)),
                                              idempotency_key="mixed-new")
    assert result.state == "awaiting_review" and result.committed
    assert [(row.skill.skill_id, row.state) for row in result.outcomes] == [
        ("expenses", "applied"), ("another", "awaiting_review")]
    card = world.sources.open_update(ANOTHER)
    assert card is not None and result.outcomes[1].update_id == card.update_id
    held = world.store.get_skill("another", tenant_id="acme")
    assert held.status.value == "held" and not world.store.rules_for_skill("another", tenant_id="acme")
    installed = world.store.get_skill("expenses", tenant_id="acme")
    assert installed.status.value == "active" and world.store.rules_for_skill("expenses", tenant_id="acme")
    assert world.sources.open_update(world.skill) is None


def test_a_real_email_in_a_new_package_is_held_as_personal_data_only(community_world):
    # The real safety screen, not the fixture policy: a sample address is held as
    # personal data alone, so the reviewer may take it with an acknowledgement.
    from oms.sources.triage import review_parts
    world = community_world
    request = local_request(world, create=True, description="Send expense questions to jane@company.com.")
    result = world.service.submit_local(world.context, request, idempotency_key="email-upload")
    assert result.state == "awaiting_review" and result.committed
    assert result.outcomes[0].code == "safety_review_required"
    assert "email" in result.outcomes[0].detail
    assert "jane@company.com" not in result.outcomes[0].detail
    assert world.store.get_skill("expenses", tenant_id="acme").status.value == "held"
    update = world.sources.open_update(world.skill)
    parts = review_parts(update.plan, held_install=True)
    flagged = [row for row in parts.values() if row.overridable]
    assert flagged and all(row.overridable == ("email",) for row in flagged)
    taken = {row.part_id for row in flagged}
    choices = tuple(DraftChoice(part_id=row.part_id, choice="use_upstream",
                                part_fingerprint=part_fingerprint(update, row.part_id),
                                acknowledged=("email",) if row.part_id in taken else ())
                    for row in update.plan.conflicts if "use_upstream" in row.allowed_choices)
    world.service.save_draft(world.context, SaveDraftRequest(**_request(update).model_dump(), choices=choices),
                             idempotency_key="take-email")
    update = world.sources.get_update(update.update_id, tenant_id="acme")
    applied = _apply(world, update, key="apply-email")
    assert applied.state == "complete" and applied.committed
    skill = world.store.get_skill("expenses", tenant_id="acme")
    assert skill.status.value == "active"
    assert skill.description == "Send expense questions to jane@company.com."
    assert world.sources.get_local_stream(world.skill).baseline is not None
    assert world.sources.open_update(world.skill) is None
