"""A HELD skill is stored but never published and never served to agents."""
import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType, SkillStatus
from oms.publish.catalogue import SkillCatalogue, SkillNotFound
from oms.publish.publisher import Publisher

TENANT = "acme"


def _store(status: SkillStatus) -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    store.upsert_skill(Skill(id="held-one", name="Held One", description="Guidance.",
                             domain="finance", tenant_id=TENANT, status=status))
    store.upsert_rule(Rule(id="held-one-rule", body="Follow the checklist.",
                           tenant_id=TENANT))
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="held-one-rule",
                           to_id="held-one"), tenant_id=TENANT)
    return store


def _ids(publisher: Publisher) -> list[str]:
    return [skill.id for skill, _rendered in publisher.publishable_skills(TENANT)]


def test_held_skill_is_not_publishable_but_active_is():
    assert _ids(Publisher(_store(SkillStatus.ACTIVE))) == ["held-one"]
    assert _ids(Publisher(_store(SkillStatus.HELD))) == []


def test_held_skill_is_not_listed_or_served():
    store = _store(SkillStatus.HELD)
    catalogue = SkillCatalogue(store, Publisher(store))
    assert catalogue.list_skills(TENANT) == []
    with pytest.raises(SkillNotFound) as held_error:
        catalogue.query_skill(TENANT, "held-one")
    with pytest.raises(SkillNotFound) as unknown_error:
        catalogue.query_skill(TENANT, "unknown-one")
    assert str(held_error.value).replace("held-one", "X") == \
        str(unknown_error.value).replace("unknown-one", "X")
    with pytest.raises(SkillNotFound):
        catalogue.query_skill(TENANT, "Held One")


def test_active_skill_is_listed_and_served():
    store = _store(SkillStatus.ACTIVE)
    catalogue = SkillCatalogue(store, Publisher(store))
    assert [s.id for s in catalogue.list_skills(TENANT)] == ["held-one"]
    assert catalogue.query_skill(TENANT, "held-one").id == "held-one"


def test_memory_store_round_trips_held():
    store = _store(SkillStatus.HELD)
    assert store.skills_for_tenant(TENANT)[0].status is SkillStatus.HELD
