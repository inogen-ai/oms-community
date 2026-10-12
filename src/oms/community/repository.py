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
        if not hasattr(store, "_workflow_depth"):
            store._workflow_depth = 0

    def atomic(self, transaction_id, tenant_id, operation, *, expected_generations=(), rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=True,
                            expected_generations=expected_generations, rollback_participants=rollback_participants)

    def metadata(self, transaction_id, tenant_id, operation, *, rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=False, metadata=True,
                            rollback_participants=rollback_participants)

    def query(self, transaction_id, tenant_id, operation):
        with self._mutex:
            return operation(self.store, self.queue)

    def atomic_with_receipt(self, transaction_id, tenant_id, operation, *, expected_generations=(), rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=True, receipt=True,
                            expected_generations=expected_generations, rollback_participants=rollback_participants)

    def _atomic(self, transaction_id, tenant_id, operation, *, track, metadata=False,
                expected_generations=(), rollback_participants=(), receipt=False):
        from oms.sources.mutation import track_changes, validate_guards, mutation_receipt
        with self._mutex:
            depth = self.store._workflow_depth
            # Metadata never changes skill content, so only source records need a copy.
            original = {name: deepcopy(value) for name, value in vars(self.store).items()
                        if "mutex" not in name and (not metadata or name == "_source_records")}
            items = deepcopy(self.queue.items)
            participants = [(item, item.snapshot_state()) for item in rollback_participants]
            try:
                self.store._workflow_depth = depth + 1
                validate_guards(self.store, tenant_id, expected_generations)
                changed = ()
                if track:
                    result, changed = track_changes(self.store, tenant_id, lambda graph: operation(graph, self.queue))
                else:
                    result = operation(self.store, self.queue)
                if receipt:
                    return mutation_receipt(self.store, tenant_id, result,
                                            (*(guard.skill for guard in expected_generations), *changed))
                return result
            except BaseException:
                for name in set(vars(self.store)) - set(original):
                    if "mutex" not in name and (not metadata or name == "_source_records"):
                        delattr(self.store, name)
                for name, value in original.items():
                    setattr(self.store, name, value)
                self.queue.items = items
                for participant, state in reversed(participants):
                    participant.restore_state(state)
                raise
            finally:
                self.store._workflow_depth = depth

    def atomic_skill_change(self, request, factory, operation):
        from oms.sources.mutation import conditional_change
        return self._atomic(request.operation_id, request.tenant_id,
                            lambda graph, reviews: conditional_change(graph, reviews, request, factory, operation),
                            track=False)

    def transactions(self, tenant_id, limit=1000):
        return sorted((deepcopy(t) for t in self.store.transactions.values() if t.tenant_id == tenant_id),
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

    def _session(self):
        """A session chained on the driver's shared bookmarks.

        On a routed cluster a read then sees every write committed through this
        driver before it, whichever repository instance wrote it.
        """
        manager = getattr(self.driver, "execute_query_bookmark_manager", None)
        return self.driver.session(bookmark_manager=manager) if manager is not None else self.driver.session()

    def atomic(self, transaction_id, tenant_id, operation, *, expected_generations=(), rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=True,
                            expected_generations=expected_generations, rollback_participants=rollback_participants)

    def metadata(self, transaction_id, tenant_id, operation, *, rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=False, metadata=True,
                            rollback_participants=rollback_participants)

    def query(self, transaction_id, tenant_id, operation):
        from copy import copy

        def run(tx):
            graph, reviews = copy(self.store), copy(self.queue)
            graph._driver = reviews._driver = _TransactionDriver(tx)
            return operation(graph, reviews)

        with self._session() as session:
            return session.execute_read(run)

    def atomic_with_receipt(self, transaction_id, tenant_id, operation, *, expected_generations=(), rollback_participants=()):
        return self._atomic(transaction_id, tenant_id, operation, track=True, receipt=True,
                            expected_generations=expected_generations, rollback_participants=rollback_participants)

    def _atomic(self, transaction_id, tenant_id, operation, *, track, metadata=False,
                expected_generations=(), rollback_participants=(), receipt=False):
        from copy import copy
        from oms.sources.mutation import track_changes, validate_guards, mutation_receipt

        participants = [(item, item.snapshot_state()) for item in rollback_participants]

        def run(tx):
            for participant, state in participants:
                participant.restore_state(state)
            if not metadata:
                # Global event identity is reserved before touching its payload;
                # different tenants cannot race to overwrite one event ID.
                # Metadata names no event, so it does not serialise on one.
                tx.run("MERGE (c:ContributionLock {id:$id}) SET c.locked=true REMOVE c.locked",
                       id=transaction_id).consume()
            # One workspace lock also serialises reinforcement of the same
            # rule by different transactions. Core schema makes this unique.
            tx.run("MERGE (w:WorkflowLock {tenant_id:$tenant}) "
                   "SET w.locked=true REMOVE w.locked", tenant=tenant_id).consume()
            proxy = _TransactionDriver(tx)
            graph, reviews = copy(self.store), copy(self.queue)
            graph._driver = reviews._driver = proxy
            validate_guards(graph, tenant_id, expected_generations)
            changed = ()
            if track:
                result, changed = track_changes(graph, tenant_id, lambda recorded: operation(recorded, reviews))
            else:
                result = operation(graph, reviews)
            if receipt:
                return mutation_receipt(graph, tenant_id, result,
                                        (*(guard.skill for guard in expected_generations), *changed))
            return result

        try:
            with self._session() as session:
                return session.execute_write(run)
        except BaseException:
            for participant, state in reversed(participants):
                participant.restore_state(state)
            raise

    def atomic_skill_change(self, request, factory, operation):
        from oms.sources.mutation import conditional_change
        return self._atomic(request.operation_id, request.tenant_id,
                            lambda graph, reviews: conditional_change(graph, reviews, request, factory, operation),
                            track=False)

    def transactions(self, tenant_id, limit=1000):
        with self._session() as session:
            ids = [row["id"] for row in session.run(
                "MATCH (t:Transaction {tenant_id:$tenant}) "
                "RETURN t.id AS id ORDER BY t.timestamp,t.id LIMIT $limit",
                tenant=tenant_id, limit=limit)]
        return [self.store.get_transaction(tid) for tid in ids]

    def tenants(self):
        with self._session() as session:
            return [row["tenant"] for row in session.run(
                "MATCH (t:Transaction) RETURN DISTINCT t.tenant_id AS tenant ORDER BY tenant")]

    def undisposed(self, tenant_id, limit=1000, adopt=frozenset()):
        with self._session() as session:
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
