"""Source records as Neo4j nodes, written in the workflow's own transaction.

Each record is one `SourceRecord` node keyed by tenant, kind and id; a write takes
the tenant's workflow lock first so it serialises with every other write.
"""
from oms.community.repository import _TransactionDriver
from oms.sources.repository import SourceRecords, record_key


SOURCE_CONSTRAINT = (
    "CREATE CONSTRAINT source_record_key IF NOT EXISTS "
    "FOR (n:SourceRecord) REQUIRE n.storage_key IS UNIQUE"
)


class Neo4jSourceRepository(SourceRecords):
    def __init__(self, driver, *, bound=False, workflow_bound=None):
        self._driver = driver
        self._bound = bound or isinstance(driver, _TransactionDriver)
        self._workflow_bound = isinstance(driver, _TransactionDriver) if workflow_bound is None else workflow_bound

    def _tracks_content(self):
        return self._workflow_bound

    def ensure_schema(self):
        if self._bound:
            raise RuntimeError("Source schema must be initialized outside a data transaction")
        with self._driver.session() as session:
            session.run(SOURCE_CONSTRAINT).consume()
            session.run("CREATE INDEX source_record_scope IF NOT EXISTS "
                        "FOR (n:SourceRecord) ON (n.tenant_id,n.kind,n.record_id)").consume()
            session.run("CREATE CONSTRAINT workflow_lock IF NOT EXISTS "
                        "FOR (n:WorkflowLock) REQUIRE n.tenant_id IS UNIQUE").consume()

    def _atomic(self, tenant_id, operation):
        # Setting and removing a property is how Cypher takes a write lock on
        # a node without changing it. The tenant's WorkflowLock node is the
        # same one the graph store's workflow transaction locks, so a source
        # write made on its own still serialises with every other write.
        def run(tx):
            tx.run("MERGE (n:WorkflowLock {tenant_id:$tenant}) "
                   "SET n.locked=true REMOVE n.locked", tenant=tenant_id).consume()
            return operation(type(self)(_TransactionDriver(tx), bound=True, workflow_bound=self._workflow_bound))
        with self._driver.session() as session:
            return session.execute_write(run)

    def _get(self, tenant, kind, key):
        with self._driver.session() as session:
            row = session.run("MATCH (n:SourceRecord {storage_key:$key}) RETURN n.value AS value",
                              key=record_key(tenant, kind, key)).single()
            return row["value"] if row else None

    def _put(self, tenant, kind, key, value):
        with self._driver.session() as session:
            session.run("MERGE (n:SourceRecord {storage_key:$storage}) "
                        "SET n.tenant_id=$tenant,n.kind=$kind,n.record_id=$key,n.value=$value",
                        storage=record_key(tenant, kind, key), tenant=tenant, kind=kind,
                        key=key, value=value).consume()

    def _delete(self, tenant, kind, key):
        with self._driver.session() as session:
            session.run("MATCH (n:SourceRecord {storage_key:$key}) DELETE n",
                        key=record_key(tenant, kind, key)).consume()

    def _rows(self, tenant, kind, *, prefix=""):
        with self._driver.session() as session:
            return [(row["key"], row["value"]) for row in session.run(
                "MATCH (n:SourceRecord {tenant_id:$tenant,kind:$kind}) WHERE n.record_id STARTS WITH $prefix "
                "RETURN n.record_id AS key,n.value AS value ORDER BY n.record_id",
                tenant=tenant, kind=kind, prefix=prefix)]

    def lock_skills(self, skills):
        super().lock_skills(skills)
        # A lock outside a transaction would be released before the write it
        # protects; the base class has already checked the order that keeps
        # two overlapping lock sets from deadlocking.
        if not self._bound:
            raise RuntimeError("Skill locks require the workflow transaction")
        with self._driver.session() as session:
            for ref in skills:
                session.run("MERGE (n:SourceRecord {storage_key:$key}) "
                            "SET n.locked=true REMOVE n.locked",
                            key=record_key(ref.tenant_id, "lock", ref.skill_id)).consume()
