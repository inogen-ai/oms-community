"""Independent, real-database checks for schema ownership and migrations."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def schema_driver():
    from neo4j import GraphDatabase
    external_uri = os.environ.get("OMS_TEST_NEO4J_URI")
    if external_uri:
        if os.environ.get("OMS_TEST_NEO4J_DISPOSABLE") != "1":
            raise pytest.UsageError("OMS_TEST_NEO4J_DISPOSABLE=1 is required before resetting an external test database")
        password = os.environ.get("OMS_TEST_NEO4J_PASSWORD")
        if not password:
            raise pytest.UsageError("OMS_TEST_NEO4J_PASSWORD is required for the disposable test database")
        with GraphDatabase.driver(external_uri, auth=("neo4j", password)) as driver:
            driver.verify_connectivity()
            yield driver
        return
    from testcontainers.neo4j import Neo4jContainer
    with Neo4jContainer("neo4j:5.20") as container:
        with GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", container.password)) as driver:
            yield driver


@pytest.fixture
def database(schema_driver):
    with schema_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    return schema_driver


def _manager(driver):
    from oms.schema.metadata import SchemaManager
    return SchemaManager(driver)


def _metadata(driver):
    with driver.session() as session:
        rows = list(session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS value"))
    assert len(rows) == 1
    return dict(rows[0]["value"])


def _snapshot(driver):
    """Include DDL so an incompatible startup cannot mutate then refuse."""
    with driver.session() as session:
        nodes = [(sorted(row["n"].labels), dict(row["n"])) for row in session.run("MATCH (n) RETURN n")]
        edges = [dict(row) for row in session.run(
            "MATCH (a)-[r]->(b) RETURN elementId(a) AS start,type(r) AS type,"
            "elementId(b) AS end,properties(r) AS properties")]
        constraints = [dict(row) for row in session.run(
            "SHOW CONSTRAINTS YIELD name,type,entityType,labelsOrTypes,properties "
            "RETURN name,type,entityType,labelsOrTypes,properties")]
        indexes = [dict(row) for row in session.run(
            "SHOW INDEXES YIELD name,type,entityType,labelsOrTypes,properties "
            "RETURN name,type,entityType,labelsOrTypes,properties")]
    return tuple(tuple(sorted(json.dumps(value, sort_keys=True, default=str) for value in part))
                 for part in (nodes, edges, constraints, indexes))


def test_empty_database_becomes_a_versioned_community_database(database):
    _manager(database).initialise(edition="community")

    metadata = _metadata(database)
    assert metadata["edition"] == "community" and metadata["core_version"] == 1
    assert metadata["private_version"] == 0
    assert metadata["schema_version"] == 1 and metadata["managed_edition"] == "community"
    assert isinstance(metadata["minimum_core_version"], str) and metadata["minimum_core_version"]
    assert metadata["last_successful_migration"]


def test_repeated_initialisation_does_not_change_metadata_or_graph(database):
    _manager(database).initialise(edition="community")
    before = _snapshot(database)

    _manager(database).initialise(edition="community")

    assert _snapshot(database) == before


def test_community_refuses_unmarked_nonempty_database_before_any_mutation(database):
    with database.session() as session:
        session.run("CREATE (:Skill {id:'legacy', tenant_id:'acme', body:'Original'})").consume()
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community")

    assert _snapshot(database) == before


def test_community_refuses_enterprise_database_before_any_mutation(database):
    _manager(database).initialise(edition="enterprise")
    with database.session() as session:
        session.run("CREATE (:EnterpriseInvariant {value:'Preserve'})").consume()
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community")

    assert _snapshot(database) == before


def test_enterprise_can_adopt_legacy_graph_without_rewriting_its_data(database):
    with database.session() as session:
        session.run("CREATE (r:Rule {id:'rule-original', tenant_id:'acme', body:'Original'})"
                    "-[:DERIVED_FROM {evidence:'original'}]->"
                    "(t:Transaction {id:'original', tenant_id:'acme'})").consume()
    before_nodes, before_edges, _, _ = _snapshot(database)

    _manager(database).initialise(edition="enterprise")

    after_nodes, after_edges, _, _ = _snapshot(database)
    assert set(before_nodes).issubset(after_nodes) and before_edges == after_edges
    metadata = _metadata(database)
    assert metadata["edition"] == "enterprise" and metadata["private_version"] >= 1


def test_community_to_enterprise_upgrade_preserves_ids_lineage_and_workspace(database):
    _manager(database).initialise(edition="community")
    with database.session() as session:
        session.run("CREATE (r:Rule {id:'rule-original', tenant_id:'acme', body:'Original'})"
                    "-[:DERIVED_FROM {evidence:'original'}]->"
                    "(t:Transaction {id:'original', tenant_id:'acme'})").consume()
        before = [dict(row) for row in session.run(
            "MATCH (r:Rule)-[edge:DERIVED_FROM]->(t:Transaction) "
            "RETURN properties(r) AS rule,properties(edge) AS edge,properties(t) AS transaction")]

    _manager(database).initialise(edition="enterprise")

    with database.session() as session:
        after = [dict(row) for row in session.run(
            "MATCH (r:Rule)-[edge:DERIVED_FROM]->(t:Transaction) "
            "RETURN properties(r) AS rule,properties(edge) AS edge,properties(t) AS transaction")]
    assert after == before
    metadata = _metadata(database)
    assert metadata["edition"] == "enterprise" and metadata["core_version"] == 1


def test_newer_core_schema_is_refused_without_touching_graph(database):
    _manager(database).initialise(edition="community")
    with database.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=999,m.schema_version=999").consume()
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community", target_version=1)

    assert _snapshot(database) == before


def test_newer_minimum_core_package_version_is_refused_even_when_schema_matches(database):
    _manager(database).initialise(edition="community")
    with database.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.minimum_core_version='999.0.0'").consume()
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community", target_version=1)

    assert _snapshot(database) == before


@pytest.mark.parametrize("changes", [
    {"edition": "enterprise", "managed_edition": "community"},
    {"edition": "community", "managed_edition": "community", "private_version": 1},
    {"core_version": 0, "schema_version": 1},
    {"private_version": True},
    {"private_version": -1},
    {"minimum_core_version": "not a version"},
])
def test_inconsistent_or_invalid_metadata_fails_closed_without_repairing_it(database, changes):
    _manager(database).initialise(edition="community")
    with database.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m += $changes", changes=changes).consume()
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community")

    assert _snapshot(database) == before


@pytest.mark.parametrize("target", [True, False, 0, -1, 1.0, "1"])
def test_invalid_schema_version_does_not_create_metadata(database, target):
    before = _snapshot(database)

    with pytest.raises((ValueError, TypeError)):
        _manager(database).initialise(edition="community", target_version=target)

    assert _snapshot(database) == before


def test_missing_migration_cannot_silently_advance_schema_version(database):
    _manager(database).initialise(edition="community")
    before = _snapshot(database)

    with pytest.raises(ValueError):
        _manager(database).initialise(edition="community", target_version=2)

    assert _snapshot(database) == before


def test_failing_migration_rolls_back_all_steps_and_metadata(database):
    _manager(database).initialise(edition="community")
    before = _snapshot(database)

    def second(tx):
        tx.run("CREATE (:MigrationWitness {id:'second'})").consume()

    def third(tx):
        tx.run("CREATE (:MigrationWitness {id:'third'})").consume()
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        _manager(database).initialise(edition="community", target_version=3,
                                      migrations={2: second, 3: third})

    assert _snapshot(database) == before


def test_concurrent_initialisers_apply_each_migration_once(database):
    _manager(database).initialise(edition="community")
    barrier = Barrier(8)

    def migration(tx):
        tx.run("MERGE (w:MigrationWitness {id:'counter'}) "
               "SET w.applied=coalesce(w.applied,0)+1").consume()

    def upgrade(_):
        barrier.wait(timeout=10)
        _manager(database).initialise(edition="community", target_version=2, migrations={2: migration})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(upgrade, range(8)))

    with database.session() as session:
        counts = [row["applied"] for row in session.run(
            "MATCH (w:MigrationWitness {id:'counter'}) RETURN w.applied AS applied")]
    assert counts == [1]
    assert _metadata(database)["core_version"] == 2


def test_competing_editions_cannot_overwrite_enterprise_ownership(database):
    barrier = Barrier(8)

    def initialise(number):
        edition = "enterprise" if number % 2 else "community"
        barrier.wait(timeout=10)
        try:
            _manager(database).initialise(edition=edition)
        except ValueError:
            assert edition == "community"

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(initialise, range(8)))

    assert _metadata(database)["edition"] == "enterprise"
