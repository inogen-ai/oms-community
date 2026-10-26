"""Validate database ownership before applying the core schema or migrations."""
from collections.abc import Callable, Mapping
from packaging.version import InvalidVersion, Version

CORE_VERSION = "1.7.0"
SCHEMA_VERSION = 3
# The rule split's stages (schema 2 to 3). Named apart from the source
# identity stages, because that step runs first on every start and must
# never mistake a graph in the middle of the split for its own (plan D5).
RULE_SPLIT_STAGES = ("rule_split_fenced", "rule_splitting", "rule_split_verified")


class SchemaCompatibilityError(ValueError):
    pass


class SchemaManager:
    def __init__(self, driver):
        self.driver = driver

    @staticmethod
    def _check(metadata, edition, target_version, private_version=None, *, maintenance=False):
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
        if not maintenance and (metadata.get("migration_state") not in (None, "ready")
                                or (max(versions) >= 2 and metadata.get("migration_state") != "ready")):
            if metadata.get("migration_state") in RULE_SPLIT_STAGES:
                raise SchemaCompatibilityError("rule split maintenance is incomplete")
            raise SchemaCompatibilityError("source identity maintenance migration is incomplete")
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

    @staticmethod
    def _refuse_unmaintained(metadata) -> None:
        """Refuse a graph that still needs a maintenance step before this binary
        may open it: source identity below schema 2, the rule split at 2."""
        version = metadata.get("schema_version", metadata.get("core_version", 0))
        if version < 2:
            raise SchemaCompatibilityError("schema 2 requires explicit source identity maintenance")
        if version == 2:
            raise SchemaCompatibilityError("schema 3 requires the rule split maintenance; "
                                           "run SchemaManager.split_rules first")

    def initialise(self, edition="community", target_version=SCHEMA_VERSION,
                   migrations: Mapping[int, Callable] | None = None, *,
                   private_version: int | None = None):
        if edition not in ("community", "enterprise"):
            raise ValueError("unknown database edition")
        if type(target_version) is not int or target_version < 1:
            raise ValueError("target schema version must be a positive integer")
        if private_version is not None and (type(private_version) is not int or private_version < 1):
            raise ValueError("private schema version must be a positive integer")
        if target_version < SCHEMA_VERSION:
            raise SchemaCompatibilityError(f"this binary requires schema {SCHEMA_VERSION} or newer")
        migrations = dict(migrations or {})
        # This read-only preflight precedes even CREATE CONSTRAINT. A refused
        # startup must not alter the database it was accidentally pointed at.
        with self.driver.session() as session:
            rec = session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS metadata").single()
            metadata = rec["metadata"] if rec else None
            self._check(metadata, edition, target_version, private_version)
            if metadata is not None:
                self._refuse_unmaintained(metadata)
            if metadata is None and edition == "enterprise":
                owned = session.run("MATCH (n) WHERE n:Skill OR n:Section OR n:ContentBlock "
                                    "OR n:Example OR n:Artefact OR n:SkillVersion RETURN count(n) AS n").single()["n"]
                if owned:
                    raise SchemaCompatibilityError("legacy custody requires explicit source identity maintenance")
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
            # Read again under the lock: a graph that gained a version after the
            # preflight is refused the same way, so no path bumps it unmaintained.
            if "core_version" in current or "schema_version" in current:
                self._refuse_unmaintained(current)
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
                   "m.last_successful_migration=$last, m.migration_state='ready' "
                   "RETURN m", edition=edition, version=target_version,
                   private=managed_private_version, minimum=CORE_VERSION,
                   last=f"core-{target_version}").consume()
            return {"edition": edition, "core_version": target_version,
                    "private_version": managed_private_version, "schema_version": target_version,
                    "managed_edition": edition, "minimum_core_version": CORE_VERSION,
                    "last_successful_migration": f"core-{target_version}", "migration_state": "ready"}
        with self.driver.session() as session:
            return session.execute_write(migrate)

    def migrate_source_identity(self, *, old_writers_stopped: bool,
                                edition="community", private_version: int | None = None):
        """Resume staged schema-2 maintenance; normal startup never runs DDL migration."""
        from oms.schema.migrations.source_identity import migrate
        return migrate(self, old_writers_stopped=old_writers_stopped,
                       edition=edition, private_version=private_version)

    def split_rules(self, *, old_writers_stopped: bool, edition: str = "community",
                    private_version: int | None = None) -> dict | None:
        """Resume the schema 3 rule split (one rule, one skill). One read, and
        nothing written, on a graph already split, on an empty graph and on a
        graph the source identity step has not finished."""
        # Imported here, as the source identity step is: the migration imports
        # this module, so a module-level import would be a cycle.
        from oms.schema.migrations.rule_split import migrate
        return migrate(self, old_writers_stopped=old_writers_stopped, edition=edition,
                       private_version=private_version)
