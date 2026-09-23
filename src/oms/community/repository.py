"""Atomic workflow adapters over the existing graph and review repositories."""
from contextlib import nullcontext
from copy import deepcopy
from threading import RLock


class MemoryWorkflowRepository:
    def __init__(self, store, queue):
        self.store, self.queue = store, queue
        if not hasattr(store, "_workflow_mutex"):
            store._workflow_mutex = RLock()
        self._mutex = store._workflow_mutex

    def atomic(self, transaction_id, tenant_id, operation):
        with self._mutex:
            original = {name: deepcopy(value) for name, value in vars(self.store).items()
                        if "mutex" not in name}
            items = deepcopy(self.queue.items)
            try:
                return operation(self.store, self.queue)
            except BaseException:
                for name, value in original.items():
                    setattr(self.store, name, value)
                self.queue.items = items
                raise

    def transactions(self, tenant_id, limit=1000):
        return sorted((t for t in self.store.transactions.values() if t.tenant_id == tenant_id),
                      key=lambda t: (t.timestamp, t.id))[:limit]

    def tenants(self):
        return sorted({t.tenant_id for t in self.store.transactions.values()})

    def undisposed(self, tenant_id, limit=1000, adopt=frozenset()):
        from oms.domain.types import SignalType
        from oms.ports.workflow import TERMINAL_STATES, effective_state
        rows = []
        for txn in self.transactions(tenant_id, limit=2**31 - 1):
            if txn.signal_type is SignalType.SKILL_IMPORT:
                continue
            if effective_state(txn, self.store) in TERMINAL_STATES:
                continue
            missing_review = (self.queue.get(f"manual-{txn.id}") is None
                              and self.queue.get(f"review-held-{txn.id}") is None
                              and self.queue.get(f"review-unbound-{txn.id}") is None
                              and self.queue.get(f"review-machine-{txn.id}") is None)
            # The branch tests mirror the Neo4j predicate on the raw property:
            # a record without an explicit state predates the workflow and is
            # always the coordinator's to adopt, and `adopt` lets an edition's
            # disposition reclaim states another edition wrote and abandoned.
            if (txn.workflow_state is None or txn.workflow_state == "received"
                    or txn.workflow_state in adopt
                    or (txn.workflow_state in ("held_safety", "awaiting_manual_review")
                        and missing_review)):
                rows.append(txn)
                if len(rows) >= limit:
                    break
        return rows


class _TransactionSession:
    """Adapt existing repository methods to one explicit Neo4j transaction."""
    def __init__(self, transaction):
        self.transaction = transaction

    def run(self, *args, **kwargs):
        return self.transaction.run(*args, **kwargs)

    def execute_write(self, operation, *args, **kwargs):
        return operation(self.transaction, *args, **kwargs)

    execute_read = execute_write


class _TransactionDriver:
    def __init__(self, transaction):
        self.transaction = transaction

    def session(self, **kwargs):
        return nullcontext(_TransactionSession(self.transaction))


class Neo4jWorkflowRepository:
    def __init__(self, driver, store, queue):
        self.driver, self.store, self.queue = driver, store, queue

    def atomic(self, transaction_id, tenant_id, operation):
        from oms.adapters.neo4j.store import Neo4jGraphStore
        from oms.adapters.neo4j.review_queue import Neo4jReviewQueue

        def run(tx):
            # Global event identity is reserved before touching its payload;
            # different tenants cannot race to overwrite one event ID.
            tx.run("MERGE (c:ContributionLock {id:$id}) SET c.locked=true REMOVE c.locked",
                   id=transaction_id).consume()
            # One workspace lock also serialises reinforcement of the same
            # rule by different transactions. Core schema makes this unique.
            tx.run("MERGE (w:WorkflowLock {tenant_id:$tenant}) "
                   "SET w.locked=true REMOVE w.locked", tenant=tenant_id).consume()
            proxy = _TransactionDriver(tx)
            return operation(Neo4jGraphStore(proxy), Neo4jReviewQueue(proxy))

        with self.driver.session() as session:
            return session.execute_write(run)

    def transactions(self, tenant_id, limit=1000):
        with self.driver.session() as session:
            ids = [row["id"] for row in session.run(
                "MATCH (t:Transaction {tenant_id:$tenant}) "
                "RETURN t.id AS id ORDER BY t.timestamp,t.id LIMIT $limit",
                tenant=tenant_id, limit=limit)]
        return [self.store.get_transaction(tid) for tid in ids]

    def tenants(self):
        with self.driver.session() as session:
            return [row["tenant"] for row in session.run(
                "MATCH (t:Transaction) RETURN DISTINCT t.tenant_id AS tenant ORDER BY tenant")]

    def undisposed(self, tenant_id, limit=1000, adopt=frozenset()):
        with self.driver.session() as session:
            ids = [row["id"] for row in session.run(
                "MATCH (t:Transaction {tenant_id:$tenant}) "
                "WHERE t.signal_type <> 'skill_import' AND NOT (:Rule)-[:DERIVED_FROM]->(t) "
                "AND coalesce(t.compile_status,'') <> 'failed' AND "
                "(t.workflow_state IS NULL OR t.workflow_state='received' OR t.workflow_state IN $adopt OR "
                "(t.workflow_state IN ['held_safety','awaiting_manual_review'] "
                "AND NOT EXISTS { MATCH (r:ReviewItem) WHERE r.id IN ['manual-'+t.id,'review-held-'+t.id,'review-unbound-'+t.id,'review-machine-'+t.id] })) "
                "RETURN t.id AS id ORDER BY t.timestamp,t.id LIMIT $limit",
                tenant=tenant_id, limit=limit, adopt=sorted(adopt))]
        return [self.store.get_transaction(tid) for tid in ids]
