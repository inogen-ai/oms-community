"""A fresh installation request can return its visible repository/path binding."""
from datetime import timedelta

import pytest

from oms.domain.models import Skill
from oms.sources.errors import SourceConflict, SourceNotFound
from oms.sources.models import InstallRequest, InstallSelection


def _repeat(world, *, name="expenses", discovery="discovery", key="repeat"):
    return world.service.install(world.context, InstallRequest(discovery_id=discovery,
        selections=(InstallSelection(package_path="", local_name=name, domain="finance"),)), idempotency_key=key)


@pytest.mark.parametrize("name", ["expenses", "new-local-name"])
def test_repeat_install_returns_visible_binding_without_writes(source_world, name):
    world = source_world
    world.install()
    before = world.snapshot()
    ownership = world.sources.ownership_for_skill(world.skill)
    lineage = {rule.id: tuple(world.store.lineage(rule.id)) for rule in world.store.rules_for_skill("expenses", tenant_id="acme")}
    result = _repeat(world, name=name)
    assert result.state == "complete"
    assert not result.committed
    assert len(result.outcomes) == 1
    assert result.outcomes[0].skill == world.skill
    assert result.outcomes[0].state == "unchanged"
    assert result.outcomes[0].code == "already_installed"
    assert world.snapshot() == before
    assert world.sources.ownership_for_skill(world.skill) == ownership
    assert lineage == {rule.id: tuple(world.store.lineage(rule.id)) for rule in world.store.rules_for_skill("expenses", tenant_id="acme")}
    assert [skill.id for skill in world.store.skills_for_tenant("acme")] == ["expenses"]
    assert _repeat(world, name=name) == result


def test_repeat_install_across_ref_and_profile_keeps_original_binding(source_world):
    world = source_world
    world.install()
    before = world.snapshot()
    discovery = world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    ref = discovery.resolved_ref.model_copy(update={"kind": "tag", "name": "release", "commit": "c" * 40})
    package = world.package("c", "Different branch content").model_copy(update={"resolved_ref": ref})
    world.discoveries.save(discovery.model_copy(update={"discovery_id": "other-ref", "credential_profile_id": "other-profile",
        "resolved_ref": ref, "acquired_packages": (package,), "expires_at": world.now + timedelta(hours=1)}))
    result = _repeat(world, name="another-name", discovery="other-ref")
    assert result.outcomes[0].skill == world.skill
    assert result.outcomes[0].state == "unchanged"
    assert world.snapshot() == before


def test_repeat_install_does_not_disclose_inaccessible_binding(source_world, monkeypatch):
    world = source_world
    world.install()
    before = world.snapshot()

    def deny(action, context, skills):
        if world.skill in skills:
            raise PermissionError("secret skill name and domain")
    monkeypatch.setattr(world.policy, "admit", deny)
    with pytest.raises(SourceNotFound, match="installation unavailable"):
        _repeat(world, name="new-local-name")
    assert world.snapshot() == before


def test_unrelated_name_collision_still_refuses_install(source_world):
    world = source_world
    world.store.upsert_skill(Skill(id="expenses", name="Existing local skill", description="Keep me",
        domain="finance", tenant_id="acme"))
    with pytest.raises(SourceConflict, match="skill already exists"):
        _repeat(world)
    assert world.store.get_skill("expenses", tenant_id="acme").description == "Keep me"
    assert world.sources.get_binding(world.skill) is None


def test_repeat_install_does_not_apply_newly_held_incoming_content(source_world):
    world = source_world
    world.install()
    before = world.snapshot()
    world.policy.held = True
    result = _repeat(world)
    assert result.outcomes[0].state == "unchanged"
    assert not result.committed
    assert world.snapshot() == before


def test_repeat_and_new_package_install_return_ordered_independent_outcomes(source_world):
    from oms.sources.models import DiscoveredPackage
    world = source_world
    world.install()
    original = world.snapshot()
    discovery = world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    second = world.package("a", "Second package").model_copy(update={"package_path": "second"})
    world.discoveries.save(discovery.model_copy(update={"discovery_id": "mixed", "packages": (
        *discovery.packages, DiscoveredPackage(path="second", valid=True)),
        "acquired_packages": (*discovery.acquired_packages, second)}))
    result = world.service.install(world.context, InstallRequest(discovery_id="mixed", selections=(
        InstallSelection(package_path="", local_name="unused-alias", domain="finance"),
        InstallSelection(package_path="second", local_name="second", domain="finance"))), idempotency_key="mixed")
    assert [(row.skill.skill_id, row.state) for row in result.outcomes] == [
        ("expenses", "unchanged"), ("second", "applied")]
    assert result.committed
    assert world.snapshot() == original
    assert world.store.get_skill("unused-alias", tenant_id="acme") is None
    assert world.store.get_skill("second", tenant_id="acme").description == "Second package"


def test_mixed_install_rejects_new_name_colliding_with_returned_existing_skill(source_world):
    from oms.sources.models import DiscoveredPackage
    world = source_world
    world.install()
    original = world.snapshot()
    discovery = world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    second = world.package("a", "Second package").model_copy(update={"package_path": "second"})
    world.discoveries.save(discovery.model_copy(update={"discovery_id": "mixed-collision", "packages": (
        *discovery.packages, DiscoveredPackage(path="second", valid=True)),
        "acquired_packages": (*discovery.acquired_packages, second)}))
    with pytest.raises(SourceConflict, match="skill already exists"):
        world.service.install(world.context, InstallRequest(discovery_id="mixed-collision", selections=(
            InstallSelection(package_path="", local_name="unused-alias", domain="finance"),
            InstallSelection(package_path="second", local_name="expenses", domain="finance"))), idempotency_key="mixed-collision")
    assert world.snapshot() == original
