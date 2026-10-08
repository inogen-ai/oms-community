import pytest

from oms.sources.errors import SourceForbidden
from oms.sources.models import AutomationRequest, ScheduleRequest


def test_enable_automation_requires_enable_authority_and_disable_remains_separate(source_world, monkeypatch):
    source_world.install()

    def admit(action, context, refs):
        if action == "enable_automation":
            raise SourceForbidden("administrator_required")
    monkeypatch.setattr(source_world.policy, "admit", admit)
    guard = source_world.sources.get_generations(source_world.skill)
    request = AutomationRequest(skill=source_world.skill, expected_content_generation=guard.content, expected_binding_generation=guard.binding, enabled=True)
    with pytest.raises(SourceForbidden):
        source_world.service.configure_automation(source_world.context, request, idempotency_key="enable")
    assert not source_world.sources.get_binding(source_world.skill).automatic_apply
    source_world.service.configure_automation(source_world.context, request.model_copy(update={"enabled": False}), idempotency_key="disable")


def test_source_schedule_accepts_the_typed_request_and_preserves_content(source_world):
    source_world.install()
    binding = source_world.sources.get_binding(source_world.skill)
    source = source_world.sources.get_source(binding.source_id, tenant_id="acme")
    request = ScheduleRequest(source_id=source.source_id, expected_source_generation=source.generation,
        expected_generations=(source_world.sources.get_generations(source_world.skill),), enabled=True)
    result = source_world.service.configure_schedule(source_world.context, request, idempotency_key="schedule")
    assert source_world.sources.get_source(source.source_id, tenant_id="acme").scheduled
    assert not result.committed
    assert source_world.description() == "First description"
