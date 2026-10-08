"""Resumable schema-2 maintenance with separate DDL and atomic data stages."""
import json

from oms.adapters.neo4j import schema
from oms.domain.identity import SkillRef
from oms.schema.metadata import CORE_VERSION, SchemaCompatibilityError

OWNED_LABELS = ("Skill", "Section", "ContentBlock", "Example", "SkillVersion")


def _metadata(runner):
    row = runner.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS metadata").single()
    return dict(row["metadata"]) if row else None


def _set_stage(driver, expected, stage):
    with driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) WHERE m.migration_state=$expected SET m.migration_state=$stage",
                    expected=expected, stage=stage).consume()


def _ddl(driver, statements):
    for statement in statements:
        with driver.session() as session:
            session.run(statement).consume()


def _identity(props):
    tenant, identifier = props.get("tenant_id"), props.get("id")
    if not isinstance(tenant, str) or not tenant or not isinstance(identifier, str) or not identifier:
        raise SchemaCompatibilityError("missing source custody ownership or public identity")
    return SkillRef(tenant, identifier).storage_key


def _validate_ownership(tx):
    rows = list(tx.run("MATCH (n) WHERE any(label IN labels(n) WHERE label IN $labels) "
                       "RETURN elementId(n) AS element, labels(n) AS labels, properties(n) AS props",
                       labels=list(OWNED_LABELS) + ["Artefact"]))
    for row in rows:
        _identity(row["props"])
    contradictory = tx.run(
        "MATCH (a)-[r]->(b) WHERE "
        "(any(label IN labels(a) WHERE label IN $owned) OR any(label IN labels(b) WHERE label IN $owned)) "
        "AND NOT a:Tag AND NOT b:Tag "
        "AND any(label IN labels(a) WHERE label IN $core) "
        "AND any(label IN labels(b) WHERE label IN $core) "
        "AND (a.tenant_id IS NULL OR b.tenant_id IS NULL OR a.tenant_id <> b.tenant_id) "
        "RETURN count(r) AS n", owned=list(OWNED_LABELS)+["Artefact"],
        core=list(OWNED_LABELS)+["Artefact", "Rule", "Transaction", "Constraint", "Learning"]).single()["n"]
    if contradictory:
        raise SchemaCompatibilityError("contradictory source custody ownership")
    invalid_sections = tx.run(
        "MATCH (s:Section) WHERE s.skill_id IS NULL OR NOT EXISTS { "
        "MATCH (:Skill {id:s.skill_id,tenant_id:s.tenant_id})-[:HAS_SECTION]->(s) } "
        "OR EXISTS { MATCH (owner:Skill)-[:HAS_SECTION]->(s) WHERE owner.id <> s.skill_id } "
        "RETURN count(s) AS n").single()["n"]
    if invalid_sections:
        raise SchemaCompatibilityError("section ownership is missing or contradictory")
    invalid_versions = tx.run(
        "MATCH (s:Skill)-[:HAS_VERSION]->(v:SkillVersion) WHERE s.id <> v.skill_id "
        "RETURN count(v) AS n").single()["n"]
    if invalid_versions:
        raise SchemaCompatibilityError("history ownership contradicts its skill reference")
    occurrences = set()
    for file in tx.run("MATCH (s:Skill)-[r:HAS_ARTEFACT]->(a:Artefact) "
                       "RETURN s.tenant_id AS tenant,s.id AS skill,r.path AS path"):
        path = file["path"]
        if (not isinstance(path,str) or not path or path.startswith("/") or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise SchemaCompatibilityError("file occurrence ownership has an invalid path")
        key = (file["tenant"],file["skill"],path.casefold())
        if key in occurrences:
            raise SchemaCompatibilityError("duplicate file occurrence ownership paths")
        occurrences.add(key)
    examples = [row for row in rows if "Example" in row["labels"]]
    for row in examples:
        props = row["props"]
        parents = [(label, props.get(field)) for label, field in (
            ("Section", "parent_section_id"), ("Rule", "parent_rule_id"),
            ("Skill", "parent_skill_id")) if props.get(field) is not None]
        if not parents:
            raise SchemaCompatibilityError("example ownership requires a declared parent")
        actual = list(tx.run("MATCH (e:Example) WHERE elementId(e)=$element "
                             "MATCH (e)-[:HAS_EXAMPLE|ILLUSTRATES]-(p) "
                             "RETURN DISTINCT elementId(p) AS parent", element=row["element"]))
        if len(actual) != 1:
            raise SchemaCompatibilityError("example ownership is missing or contradictory")
        if parents:
            # Legacy writes selected section before rule before fallback skill.
            # Preserve the remaining fields as context, not additional owners.
            label, identifier = parents[0]
            count = tx.run(f"MATCH (p:{label} {{id:$id,tenant_id:$tenant}})-[:HAS_EXAMPLE]->(e:Example) "
                           "WHERE elementId(e)=$element RETURN count(p) AS n",
                           id=identifier,tenant=props["tenant_id"],element=row["element"]).single()["n"]
            if count != 1:
                raise SchemaCompatibilityError("example ownership is missing or contradictory")
    return rows


def migrate_data(tx):
    """Validate before changing keys; a failed data stage leaves legacy data intact."""
    rows = _validate_ownership(tx)
    for row in rows:
        if "Artefact" in row["labels"]:
            continue
        props = row["props"]
        tx.run("MATCH (n) WHERE elementId(n)=$element SET n.storage_key=$key",
               element=row["element"], key=_identity(props)).consume()
    # A legacy digest node may be shared. Copy occurrence metadata per owning
    # skill/path and preserve the legacy node for any historical references.
    artefacts = list(tx.run(
        "MATCH (s:Skill)-[r:HAS_ARTEFACT]->(a:Artefact) "
        "RETURN elementId(s) AS skill, s.id AS skill_id, s.tenant_id AS tenant, "
        "elementId(a) AS artefact, properties(a) AS props, properties(r) AS edge"))
    for row in artefacts:
        path = row["edge"].get("path")
        if not isinstance(path, str) or not path:
            raise SchemaCompatibilityError("file occurrence ownership lacks a path")
        key = json.dumps([row["tenant"], row["skill_id"], path], ensure_ascii=False, separators=(",", ":"))
        # Drop the old global artefact label inside the data transaction so two
        # occurrences can retain the same public digest ID before DDL cleanup.
        tx.run("MATCH (a) WHERE elementId(a)=$element SET a:LegacyArtefact REMOVE a:Artefact",
               element=row["artefact"]).consume()
        tx.run("MATCH (s:Skill) WHERE elementId(s)=$skill "
               "MERGE (a:ArtefactOccurrence {storage_key:$key}) SET a += $props, a:Entity, "
               "a.storage_key=$key, a.owner_skill_id=$skill_id, a.occurrence_path=$path "
               "WITH s,a MATCH (s)-[old:HAS_ARTEFACT]->(legacy:LegacyArtefact) "
               "WHERE elementId(legacy)=$legacy AND old.path=$path DELETE old "
               "MERGE (s)-[r:HAS_ARTEFACT]->(a) SET r += $edge",
               skill=row["skill"], key=key, props=row["props"], skill_id=row["skill_id"],
               path=path, legacy=row["artefact"], edge=row["edge"]).consume()
    references = list(tx.run(
        "MATCH (s:Skill)-[r:HAS_REFERENCE]->(legacy:LegacyArtefact) "
        "RETURN elementId(s) AS skill, elementId(legacy) AS legacy, properties(r) AS props"))
    for reference in references:
        choices = [row for row in artefacts if row["skill"] == reference["skill"]
                   and row["artefact"] == reference["legacy"]]
        if len(choices) != 1:
            raise SchemaCompatibilityError("ambiguous file reference ownership")
        row = choices[0]
        key = json.dumps([row["tenant"],row["skill_id"],row["edge"]["path"]],
                         ensure_ascii=False,separators=(",", ":"))
        tx.run("MATCH (s:Skill)-[old:HAS_REFERENCE]->(legacy:LegacyArtefact) "
               "WHERE elementId(s)=$skill AND elementId(legacy)=$legacy "
               "MATCH (a:ArtefactOccurrence {storage_key:$key}) DELETE old "
               "MERGE (s)-[r:HAS_REFERENCE]->(a) SET r += $props",
               skill=row["skill"],legacy=row["artefact"],key=key,props=reference["props"]).consume()
    # Unattached legacy blobs remain readable historical custody, not live occurrences.
    tx.run("MATCH (a:Artefact) WHERE a.storage_key IS NULL "
           "SET a:LegacyArtefact REMOVE a:Artefact").consume()


def validate_ready(tx):
    _validate_ownership(tx)
    missing = tx.run("MATCH (n) WHERE any(label IN labels(n) WHERE label IN $labels) "
                     "AND n.storage_key IS NULL RETURN count(n) AS n",
                     labels=list(OWNED_LABELS)+["Artefact"]).single()["n"]
    if missing:
        raise SchemaCompatibilityError("source identity migration has incomplete keys")


def migrate(manager, *, old_writers_stopped, edition, private_version):
    if old_writers_stopped is not True:
        raise SchemaCompatibilityError("incompatible writers must be stopped before maintenance")
    if edition not in ("community", "enterprise"):
        raise ValueError("unknown database edition")
    if private_version is not None and (type(private_version) is not int or private_version < 1):
        raise ValueError("private schema version must be a positive integer")
    with manager.driver.session() as session:
        metadata = _metadata(session)
        manager._check(metadata, edition, 2, private_version, maintenance=True)
        if metadata and metadata.get("migration_state") not in (
                None, "ready", "fenced", "additive_ready", "data_ready", "constraints_ready"):
            raise SchemaCompatibilityError("unknown source identity maintenance stage")
        if metadata and metadata.get("schema_version") == 2 and metadata.get("migration_state") == "ready":
            return metadata
        if metadata is None and edition == "community":
            raise SchemaCompatibilityError("unmarked database requires an Enterprise migration")
    _ddl(manager.driver, ["CREATE CONSTRAINT oms_metadata_id IF NOT EXISTS FOR (m:OMSMetadata) REQUIRE m.id IS UNIQUE"])

    def fence(tx):
        tx.run("MERGE (m:OMSMetadata {id:'core'}) SET m._migration_lock=true REMOVE m._migration_lock").consume()
        current = _metadata(tx)
        if current.get("schema_version") == 2 and current.get("migration_state") == "ready":
            return current
        if current.get("edition"):
            manager._check(current, edition, 2, private_version, maintenance=True)
        tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=2,m.schema_version=2,"
               "m.edition=$edition,m.managed_edition=$edition,m.private_version=$private,"
               "m.minimum_core_version=$minimum,m.migration_state=$stage",
               edition=edition, private=max(current.get("private_version",0), 1 if edition == "enterprise" else 0),
               minimum=CORE_VERSION, stage=(current.get("migration_state")
                   if current.get("migration_state") not in (None, "ready") else "fenced")).consume()
        return _metadata(tx)

    with manager.driver.session() as session:
        metadata = session.execute_write(fence)
    stage = metadata["migration_state"]
    if stage == "ready":
        return metadata
    if stage == "fenced":
        _ddl(manager.driver, schema.CORE_STATEMENTS)
        _set_stage(manager.driver, "fenced", "additive_ready")
        with manager.driver.session() as session:
            stage = _metadata(session)["migration_state"]
    if stage == "additive_ready":
        with manager.driver.session() as session:
            def data(tx):
                tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m._migration_lock=true REMOVE m._migration_lock").consume()
                if _metadata(tx)["migration_state"] != "additive_ready":
                    return
                migrate_data(tx)
                tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state='data_ready'").consume()
            session.execute_write(data)
        with manager.driver.session() as session:
            stage = _metadata(session)["migration_state"]
    if stage == "data_ready":
        _ddl(manager.driver, [f"DROP CONSTRAINT {name} IF EXISTS" for name in schema.OBSOLETE_ID_CONSTRAINTS])
        _set_stage(manager.driver, "data_ready", "constraints_ready")
        with manager.driver.session() as session:
            stage = _metadata(session)["migration_state"]
    if stage == "ready":
        with manager.driver.session() as session:
            return _metadata(session)
    if stage != "constraints_ready":
        raise SchemaCompatibilityError("unknown source identity maintenance stage")
    with manager.driver.session() as session:
        def finish(tx):
            tx.run("MATCH (a:ArtefactOccurrence) SET a:Artefact").consume()
            validate_ready(tx)
            tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state='ready',"
                   "m.last_successful_migration='core-2'").consume()
            return _metadata(tx)
        return session.execute_write(finish)
