"""A held skill keeps its review card until the card is applied or the skill is deleted.

Unlinking the skill, or removing its source, would close that card and leave an
empty held skill that nothing can make live. Both are refused while the skill is
held, and the binding read names the open card so the skill's page can link to it.
"""
import pytest

from oms.domain.identity import SkillRef
from oms.sources.errors import SourceConflict
from oms.sources.models import RemoveSourceRequest, UnlinkRequest
from tests.sources.test_api import source_api  # noqa: F401
from tests.sources.test_held_install_card import INVOICES, _held_card


EXPENSES = SkillRef("acme", "expenses")


def test_unlinking_a_held_skill_is_refused_and_keeps_its_card(source_world):
    world = source_world
    update = _held_card(world)
    guard = world.sources.get_generations(INVOICES)
    request = UnlinkRequest(skill=INVOICES, expected_content_generation=guard.content,
                            expected_binding_generation=guard.binding)
    with pytest.raises(SourceConflict) as refused:
        world.service.unlink(world.context, request, idempotency_key="unlink-held")
    assert refused.value.code == "held_install_requires_apply"
    assert world.sources.open_update(INVOICES) == update
    assert world.sources.get_binding(INVOICES).active


def test_a_held_skill_with_no_open_card_is_told_to_install_again(source_world):
    # Undoing the apply that made a held install live leaves it held with no card (undo.restore_status).
    world = source_world
    update = _held_card(world)
    world.sources.put_update(update.model_copy(update={"status": "closed"}))
    guard = world.sources.get_generations(INVOICES)
    request = UnlinkRequest(skill=INVOICES, expected_content_generation=guard.content,
                            expected_binding_generation=guard.binding)
    with pytest.raises(SourceConflict) as refused:
        world.service.unlink(world.context, request, idempotency_key="unlink-held-no-card")
    assert "no open review card" in str(refused.value.args)
    assert "install it again" in str(refused.value.args)


def test_removing_a_source_with_a_held_skill_is_refused(source_world):
    world = source_world
    update = _held_card(world)
    source = world.sources.get_source(world.sources.get_binding(INVOICES).source_id, tenant_id="acme")
    request = RemoveSourceRequest(source_id=source.source_id, expected_source_generation=source.generation,
                                  expected_generations=world.sources.generations_for_tenant(tenant_id="acme"))
    with pytest.raises(SourceConflict) as refused:
        world.service.remove_source(world.context, request, idempotency_key="remove-held")
    assert refused.value.code == "held_install_requires_apply"
    assert world.sources.open_update(INVOICES) == update
    assert world.sources.get_binding(INVOICES).active and world.sources.get_binding(EXPENSES).active


def test_the_binding_read_names_the_open_card(source_api):
    client, world, _ = source_api
    update = _held_card(world)
    assert client.get("/api/skills/invoices/source-binding").json()["open_update_id"] == update.update_id
    assert client.get("/api/skills/expenses/source-binding").json()["open_update_id"] is None
