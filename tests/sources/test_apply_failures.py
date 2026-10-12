import pytest


def test_history_failure_rolls_back_entire_update(source_world):
    candidate = source_world.open_update()
    before = source_world.snapshot()
    source_world.fail_required_history()
    with pytest.raises(OSError):
        source_world.apply(candidate)
    assert source_world.snapshot() == before


def test_required_history_retention_read_failure_rolls_back_update(source_world):
    candidate = source_world.open_update()
    before = source_world.snapshot()
    original = source_world.service.factory_for

    def factory_for(*args):
        factory = original(*args)

        def bind(graph, reviews):
            bound = factory(graph, reviews)

            def unavailable(tenant):
                raise OSError("retention settings unavailable")
            bound.history._keep = unavailable
            return bound
        return bind
    source_world.service.factory_for = factory_for
    with pytest.raises(OSError, match="retention settings unavailable"):
        source_world.apply(candidate)
    assert source_world.snapshot() == before


def test_check_cannot_resurrect_deleted_binding_after_fetch(source_world):
    from oms.skills.service import SkillAdminService
    from oms.sources.errors import StaleMutation, SourceNotFound
    source_world.install()

    def delete():
        SkillAdminService(store=source_world.store, repository=source_world.repository).delete_skill("expenses", "acme")
    source_world.reader.before_read = delete
    with pytest.raises((StaleMutation, SourceNotFound)):
        source_world.check()
    assert source_world.store.get_skill("expenses", tenant_id="acme") is None
    assert source_world.sources.open_update(source_world.skill) is None


def test_revoked_admission_during_fetch_prevents_candidate_commit(source_world):
    source_world.install()
    source_world.reader.before_read = lambda: setattr(source_world.case, "denied", True)
    with pytest.raises(PermissionError):
        source_world.check()
    assert source_world.sources.open_update(source_world.skill) is None


def test_polling_expired_operation_never_fetches(source_world):
    from datetime import timedelta
    from oms.sources import operations
    from oms.sources.models import CheckRequest
    source_world.install()
    binding = source_world.sources.get_binding(source_world.skill)
    request = CheckRequest(source_id=binding.source_id, expected_source_generation=0, skills=(source_world.skill,))
    operation, _ = operations.begin(source_world.sources, source_world.context, "check", request,
                                    "interrupted", source_world.now)
    source_world.now += timedelta(minutes=11)
    result = source_world.service.get_operation(source_world.context, operation.operation_id)
    assert result.state == "failed"
    assert not result.committed
    assert source_world.reader.reads == 0


@pytest.mark.parametrize("boundary", ["graph", "queue", "source", "audit"])
def test_collaborator_failure_rolls_back_all_live_writes(source_world, monkeypatch, boundary):
    from oms.sources.repository import SourceRecords
    candidate = source_world.open_update()
    before = source_world.snapshot()
    targets = {
        "graph": (type(source_world.store), "upsert_skill"),
        "queue": (type(source_world.queue), "resolve"),
        "source": (SourceRecords, "put_binding"),
        "audit": (SourceRecords, "append_event"),
    }
    owner, name = targets[boundary]
    original = getattr(owner, name)

    def fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("injected collaborator failure")
    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(OSError):
        source_world.apply(candidate)
    assert source_world.snapshot() == before
    operation = source_world.sources.operation_for_key("apply", tenant_id="acme", actor_id="reviewer")
    assert operation.result.state == "failed"
    assert not operation.result.committed


def test_recreated_facade_recovers_result_without_fetching(source_world):
    from oms.sources.service import SourceService
    result = source_world.install()
    old = source_world.service
    recreated = SourceService(old.repository, old.factory_for, old.reader, old.builder,
                              old.policy, old.discoveries, clock=old.clock)
    assert recreated.get_operation(source_world.context, result.operation_id) == result
    assert source_world.reader.reads == 0


@pytest.mark.parametrize("action", ["install", "check", "apply"])
def test_policy_change_during_screening_refuses_commit(source_world, monkeypatch, action):
    from oms.sources.errors import StaleMutation
    candidate = source_world.open_update() if action == "apply" else None
    if action == "check":
        source_world.install()
    before = source_world.snapshot()
    original = source_world.policy.screen

    def screen(snapshot, local):
        result = original(snapshot, local)
        source_world.policy.policy_generation += 1
        return result
    monkeypatch.setattr(source_world.policy, "screen", screen)
    with pytest.raises(StaleMutation):
        source_world.apply(candidate) if action == "apply" else getattr(source_world, action)()
    assert source_world.snapshot() == before


def test_policy_change_after_check_invalidates_card(source_world):
    from oms.sources.errors import StaleMutation
    candidate = source_world.open_update()
    before = source_world.snapshot()
    source_world.policy.policy_generation += 1
    with pytest.raises(StaleMutation):
        source_world.apply(candidate)
    assert source_world.snapshot() == before
