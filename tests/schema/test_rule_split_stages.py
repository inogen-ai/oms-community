"""The staged split (spec 8.1): fenced, splitting, verified, ready; resumable, idempotent."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from neo4j import Driver

from oms.domain.types import SkillVersionCause
from oms.schema import metadata as schema_metadata
from oms.schema.metadata import SCHEMA_VERSION, SchemaCompatibilityError, SchemaManager
from oms.schema.migrations import rule_split
from tests.legacy_rules import share_rule
from tests.schema import split_world
from tests.schema.test_critic_schema import _metadata, _snapshot, schema_driver  # noqa: F401

pytestmark = pytest.mark.integration


class Stop(Exception):
    """A process stopped part way (a crash, a redeploy)."""


def _split(driver):
    return SchemaManager(driver).split_rules(old_writers_stopped=True)


def _expected(driver):
    split_world.build(driver)
    _split(driver)
    return split_world.normalised(driver)


def test_schema_3_is_the_target():
    assert SCHEMA_VERSION == 3


def test_writers_refuse_an_unsplit_schema_2_graph_before_any_write(schema_driver):
    split_world.build(schema_driver)
    before = _snapshot(schema_driver)
    with pytest.raises(SchemaCompatibilityError, match="rule split maintenance"):
        SchemaManager(schema_driver).initialise(edition="community")
    assert _snapshot(schema_driver) == before


@pytest.mark.parametrize("stage", rule_split.RULE_SPLIT_STAGES)
def test_writers_refuse_while_the_split_is_at_any_stage(schema_driver, stage):
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state=$stage", stage=stage).consume()
    with pytest.raises(SchemaCompatibilityError, match="rule split maintenance is incomplete"):
        SchemaManager(schema_driver).initialise(edition="community")


def test_the_split_reaches_schema_3_and_records_one_event_per_tenant(schema_driver):
    split_world.build(schema_driver)
    metadata = _split(schema_driver)
    assert (metadata["schema_version"], metadata["core_version"], metadata["migration_state"],
            metadata["last_successful_migration"]) == (3, 3, "ready", "core-3")
    SchemaManager(schema_driver).initialise(edition="community")
    with schema_driver.session() as session:
        events = {row["tenant"]: json.loads(row["after"]) for row in session.run(
            "MATCH (a:AdminEvent {action:'rules_split'}) RETURN a.tenant_id AS tenant, a.after AS after, "
            "a.subject_kind AS kind")}
    assert events == {"acme": {"shared_rules": 6, "skills_affected": 4, "copies": 7, "held_items": 4,
                               "divergence_closed": 1},
                      "beta": {"shared_rules": 1, "skills_affected": 2, "copies": 1, "held_items": 0,
                               "divergence_closed": 0}}
    assert "rule_split_counts" not in _metadata(schema_driver)


def test_a_second_run_changes_nothing(schema_driver):
    split_world.build(schema_driver)
    _split(schema_driver)
    before = _snapshot(schema_driver)
    assert _split(schema_driver)["migration_state"] == "ready"
    assert _snapshot(schema_driver) == before


@pytest.mark.parametrize("stop", ["after_fence", "after_one_rule", "after_four_rules", "before_verified", "before_ready"])
def test_a_stop_at_any_stage_resumes_to_the_same_graph(schema_driver, monkeypatch, stop):
    expected = _expected(schema_driver)
    split_world.build(schema_driver)
    calls = {"n": 0}
    original_split, original_advance = rule_split.split_rule, rule_split._advance

    def split_then_stop(tx, plan, *, now):
        calls["n"] += 1
        if calls["n"] > {"after_one_rule": 1, "after_four_rules": 4}.get(stop, 99):
            raise Stop
        return original_split(tx, plan, now=now)

    def advance_then_stop(driver, expected_stage, stage):
        if (stop, expected_stage) in (("after_fence", "rule_split_fenced"), ("before_verified", "rule_splitting")):
            raise Stop
        return original_advance(driver, expected_stage, stage)

    monkeypatch.setattr(rule_split, "split_rule", split_then_stop)
    monkeypatch.setattr(rule_split, "_advance", advance_then_stop)
    if stop == "before_ready":
        monkeypatch.setattr(rule_split, "_record_events", lambda tx: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        _split(schema_driver)
    assert _metadata(schema_driver)["migration_state"] in rule_split.RULE_SPLIT_STAGES
    with pytest.raises(SchemaCompatibilityError):
        SchemaManager(schema_driver).initialise(edition="community")
    monkeypatch.undo()
    assert _split(schema_driver)["migration_state"] == "ready"
    assert split_world.normalised(schema_driver) == expected


def test_concurrent_splitters_split_each_rule_once(schema_driver):
    expected = _expected(schema_driver)
    split_world.build(schema_driver)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: _split(schema_driver), range(4)))
    assert split_world.normalised(schema_driver) == expected


def test_the_source_identity_step_returns_at_once_mid_split_and_after_it(schema_driver):
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state='rule_splitting'").consume()
    before = _snapshot(schema_driver)
    assert SchemaManager(schema_driver).migrate_source_identity(old_writers_stopped=True)["migration_state"] == "rule_splitting"
    assert _snapshot(schema_driver) == before
    _split(schema_driver)
    before = _snapshot(schema_driver)
    assert SchemaManager(schema_driver).migrate_source_identity(old_writers_stopped=True)["schema_version"] == 3
    assert _snapshot(schema_driver) == before


def test_an_empty_graph_starts_at_schema_3_with_nothing_to_split(schema_driver):
    split_world.reset(schema_driver)
    assert _split(schema_driver) is None
    SchemaManager(schema_driver).initialise(edition="community")
    assert _metadata(schema_driver)["schema_version"] == 3


def test_a_schema_1_graph_is_left_to_the_source_identity_step(schema_driver):
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=1, m.schema_version=1").consume()
    before = _snapshot(schema_driver)
    assert _split(schema_driver)["schema_version"] == 1
    assert _snapshot(schema_driver) == before


def test_the_split_versions_are_recorded_once_with_their_cause(schema_driver):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory
    store = split_world.build(schema_driver)
    history = SkillHistory(store=store, publisher=Publisher(store))
    for tenant in ("acme", "beta"):
        for skill in store.skills_for_tenant(tenant):
            history.capture_required(skill.id, tenant, cause=SkillVersionCause.CREATED)
    _split(schema_driver)
    store = Neo4jGraphStore(schema_driver)
    history = SkillHistory(store=store, publisher=Publisher(store))
    assert rule_split.capture_pending_split_versions(schema_driver, history) == 4
    changed = {(tenant, skill.id) for tenant in ("acme", "beta") for skill in store.skills_for_tenant(tenant)
               if store.latest_skill_version(tenant, skill.id).cause is SkillVersionCause.RULE_SPLIT}
    assert changed == {("acme", "blog"), ("acme", "email"), ("acme", "press"), ("beta", "email")}
    # In the words the skill's history label uses for the same event.
    assert {store.latest_skill_version(tenant, skill).detail for tenant, skill in changed} == {
        "the rules shared between skills were split into one copy per skill"}
    assert rule_split.pending_split_captures(schema_driver) == ()
    assert rule_split.capture_pending_split_versions(schema_driver, history) == 0


def test_community_start_up_splits_the_graph_and_records_the_versions(schema_driver, tmp_path):
    from oms.community.app import build_community
    from oms.settings.core import CoreSettings
    split_world.build(schema_driver)
    # If `validate()` refuses a default (a password, a port), set the field it names.
    build_community(CoreSettings(data_dir=tmp_path, tenant_id="acme"), schema_driver)
    assert _metadata(schema_driver)["schema_version"] == 3
    assert {ref.tenant_id for ref in rule_split.pending_split_captures(schema_driver)} == {"beta"}


# Beyond the task text (Review Focus 3, no stop or refusal wedges start-up):
# the stage names, rules the split refuses, a rule shared again before
# `ready`, races the first read cannot see, and versions that fail to record.

_SOURCE_IDENTITY_STAGES = ("fenced", "additive_ready", "data_ready", "constraints_ready", "ready")


def test_the_stages_live_beside_the_schema_and_share_no_name_with_the_source_identity_step():
    # Plan D5: the source identity step runs first on every start, so a shared
    # name would let it take a graph in the middle of the split for its own.
    assert rule_split.RULE_SPLIT_STAGES is schema_metadata.RULE_SPLIT_STAGES
    assert rule_split.RULE_SPLIT_STAGES == ("rule_split_fenced", "rule_splitting", "rule_split_verified")
    assert not set(rule_split.RULE_SPLIT_STAGES) & set(_SOURCE_IDENTITY_STAGES)


def _place_in_a_deleted_skill(driver, rule_id):
    """A hand-edited graph: a section whose skill was deleted still places the rule."""
    with driver.session() as session:
        session.run("MATCH (r:Rule {id:$rule, tenant_id:'acme'}) "
                    "CREATE (s:Section:Entity {id:'ghost-rules', skill_id:'ghost', tenant_id:'acme', "
                    "kind:'rules', heading:'Rules', order:1, mutability:'system_aggregated'}) "
                    "CREATE (s)-[:CONTAINS_RULE {order:1}]->(r)", rule=rule_id).consume()


def test_a_rule_the_split_refuses_is_named_with_its_repair_and_the_next_start_finishes(schema_driver):
    expected = _expected(schema_driver)
    split_world.build(schema_driver)
    _place_in_a_deleted_skill(schema_driver, "rule-t-plain")
    assert any("ghost-rules" in warning and "does not exist" in warning for plan in rule_split.plan_split(schema_driver)
               for rule in plan.rules if rule.rule_id == "rule-t-plain" for warning in rule.warnings)
    for _ in range(2):  # every start says the same until the graph is repaired
        with pytest.raises(SchemaCompatibilityError) as refused:
            _split(schema_driver)
        message = str(refused.value)
        assert "rule rule-t-plain" in message and "section ghost-rules" in message and "skill ghost" in message
        assert "delete that placement or restore the skill" in message
        assert "next start" in message
        # Left at a stage the next start resumes, with every other shared rule split.
        assert _metadata(schema_driver)["migration_state"] == "rule_splitting"
        assert [rule.rule_id for plan in rule_split.plan_split(schema_driver) for rule in plan.rules] == ["rule-t-plain"]
        with pytest.raises(SchemaCompatibilityError, match="rule split maintenance is incomplete"):
            SchemaManager(schema_driver).initialise(edition="community")
        before = _snapshot(schema_driver)
        assert SchemaManager(schema_driver).migrate_source_identity(old_writers_stopped=True)["migration_state"] == "rule_splitting"
        assert _snapshot(schema_driver) == before
    with schema_driver.session() as session:
        session.run("MATCH (s:Section {id:'ghost-rules'}) DETACH DELETE s").consume()
    assert _split(schema_driver)["migration_state"] == "ready"
    assert split_world.normalised(schema_driver) == expected


def _place_only_in_deleted_skills(driver: Driver, rule_id: str) -> None:
    """A hand-edited graph: a rule in no skill that sections of two deleted
    skills still place, so none of its skills has a Skill node."""
    with driver.session() as session:
        session.run("CREATE (r:Rule:Entity {id:$rule, tenant_id:'acme', body:'A rule of two lost skills.', "
                    "status:'active', corroboration_count:1, created_at:datetime()}) "
                    "WITH r UNWIND ['ghost-a', 'ghost-b'] AS skill "
                    "CREATE (s:Section:Entity {id: skill + '-rules', skill_id: skill, tenant_id:'acme', "
                    "kind:'rules', heading:'Rules', order:1, mutability:'system_aggregated'}) "
                    "CREATE (s)-[:CONTAINS_RULE {order:1}]->(r)", rule=rule_id).consume()


def test_a_shared_rule_none_of_whose_skills_exists_is_refused_by_name_and_the_next_start_finishes(
        schema_driver: Driver) -> None:
    expected = _expected(schema_driver)
    split_world.build(schema_driver)
    _place_only_in_deleted_skills(schema_driver, "rule-lost")
    with pytest.raises(SchemaCompatibilityError) as planned:
        rule_split.plan_split(schema_driver)
    with pytest.raises(SchemaCompatibilityError) as refused:
        _split(schema_driver)
    for message in (str(planned.value), str(refused.value)):
        assert "rule rule-lost in tenant acme" in message
        assert "section ghost-a-rules of skill ghost-a" in message and "section ghost-b-rules of skill ghost-b" in message
        assert "delete those placements or restore one of those skills" in message
    # Stopped before any rule was split, at a stage the next start resumes.
    assert _metadata(schema_driver)["migration_state"] == "rule_splitting"
    with schema_driver.session() as session:
        assert session.run("MATCH (r:Rule) WHERE r.split_from IS NOT NULL RETURN count(r) AS n").single()["n"] == 0
        session.run("MATCH (s:Section) WHERE s.id IN ['ghost-a-rules', 'ghost-b-rules'] DETACH DELETE s").consume()
        session.run("MATCH (r:Rule {id:'rule-lost'}) DETACH DELETE r").consume()
    assert _split(schema_driver)["migration_state"] == "ready"
    assert split_world.normalised(schema_driver) == expected


def test_community_start_up_refuses_a_graph_the_split_cannot_finish_and_starts_once_repaired(schema_driver,
                                                                                               tmp_path):
    from oms.community.app import build_community
    from oms.settings.core import CoreSettings
    split_world.build(schema_driver)
    _place_in_a_deleted_skill(schema_driver, "rule-t-plain")
    settings = CoreSettings(data_dir=tmp_path, tenant_id="acme")
    with pytest.raises(SchemaCompatibilityError, match="delete that placement or restore the skill"):
        build_community(settings, schema_driver)
    assert _metadata(schema_driver)["migration_state"] == "rule_splitting"
    with schema_driver.session() as session:
        assert session.run("MATCH (v:SkillVersion) RETURN count(v) AS n").single()["n"] == 0
        session.run("MATCH (s:Section {id:'ghost-rules'}) DETACH DELETE s").consume()
    build_community(settings, schema_driver)
    assert (_metadata(schema_driver)["schema_version"], _metadata(schema_driver)["migration_state"]) == (3, "ready")


def test_a_rule_shared_again_before_ready_sends_the_split_back_to_splitting(schema_driver, monkeypatch):
    store = split_world.build(schema_driver)
    monkeypatch.setattr(rule_split, "_record_events", lambda tx: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        _split(schema_driver)
    monkeypatch.undo()
    assert _metadata(schema_driver)["migration_state"] == "rule_split_verified"
    share_rule(store, "rule-blog-a", "email", tenant_id="acme")  # a hand edit while it was stopped
    with pytest.raises(SchemaCompatibilityError, match="rule-blog-a"):
        _split(schema_driver)
    assert _metadata(schema_driver)["migration_state"] == "rule_splitting"
    assert _split(schema_driver)["migration_state"] == "ready"
    assert rule_split.plan_split(schema_driver) == ()
    assert [skill.id for skill in store.skills_for_rule("rule-blog-a", tenant_id="acme")] == ["blog"]


class _SchemaTwoAppearsAfterThePreflight:
    """The driver, with a schema 2 graph of shared rules written once the
    preflight has read an empty database (a race the preflight cannot see)."""
    def __init__(self, driver):
        self._driver, self._sessions = driver, 0

    def session(self, **kwargs):
        self._sessions += 1
        if self._sessions == 2:
            split_world.build(self._driver)
        return self._driver.session(**kwargs)


def test_initialise_refuses_a_schema_2_graph_its_preflight_did_not_see(schema_driver):
    split_world.reset(schema_driver)
    with pytest.raises(SchemaCompatibilityError, match="rule split maintenance"):
        SchemaManager(_SchemaTwoAppearsAfterThePreflight(schema_driver)).initialise(edition="community")
    assert (_metadata(schema_driver)["schema_version"], _metadata(schema_driver)["migration_state"]) == (2, "ready")


@pytest.mark.parametrize("rule_id, skill_id, damage, repair, named", [
    # The skill has no Entity label, so the store skips removing the original's membership.
    ("rule-t-plain", "press", "MATCH (s:Skill {id:'press', tenant_id:'acme'}) REMOVE s:Entity",
     "MATCH (s:Skill {id:'press', tenant_id:'acme'}) SET s:Entity", "no Entity label"),
    # The section became free text after the rule was placed, so no copy can be placed there.
    ("rule-t-placed", "press", "MATCH (s:Section {id:'press-rules'}) SET s.mutability='authorial_passthrough'",
     "MATCH (s:Section {id:'press-rules'}) SET s.mutability='system_aggregated'", "press-rules"),
])
def test_a_rule_the_store_cannot_split_rolls_back_whole_and_is_named(schema_driver, rule_id, skill_id, damage,
                                                                     repair, named):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.domain.copies import split_rule_id
    from oms.domain.identity import SkillRef
    expected = _expected(schema_driver)
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run(damage).consume()
    with pytest.raises(SchemaCompatibilityError, match=named) as refused:
        _split(schema_driver)
    assert f"rule {rule_id}" in str(refused.value)
    assert Neo4jGraphStore(schema_driver).get_rule(split_rule_id(rule_id, SkillRef("acme", skill_id))) is None
    assert [rule.rule_id for plan in rule_split.plan_split(schema_driver) for rule in plan.rules] == [rule_id]
    with schema_driver.session() as session:
        session.run(repair).consume()
    assert _split(schema_driver)["migration_state"] == "ready"
    assert split_world.normalised(schema_driver) == expected


class _SplitBeginsAfterTheRead:
    """The driver; once the source identity step has read a schema 1 graph,
    another process takes it to schema 2 and begins the split (a race)."""
    def __init__(self, driver):
        self._driver, self._sessions, self.raced = driver, 0, None

    def session(self, **kwargs):
        self._sessions += 1
        if self._sessions == 3:  # the read, the DDL, then the fence
            with self._driver.session() as session:
                session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=2, m.schema_version=2, "
                            "m.migration_state='rule_splitting'").consume()
            self.raced = _snapshot(self._driver)
        return self._driver.session(**kwargs)


def test_the_source_identity_step_never_writes_over_a_split_that_began_after_it_read(schema_driver):
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=1, m.schema_version=1").consume()
    driver = _SplitBeginsAfterTheRead(schema_driver)
    assert SchemaManager(driver).migrate_source_identity(old_writers_stopped=True)["migration_state"] == "rule_splitting"
    assert driver.raced is not None and _snapshot(schema_driver) == driver.raced


class _Counting:
    """The fixture driver, counting the sessions it opens."""
    def __init__(self, driver):
        self._driver, self.sessions = driver, 0

    def session(self, **kwargs):
        self.sessions += 1
        return self._driver.session(**kwargs)


def test_every_boot_after_the_split_reads_once_per_step_and_writes_nothing(schema_driver):
    split_world.build(schema_driver)
    _split(schema_driver)
    before = _snapshot(schema_driver)
    for step in ("migrate_source_identity", "split_rules"):
        counting = _Counting(schema_driver)
        assert getattr(SchemaManager(counting), step)(old_writers_stopped=True)["migration_state"] == "ready"
        assert counting.sessions == 1, step
    assert _snapshot(schema_driver) == before


class _FailsFor:
    """A history whose capture fails for one skill (a publisher that cannot render it)."""
    def __init__(self, history, skill_id):
        self._history, self._skill_id = history, skill_id

    def capture_required(self, skill_id, tenant_id, **kwargs):
        if skill_id == self._skill_id:
            raise RuntimeError("the publisher could not render this skill")
        return self._history.capture_required(skill_id, tenant_id, **kwargs)


def _pending(driver):
    return {(ref.tenant_id, ref.skill_id) for ref in rule_split.pending_split_captures(driver)}


def test_a_version_that_cannot_be_recorded_stays_pending_and_the_others_are_recorded(schema_driver):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory
    split_world.build(schema_driver)
    _split(schema_driver)
    store = Neo4jGraphStore(schema_driver)
    history = SkillHistory(store=store, publisher=Publisher(store))
    assert rule_split.capture_pending_split_versions(schema_driver, _FailsFor(history, "email"), tenant_id="acme") == 2
    assert _pending(schema_driver) == {("acme", "email"), ("beta", "email")}
    assert store.latest_skill_version("acme", "email") is None
    assert rule_split.capture_pending_split_versions(schema_driver, history, tenant_id="acme") == 1
    assert _pending(schema_driver) == {("beta", "email")}
    assert store.latest_skill_version("acme", "email").cause is SkillVersionCause.RULE_SPLIT


def test_community_start_up_goes_on_when_the_versions_cannot_be_recorded(schema_driver, tmp_path, monkeypatch):
    from oms.community.app import build_community
    from oms.settings.core import CoreSettings
    from oms.skills.history import SkillHistory

    def unavailable(self, skill_id, tenant_id, **kwargs):
        raise RuntimeError("the publisher could not render this skill")

    split_world.build(schema_driver)
    monkeypatch.setattr(SkillHistory, "capture_required", unavailable)
    services = build_community(CoreSettings(data_dir=tmp_path, tenant_id="acme"), schema_driver)
    assert services.store.get_skill("blog", tenant_id="acme") is not None
    assert _metadata(schema_driver)["schema_version"] == 3
    assert _pending(schema_driver) == {("acme", "blog"), ("acme", "email"), ("acme", "press"), ("beta", "email")}
