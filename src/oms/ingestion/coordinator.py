"""Persist, then complete or recover the edition's explicit disposition."""
from oms.domain.types import SignalType
from oms.ingestion.service import SanitisationError
from oms.ports.workflow import effective_state


class ContributionCoordinator:
    def __init__(self, ingestion, disposition, repository):
        if disposition is None or not callable(getattr(disposition, "accept", None)):
            raise ValueError("a transaction disposition is required")
        self.ingestion, self.disposition, self.repository = ingestion, disposition, repository

    def ingest(self, payload, principal):
        if payload.signal_type is SignalType.SKILL_IMPORT:
            raise ValueError("skill imports must use the trusted import command")
        # The same ID always completes the second write, including when the
        # first request persisted successfully and then lost its connection.
        def persist(store, queue):
            try:
                txn = self.ingestion.in_transaction(store).ingest(payload, principal)
            except SanitisationError as exc:
                # Commit the safe quarantine record before reporting refusal.
                # No partially sanitised transaction is written by ingestion.
                return exc
            if txn.workflow_state is None and effective_state(txn, store) == "received":
                txn.workflow_state = "received"
                store.upsert_transaction(txn)
            return txn
        transaction = self.repository.atomic(payload.transaction_id, principal.tenant_id, persist)
        if isinstance(transaction, SanitisationError):
            raise transaction
        return self._verified(transaction.id, self.disposition.accept(transaction))

    def _verified(self, transaction_id, result):
        stored = self.repository.store.get_transaction(transaction_id)
        # A reviewer can legitimately decide the item between the disposition
        # write and this read, so any advance beyond `received` proves the
        # disposition acted; only a still-received transaction is a failure.
        if (result is None or result.state == "received"
                or effective_state(stored, self.repository.store) == "received"):
            raise RuntimeError("transaction disposition did not produce an actionable state")
        return stored

    def _adoptable(self):
        return frozenset(getattr(self.disposition, "adoptable_states", ()) or ())

    def reconcile(self, tenant_id, limit=1000):
        adopt = self._adoptable()
        repaired = 0
        for transaction in self.repository.undisposed(tenant_id, limit, adopt=adopt):
            if transaction.signal_type is SignalType.SKILL_IMPORT:
                continue
            state = effective_state(transaction, self.repository.store)
            if state in ("received", "held_safety", "awaiting_manual_review") or state in adopt:
                self._verified(transaction.id, self.disposition.accept(transaction))
                repaired += 1
        return repaired

    def reconcile_all(self, limit=1000):
        return sum(self.reconcile(tenant, limit) for tenant in self.repository.tenants())
