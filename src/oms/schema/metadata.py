"""Validate database ownership before applying the core schema or migrations."""
from collections.abc import Callable, Mapping
from packaging.version import InvalidVersion, Version

CORE_VERSION = "1.4.1"
SCHEMA_VERSION = 1


class SchemaCompatibilityError(ValueError):
    pass


class SchemaManager:
    def __init__(self, driver):
        self.driver = driver

    @staticmethod
    def _check(metadata, edition, target_version, private_version=None):
        if not metadata:
            return
        owner = metadata.get("managed_edition", metadata.get("edition"))
        if ("edition" in metadata and "managed_edition" in metadata
                and metadata["edition"] != metadata["managed_edition"]):
            raise SchemaCompatibilityError("database edition markers disagree")
        if owner not in ("community", "enterprise"):
            raise SchemaCompatibilityError("database edition marker is invalid")
        if edition == "community" and owner != "community":
            raise SchemaCompatibilityError("Community cannot open an Enterprise-managed database for writing")
        versions = [metadata.get(k, 0) for k in ("core_version", "schema_version")]
        if any(type(v) is not int or v < 0 or v > target_version for v in versions):
            raise SchemaCompatibilityError("database requires a newer schema; downgrade is unsupported")
        if ("core_version" in metadata and "schema_version" in metadata
                and metadata["core_version"] != metadata["schema_version"]):
            raise SchemaCompatibilityError("database schema markers disagree")
        private = metadata.get("private_version", 0)
        if type(private) is not int or private < 0 or (owner == "community" and private != 0):
            raise SchemaCompatibilityError("invalid private schema marker")
        if private_version is not None and private > private_version:
            raise SchemaCompatibilityError("database requires a newer private schema; downgrade is unsupported")
        if not isinstance(metadata.get("minimum_core_version", "0"), str):
            raise SchemaCompatibilityError("invalid minimum core version")
        try:
            minimum = Version(str(metadata.get("minimum_core_version", "0")))
        except InvalidVersion as exc:
            raise SchemaCompatibilityError("invalid minimum core version") from exc
        if minimum > Version(CORE_VERSION):
            raise SchemaCompatibilityError("database requires a newer core release")

    def initialise(self, edition="community", target_version=SCHEMA_VERSION,
                   migrations: Mapping[int, Callable] | None = None, *,
                   private_version: int | None = None):
        if edition not in ("community", "enterprise"):
            raise ValueError("unknown database edition")
        if type(target_version) is not int or target_version < 1:
            raise ValueError("target schema version must be a positive integer")
        if private_version is not None and (type(private_version) is not int or private_version < 1):
            raise ValueError("private schema version must be a positive integer")
        migrations = dict(migrations or {})
        # This read-only preflight precedes even CREATE CONSTRAINT. A refused
        # startup must not alter the database it was accidentally pointed at.
        with self.driver.session() as session:
            rec = session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS metadata").single()
            metadata = rec["metadata"] if rec else None
            self._check(metadata, edition, target_version, private_version)
            if metadata is None and edition == "community":
                if session.run("MATCH (n) RETURN count(n) AS count").single()["count"]:
                    raise SchemaCompatibilityError("unmarked nonempty database requires an Enterprise migration or a fresh Community database")

        from oms.adapters.neo4j import schema
        with self.driver.session() as session:
            session.run("CREATE CONSTRAINT oms_metadata_id IF NOT EXISTS FOR (m:OMSMetadata) REQUIRE m.id IS UNIQUE").consume()
            for statement in schema.CORE_STATEMENTS:
                session.run(statement).consume()

        def migrate(tx):
            # The uniqueness constraint serialises first-time initialisers;
            # the write lock is acquired before reading the current version.
            tx.run("MERGE (m:OMSMetadata {id:'core'}) SET m._migration_lock=true REMOVE m._migration_lock").consume()
            record = tx.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS metadata").single()
            current = dict(record["metadata"])
            if "edition" in current or "managed_edition" in current:
                self._check(current, edition, target_version, private_version)
            version = current.get("core_version", current.get("schema_version", 0))
            for destination in range(version + 1, target_version + 1):
                migration = migrations.get(destination)
                if migration is None and destination > SCHEMA_VERSION:
                    raise SchemaCompatibilityError(f"migration to schema {destination} is missing")
                if migration is not None:
                    migration(tx)
            # No version changes escape a failing callback. Upgrading ownership
            # also commits with the data migration; it cannot downgrade later.
            managed_private_version = max(current.get("private_version", 0), 1 if edition == "enterprise" else 0)
            tx.run("MATCH (m:OMSMetadata {id:'core'}) "
                   "SET m.edition=$edition, m.managed_edition=$edition, "
                   "m.core_version=$version, m.schema_version=$version, "
                   "m.private_version=$private, m.minimum_core_version=$minimum, "
                   "m.last_successful_migration=$last "
                   "RETURN m", edition=edition, version=target_version,
                   private=managed_private_version, minimum=CORE_VERSION,
                   last=f"core-{target_version}").consume()
            return {"edition": edition, "core_version": target_version,
                    "private_version": managed_private_version, "schema_version": target_version,
                    "managed_edition": edition, "minimum_core_version": CORE_VERSION,
                    "last_successful_migration": f"core-{target_version}"}
        with self.driver.session() as session:
            return session.execute_write(migrate)
