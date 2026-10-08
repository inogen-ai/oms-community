"""Real maintenance migration preserves custody and fences incomplete upgrades."""
from datetime import datetime, timezone

import pytest

from oms.domain.identity import SkillRef
from oms.schema.metadata import SchemaCompatibilityError, SchemaManager
from tests.schema.test_critic_schema import schema_driver, _snapshot, _metadata  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def legacy(schema_driver):
    with schema_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
        for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            session.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS").consume()
        session.run("CREATE CONSTRAINT skill_id FOR (s:Skill) REQUIRE s.id IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT artefact_id FOR (a:Artefact) REQUIRE a.id IS UNIQUE").consume()
        session.run("CREATE (:OMSMetadata {id:'core',edition:'community',managed_edition:'community',"
                    "core_version:1,schema_version:1,private_version:0,minimum_core_version:'1.4.0'})").consume()
        session.run("CREATE (s:Skill:Entity {id:'expenses',tenant_id:'one',name:'expenses',description:'',domain:'finance'}),"
                    "(other:Skill:Entity {id:'travel',tenant_id:'one',name:'travel',description:'',domain:'finance'}),"
                    "(r:Rule:Entity {id:'rule',tenant_id:'one',body:'Keep receipts'}),"
                    "(sec:Section:Entity {id:'section',tenant_id:'one',skill_id:'expenses'}),"
                    "(b:ContentBlock:Entity {id:'block',tenant_id:'one',body:'Retained prose'}),"
                    "(e:Example:Entity {id:'example',tenant_id:'one',parent_section_id:'section',body:'Example'}),"
                    "(a:Artefact:Entity {id:'digest',tenant_id:'one',content_ref:'exact-bytes',source_ref:'upload/ref.txt'}),"
                    "(v:SkillVersion {id:'version',tenant_id:'one',skill_id:'expenses',parts_json:'[]'}),"
                    "(p:Publication {id:'org-publication',tenant_id:'one',skill_id:'(org)',source_ref:'root.md'}),"
                    "(r)-[:BELONGS_TO]->(s),(r)-[:BELONGS_TO]->(other),"
                    "(s)-[:HAS_SECTION]->(sec),(sec)-[:CONTAINS_BLOCK]->(b),"
                    "(sec)-[:HAS_EXAMPLE]->(e),(s)-[:HAS_ARTEFACT {path:'ref.txt'}]->(a),"
                    "(other)-[:HAS_ARTEFACT {path:'same.txt'}]->(a),(s)-[:HAS_VERSION]->(v),"
                    "(s)-[:HAS_REFERENCE {prose:'retain'}]->(a)").consume()
    return schema_driver


def test_normal_startup_requires_explicit_maintenance_before_any_ddl(legacy):
    before = _snapshot(legacy)
    with pytest.raises(SchemaCompatibilityError, match="maintenance"):
        SchemaManager(legacy).initialise()
    assert _snapshot(legacy) == before
    with pytest.raises(SchemaCompatibilityError, match="stopped"):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=False)
    assert _snapshot(legacy) == before


def test_maintenance_migrates_keys_and_independent_file_occurrences(legacy):
    manager = SchemaManager(legacy)
    manager.migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        keys = {row['id']: row['key'] for row in session.run(
            "MATCH (s:Skill) RETURN s.id AS id,s.storage_key AS key")}
        files = [dict(row) for row in session.run(
            "MATCH (s:Skill)-[r:HAS_ARTEFACT]->(a:Artefact) "
            "RETURN s.id AS skill,r.path AS path,a.storage_key AS key,a.content_ref AS bytes ORDER BY skill")]
        assert session.run("MATCH (:Rule {id:'rule'})-[:BELONGS_TO]->() RETURN count(*) AS n").single()['n'] == 2
        assert session.run("MATCH (:Section)-[:CONTAINS_BLOCK]->(:ContentBlock {body:'Retained prose'}) RETURN count(*) AS n").single()['n'] == 1
        assert session.run("MATCH (:Publication {id:'org-publication'}) RETURN count(*) AS n").single()['n'] == 1
        assert session.run("MATCH (:Skill)-[:HAS_REFERENCE {prose:'retain'}]->(:Artefact) RETURN count(*) AS n").single()['n'] == 1
        names = {row['name'] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")}
    assert keys['expenses'] == SkillRef('one', 'expenses').storage_key
    assert len(files) == 2 and files[0]['key'] != files[1]['key']
    assert {item['bytes'] for item in files} == {'exact-bytes'}
    assert 'skill_id' not in names and 'artefact_id' not in names
    assert _metadata(legacy)['migration_state'] == 'ready'
    assert _metadata(legacy)['schema_version'] == 2
    before = _snapshot(legacy)
    manager.migrate_source_identity(old_writers_stopped=True)
    manager.initialise()
    assert _snapshot(legacy) == before
    with pytest.raises(SchemaCompatibilityError):
        manager._check(_metadata(legacy), 'community', 1)


def test_data_failure_rolls_back_keys_but_retains_fence_and_resumes(legacy, monkeypatch):
    from oms.schema.migrations import source_identity
    original = source_identity.migrate_data

    def fail(tx):
        original(tx)
        raise RuntimeError('injected data failure')

    monkeypatch.setattr(source_identity, 'migrate_data', fail)
    with pytest.raises(RuntimeError, match='injected'):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        assert session.run("MATCH (s:Skill) WHERE s.storage_key IS NOT NULL RETURN count(*) AS n").single()['n'] == 0
        assert session.run("MATCH (:Artefact) RETURN count(*) AS n").single()['n'] == 1
    state = _metadata(legacy)
    assert state['schema_version'] == state['core_version'] == 2
    assert state['migration_state'] != 'ready'
    before = _snapshot(legacy)
    with pytest.raises(SchemaCompatibilityError, match='incomplete'):
        SchemaManager(legacy).initialise()
    assert _snapshot(legacy) == before
    monkeypatch.setattr(source_identity, 'migrate_data', original)
    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    assert _metadata(legacy)['migration_state'] == 'ready'


@pytest.mark.parametrize("change", ["REMOVE b.tenant_id", "SET b.tenant_id='two'"])
def test_migration_refuses_missing_or_contradictory_ownership(legacy, change):
    with legacy.session() as session:
        session.run("MATCH (b:ContentBlock) " + change).consume()
    with pytest.raises(SchemaCompatibilityError, match='ownership'):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        assert session.run("MATCH (s:Skill) WHERE s.storage_key IS NOT NULL RETURN count(*) AS n").single()['n'] == 0


def test_ddl_interruption_is_resumable_without_reapplying_data(legacy, monkeypatch):
    from oms.schema.migrations import source_identity
    original = source_identity._ddl

    def interrupted(driver, statements):
        original(driver, statements)
        if any(statement.startswith('DROP CONSTRAINT') for statement in statements):
            raise RuntimeError('interrupted DDL completion')

    monkeypatch.setattr(source_identity, '_ddl', interrupted)
    with pytest.raises(RuntimeError, match='interrupted DDL'):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    assert _metadata(legacy)['migration_state'] == 'data_ready'
    with legacy.session() as session:
        rows = list(session.run("MATCH (a:ArtefactOccurrence) RETURN a.storage_key AS key"))
    assert len(rows) == 2
    monkeypatch.setattr(source_identity, '_ddl', original)
    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        assert session.run("MATCH (a:ArtefactOccurrence) RETURN count(a) AS n").single()['n'] == 2


def test_global_ids_and_section_parents_cannot_change_tenant(legacy):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.domain.models import Rule, Section, Transaction
    from oms.domain.types import Mutability, SectionKind, SignalType, SourceRuntime

    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    store = Neo4jGraphStore(legacy)
    store.upsert_rule(Rule(id='global', body='one', tenant_id='one'))
    with pytest.raises(ValueError):
        store.upsert_rule(Rule(id='global', body='two', tenant_id='two'))
    txn = Transaction(id='tx', signal_type=SignalType.SKILL_IMPORT,
                      source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref='fixture', tenant_id='one',
                      timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    store.upsert_transaction(txn)
    from dataclasses import replace
    with pytest.raises(ValueError):
        store.upsert_transaction(replace(txn, tenant_id='two'))
    with pytest.raises(KeyError):
        store.upsert_section(Section(id='foreign', skill_id='expenses', tenant_id='two',
            kind=SectionKind.PROSE, heading='Foreign', order=0,
            mutability=Mutability.AUTHORIAL_PASSTHROUGH))
    assert store.get_section('foreign', tenant_id='two') is None


def test_ambiguous_file_digest_graph_lookup_does_not_pick_an_occurrence(legacy):
    from oms.adapters.neo4j.store import Neo4jGraphStore

    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    store = Neo4jGraphStore(legacy)
    assert store.graph_node('digest', 'one') is None
    assert store.graph_neighbours('digest', 'one')['nodes'] == []


def test_conflicting_legacy_file_paths_refuse_instead_of_overwriting(legacy):
    with legacy.session() as session:
        session.run("MATCH (s:Skill {id:'expenses'}) CREATE (a:Artefact {id:'other-digest',"
                    "tenant_id:'one',content_ref:'other-bytes'}) CREATE (s)-[:HAS_ARTEFACT {path:'ref.txt'}]->(a)").consume()
    with pytest.raises(SchemaCompatibilityError, match='occurrence'):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        assert session.run("MATCH (:Skill {id:'expenses'})-[:HAS_ARTEFACT]->(a) RETURN count(a) AS n").single()['n'] == 2
        assert session.run("MATCH (:ArtefactOccurrence) RETURN count(*) AS n").single()['n'] == 0


@pytest.mark.parametrize('change', [
    "MATCH (s:Skill {id:'travel'}),(e:Example) CREATE (s)-[:HAS_EXAMPLE]->(e)",
    "MATCH (s:Section)-[r:HAS_EXAMPLE]->(e:Example) DELETE r REMOVE e.parent_section_id",
])
def test_ambiguous_or_missing_legacy_example_parent_refuses(legacy, change):
    with legacy.session() as session:
        session.run(change).consume()
    with pytest.raises(SchemaCompatibilityError, match='example ownership'):
        SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)


def test_block_revision_refuses_another_sections_block_before_writing(legacy):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.domain.models import ContentBlock, Section
    from oms.domain.types import BlockStatus, Mutability, SectionKind

    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    store = Neo4jGraphStore(legacy)
    for identifier in ('first', 'second'):
        store.upsert_section(Section(id=identifier, skill_id='expenses', tenant_id='one',
            kind=SectionKind.PROSE, heading=identifier, order=1, mutability=Mutability.AUTHORIAL_PASSTHROUGH))
    old = ContentBlock(id='old', content_ref='old', tenant_id='one', kind=SectionKind.PROSE, body='old', source_ref='fixture')
    incoming = ContentBlock(id='new', content_ref='new', tenant_id='one', kind=SectionKind.PROSE, body='new', source_ref='fixture')
    store.upsert_content_block(old)
    store.attach_block(old, 'first', tenant_id='one')
    with pytest.raises((KeyError, ValueError)):
        store.supersede_block('old', incoming, 'second', tenant_id='one')
    assert store.get_content_block('old', tenant_id='one').status == BlockStatus.ACTIVE
    assert store.get_content_block('new', tenant_id='one') is None


def test_equal_order_custody_reads_use_stable_public_id_tiebreaks(legacy):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.domain.models import ContentBlock, Example, Rule, Section, SkillVersion
    from oms.domain.types import ExampleKind, Mutability, SectionKind, SkillVersionCause

    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    store = Neo4jGraphStore(legacy)
    for identifier, mutability in [('rules', Mutability.SYSTEM_AGGREGATED), ('prose', Mutability.AUTHORIAL_PASSTHROUGH)]:
        store.upsert_section(Section(id=identifier, skill_id='travel', tenant_id='one',
            kind=SectionKind.PROSE, heading=identifier, order=1, mutability=mutability))
    for identifier in ('z', 'a'):
        rule = Rule(id=identifier, body=identifier, tenant_id='one', corroboration_count=99 if identifier == 'z' else 0)
        store.upsert_rule(rule)
        store.attach_rule(rule, 'rules', order=0, tenant_id='one')
        block = ContentBlock(id=identifier, content_ref=identifier, kind=SectionKind.PROSE,
                             tenant_id='one', body=identifier, source_ref='fixture')
        store.upsert_content_block(block)
        store.attach_block(block, 'prose', tenant_id='one')
        store.upsert_example(Example(id=identifier, body=identifier, kind=ExampleKind.NEUTRAL,
            tenant_id='one', parent_skill_id='travel', order=0), tenant_id='one')
    for identifier in ('a', 'z'):
        store.append_skill_version(SkillVersion(id=identifier, skill_id='travel', tenant_id='one',
            revision=identifier, cause=SkillVersionCause.CREATED, at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert [rule.id for rule in store.rules_for_section('rules', tenant_id='one')] == ['a', 'z']
    assert [placement.rule_id for placement in store.rule_placements_for_section('rules', tenant_id='one')] == ['a', 'z']
    assert [block.id for block in store.blocks_for_section('prose', tenant_id='one')] == ['a', 'z']
    assert [example.id for example in store.examples_for_skill('travel', tenant_id='one')] == ['a', 'z']
    assert [version.id for version in store.skill_versions('one', 'travel')] == ['z', 'a']


def test_legacy_example_ancillary_parent_fields_preserve_actual_section_ownership(legacy):
    with legacy.session() as session:
        session.run("MATCH (e:Example) SET e.parent_rule_id='rule',e.parent_skill_id='expenses'").consume()
    SchemaManager(legacy).migrate_source_identity(old_writers_stopped=True)
    with legacy.session() as session:
        row = session.run("MATCH (s:Section)-[:HAS_EXAMPLE]->(e:Example) "
                          "RETURN s.id AS section,e.parent_section_id AS declared_section,"
                          "e.parent_rule_id AS rule,e.parent_skill_id AS skill").single()
    assert dict(row) == {'section': 'section', 'declared_section': 'section', 'rule': 'rule', 'skill': 'expenses'}
