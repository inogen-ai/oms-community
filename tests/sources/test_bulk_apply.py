from oms.sources.models import ApplyRequest, BulkApplyRequest


def bulk_request(candidate):
    update = candidate.update
    return BulkApplyRequest(updates=(ApplyRequest(update_id=update.update_id, skill=update.plan.skill,
                                                  fingerprint=update.plan.fingerprint),))


def test_bulk_excludes_executable_content_only_changes(source_world):
    candidate = source_world.open_file_update("bin/check", old_mode="100755", new_mode="100755", new_bytes=b"Updated script\n")
    result = source_world.service.bulk_apply(source_world.context, bulk_request(candidate), idempotency_key="bulk")
    assert not result.committed
    assert result.outcomes[0].state == "blocked"
    assert result.outcomes[0].code == "script_changes"
    assert source_world.baseline_revision() == "a" * 40


def test_bulk_applies_clean_card_once_and_recovers_receipt(source_world):
    candidate = source_world.open_update()
    request = bulk_request(candidate)
    result = source_world.service.bulk_apply(source_world.context, request, idempotency_key="bulk")
    assert result.committed
    assert source_world.description() == "Second description"
    before = source_world.snapshot()
    assert source_world.service.bulk_apply(source_world.context, request, idempotency_key="bulk") == result
    assert source_world.snapshot() == before


def test_bulk_records_partial_commit_when_later_skill_history_fails(source_world, monkeypatch):
    from oms.skills.history import SkillHistory
    updates = source_world.open_two_updates()
    original = SkillHistory.capture_required

    def capture(self, skill_id, *args, **kwargs):
        if skill_id == "other":
            raise OSError("other history failed")
        return original(self, skill_id, *args, **kwargs)
    monkeypatch.setattr(SkillHistory, "capture_required", capture)
    request = BulkApplyRequest(updates=tuple(ApplyRequest(update_id=update.update_id, skill=update.plan.skill,
                                                        fingerprint=update.plan.fingerprint) for update in updates))
    result = source_world.service.bulk_apply(source_world.context, request, idempotency_key="bulk")
    assert result.state == "failed"
    assert result.committed
    assert {outcome.skill.skill_id: outcome.state for outcome in result.outcomes} == {"expenses": "applied", "other": "failed"}
    assert source_world.description() == "Second description"
    assert source_world.store.get_skill("other", tenant_id="acme").description == "First description"
    assert source_world.service.get_operation(source_world.context, result.operation_id) == result


def test_bulk_skips_unadmitted_skill_without_blocking_admitted_skill(source_world, monkeypatch):
    updates = source_world.open_two_updates()

    def admit(action, context, refs):
        if any(ref.skill_id == "other" for ref in refs):
            raise PermissionError("domain revoked")
    monkeypatch.setattr(source_world.policy, "admit", admit)
    request = BulkApplyRequest(updates=tuple(ApplyRequest(update_id=update.update_id, skill=update.plan.skill,
                                                        fingerprint=update.plan.fingerprint) for update in updates))
    result = source_world.service.bulk_apply(source_world.context, request, idempotency_key="bulk")
    assert result.committed
    assert tuple(outcome.skill.skill_id for outcome in result.outcomes) == ("expenses",)
    parent = source_world.sources.get_operation(result.operation_id, tenant_id="acme")
    assert tuple(member.skill.skill_id for member in parent.batch_members) == ("expenses",)
    assert source_world.service.get_operation(source_world.context, result.operation_id) == result
    assert source_world.service.bulk_apply(source_world.context, request, idempotency_key="bulk") == result
    assert source_world.description() == "Second description"
    assert source_world.store.get_skill("other", tenant_id="acme").description == "First description"


def test_missing_bulk_primary_has_no_outcome_or_receipt_member(source_world):
    from oms.domain.identity import SkillRef
    candidate = source_world.open_update()
    valid = bulk_request(candidate).updates[0]
    missing = valid.model_copy(update={"skill": SkillRef("acme", "missing"), "update_id": "missing"})
    result = source_world.service.bulk_apply(source_world.context, BulkApplyRequest(updates=(valid, missing)), idempotency_key="bulk")
    assert tuple(outcome.skill.skill_id for outcome in result.outcomes) == ("expenses",)
    assert tuple(member.skill.skill_id for member in source_world.sources.get_operation(result.operation_id, tenant_id="acme").batch_members) == ("expenses",)
