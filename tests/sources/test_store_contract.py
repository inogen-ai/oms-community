from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from oms.adapters.memory.source_store import InMemorySourceRepository
from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.identity import SkillRef
from oms.sources.models import (
    Binding, Evidence, Generations, Lease, OriginRef, ProjectionPart,
    ResolvedRef, SnapshotRef, Source, SourceSnapshot,
)
from oms.sources.errors import SourceConflict, StaleMutation
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def source_repository(request):
    if request.param == "memory":
        return InMemorySourceRepository(InMemoryGraphStore())
    from oms.adapters.neo4j.source_store import Neo4jSourceRepository
    driver = request.getfixturevalue("critic_neo4j_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    repository = Neo4jSourceRepository(driver)
    repository.ensure_schema()
    return repository


def make_snapshot(tenant="acme"):
    origin = OriginRef(skill=SkillRef(tenant, "expenses"), origin_id="github-1",
                       kind="github", generation=1)
    return SourceSnapshot(ref=SnapshotRef(snapshot_id="initial", origin=origin),
                          revision="a" * 40, raw_frontmatter=Evidence(kind="unknown", policy_version="1"),
                          projection_version="1", policy_version="1",
                          effective_projection=(ProjectionPart(part_id="rule:legacy", kind="rule",
                              evidence=Evidence(kind="unknown", policy_version="1")),))


def test_snapshot_roundtrip_is_immutable_and_tenant_qualified(source_repository):
    snapshot = make_snapshot()
    other = make_snapshot("other")
    source_repository.put_snapshot(snapshot)
    source_repository.put_snapshot(other)
    loaded = source_repository.get_snapshot(snapshot.ref)
    assert loaded == snapshot
    assert loaded.effective_projection[0].evidence.kind == "unknown"
    replacement = SourceSnapshot.model_validate(snapshot.model_dump() | {"revision": "b" * 40})
    with pytest.raises(SourceConflict):
        source_repository.put_snapshot(replacement)
    assert source_repository.get_snapshot(other.ref) == other


def test_stale_generation_cannot_reset_deleted_skill_fence(source_repository):
    skill = SkillRef("acme", "expenses")
    old = source_repository.get_generations(skill)
    new = Generations(skill=skill, content=old.content + 1, binding=old.binding + 1)
    source_repository.advance_generations(old, new)
    with pytest.raises(StaleMutation):
        source_repository.compare_generations((old,))
    with pytest.raises(StaleMutation):
        source_repository.advance_generations(old, new)
    assert source_repository.get_generations(skill) == new


def test_one_repository_identity_is_reused_across_profiles(source_repository):
    first = Source(source_id="first", tenant_id="acme", canonical_url="https://github.com/example/skills")
    source_repository.put_source(first)
    with pytest.raises(SourceConflict, match="repository url already owned"):
        source_repository.put_source(Source(source_id="second", tenant_id="acme",
            canonical_url="https://github.com/Example/Skills.git", credential_profile_id="private-read"))
    assert source_repository.source_for_url("https://github.com/example/skills.git", tenant_id="acme") == first
    ref = ResolvedRef(canonical_url=first.canonical_url, kind="branch", name="main", commit="a" * 40)

    def bind(identifier):
        origin = OriginRef(skill=SkillRef("acme", identifier), origin_id=identifier, kind="github", generation=1)
        try:
            source_repository.put_binding(Binding(origin=origin, source_id="first", package_path="skills/a", ref=ref))
            return "bound"
        except SourceConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(bind, ("first", "second")))
    assert sorted(outcomes) == ["bound", "conflict"]


def test_confirmed_repository_aliases_share_one_identity(source_repository):
    original = Source(source_id="old", tenant_id="acme", canonical_url="https://github.com/Example/Old.git",
        confirmed_aliases=("https://github.com/example/new.git",))
    source_repository.put_source(original)
    with pytest.raises(SourceConflict, match="repository url already owned"):
        source_repository.put_source(Source(source_id="new", tenant_id="acme",
            canonical_url="https://github.com/example/new", credential_profile_id="another-profile"))
    assert source_repository.source_for_url("https://github.com/example/new", tenant_id="acme") == original
    assert source_repository.source_for_url("https://github.com/example/new", tenant_id="other") is None


def test_new_alias_cannot_take_another_bound_repository_identity(source_repository):
    for name in ("first", "second"):
        url = f"https://github.com/example/{name}"
        source_repository.put_source(Source(source_id=name, tenant_id="acme", canonical_url=url))
        source_repository.put_binding(Binding(origin=OriginRef(skill=SkillRef("acme", name),
            origin_id=name, kind="github", generation=1), source_id=name, package_path="skills/a",
            ref=ResolvedRef(canonical_url=url, kind="branch", name="main", commit="a" * 40)))
    original = source_repository.get_source("first", tenant_id="acme")
    with pytest.raises(SourceConflict):
        source_repository.put_source(Source.model_validate(original.model_dump() | {
            "confirmed_aliases": ("https://github.com/example/second",)}))
    assert source_repository.get_source("first", tenant_id="acme") == original


def test_expired_lease_replacement_fences_previous_owner(source_repository):
    now = datetime.now(timezone.utc)
    first = Lease(tenant_id="acme", resource_id="source-1", owner_id="one", run_id="run-one",
                  fencing_token=1, expires_at=now + timedelta(seconds=1))
    second = Lease(tenant_id="acme", resource_id="source-1", owner_id="two", run_id="run-two",
                   fencing_token=2, expires_at=now + timedelta(seconds=5))
    assert source_repository.claim_lease(first, now=now)
    assert not source_repository.claim_lease(second, now=now)
    assert source_repository.claim_lease(second, now=now + timedelta(seconds=2))
    source_repository.release_lease(first)
    assert not source_repository.claim_lease(first, now=now + timedelta(seconds=2))


def test_live_ownership_is_separate_from_snapshots_and_does_not_alias_entities(source_repository):
    from oms.sources.models import PartOwnership
    snapshot = make_snapshot()
    source_repository.put_snapshot(snapshot)
    skill = snapshot.ref.origin.skill
    assert source_repository.ownership_for_skill(skill) == ()
    before = source_repository.get_generations(skill)
    first = PartOwnership(skill=skill, part_id="section:same", origin_ids=("first",),
                           entity_ids=("section-one",), proof_digest="proof-one")
    second = PartOwnership(skill=skill, part_id="section:same", origin_ids=("second",),
                            entity_ids=("section-two",), proof_digest="proof-two")
    source_repository.put_ownership(first)
    source_repository.put_ownership(second)
    assert set(source_repository.ownership_for_skill(skill)) == {first, second}
    assert source_repository.get_generations(skill).content == before.content + 2
    source_repository.clear_ownership(skill, first.part_id, entity_ids=first.entity_ids)
    assert source_repository.ownership_for_skill(skill) == (second,)


def test_tenant_bindings_hide_those_at_or_below_the_retirement_fence(source_repository):
    skill = SkillRef("acme", "expenses")
    url = "https://github.com/example/skills"
    source_repository.put_source(Source(source_id="first", tenant_id="acme", canonical_url=url))
    ref = ResolvedRef(canonical_url=url, kind="branch", name="main", commit="a" * 40)

    def binding(generation, active=True):
        origin = OriginRef(skill=skill, origin_id="first", kind="github", generation=generation)
        return Binding(origin=origin, source_id="first", package_path="skills/a", ref=ref, active=active)

    def write(kind, value):
        # Rows are written below put_binding on purpose: the fence is what retire_skill leaves behind.
        source_repository._atomic("acme", lambda bound: bound._save("acme", kind, skill.skill_id, value))
    write("binding", binding(3))
    assert [row.origin.generation for row in source_repository.active_bindings_for_tenant(tenant_id="acme")] == [3]
    write("retirement", Generations(skill=skill, content=0, binding=3))
    assert source_repository.active_bindings_for_tenant(tenant_id="acme") == ()
    write("binding", binding(4))
    assert [row.origin.generation for row in source_repository.active_bindings_for_tenant(tenant_id="acme")] == [4]
    write("binding", binding(5, active=False))
    assert source_repository.active_bindings_for_tenant(tenant_id="acme") == ()
    assert source_repository.active_bindings_for_tenant(tenant_id="other") == ()
