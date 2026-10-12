"""Tenant fences around the collaborators a source write is given.

Every graph, queue, source, history and event call made during a write passes through
`ScopedCollaborator`, which refuses any argument, looked-up record or result that
belongs to another tenant. `with_admission` is how an edition adds its own checks to
the admission step without the fences knowing about it.
"""
from dataclasses import fields, is_dataclass, replace
from copy import copy, deepcopy
from functools import wraps
from inspect import signature

from oms.domain.identity import SkillRef
from oms.ports.mutation import MutationContext
from oms.sources.errors import SourceForbidden
from oms.sources.models import Record


def _check_value(value, tenant_id: str) -> None:
    if isinstance(value, (Record, SkillRef)) or is_dataclass(value):
        owner = getattr(value, "tenant_id", tenant_id)
        if owner != tenant_id:
            raise SourceForbidden("source_scope_denied", "mutation tenant mismatch")
        names = type(value).model_fields if isinstance(value, Record) else (field.name for field in fields(value))
        for name in names:
            _check_value(getattr(value, name), tenant_id)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            _check_value(item, tenant_id)


class ScopedCollaborator:
    """One tenant's view of a collaborator; a wrong tenant anywhere in a call is a refusal.

    The fence is the last line, not the first: callers scope their own reads,
    but an upstream package is untrusted input and an identifier inside it
    could name another tenant's rule, transaction or review item. Those are
    looked up and checked here, so a write can never reach across tenants
    however the request was shaped.
    """
    def __init__(self, target, tenant_id: str, *, graph=None, reviews=None):
        self._target = target
        self._tenant_id = tenant_id
        self._graph = graph
        self._reviews = reviews

    def __getattr__(self, name):
        method = getattr(self._target, name)
        if not callable(method) or name.startswith("_"):
            raise AttributeError(name)

        @wraps(method)
        def scoped(*args, **kwargs):
            parameters = signature(method).bind(*args, **kwargs).arguments
            for key, value in parameters.items():
                if key in {"tenant", "tenant_id"} and value != self._tenant_id:
                    raise SourceForbidden("source_scope_denied", "mutation tenant mismatch")
                _check_value(value, self._tenant_id)
            if self._graph is not None:
                for key, getter in (("rule_id", "get_rule"), ("retired_rule_id", "get_rule"),
                                    ("from_rule_id", "get_rule"), ("to_rule_id", "get_rule"),
                                    ("transaction_id", "get_transaction")):
                    identifier = parameters.get(key)
                    if identifier is not None:
                        _check_value(getattr(self._graph, getter)(identifier), self._tenant_id)
                for key, getter in (("rule_ids", "get_rule"), ("transaction_ids", "get_transaction")):
                    for identifier in parameters.get(key, ()):
                        _check_value(getattr(self._graph, getter)(identifier), self._tenant_id)
                if name == "upsert_tag":
                    existing = self._graph.tag_name(parameters["tag_id"])
                    if existing is not None and existing != parameters["name"]:
                        raise SourceForbidden("source_action_forbidden", "shared tag rename requires all owners")
                if name == "upsert_learning":
                    _check_value(self._graph.get_learning(parameters["learning"].id), self._tenant_id)
                if name == "upsert_constraint":
                    raise SourceForbidden("source_action_forbidden", "source cannot replace organization constraints")
                if "constraint_id" in parameters and name == "set_constraint_status":
                    if not any(row.id == parameters["constraint_id"]
                               for row in self._graph.all_constraints(self._tenant_id)):
                        raise SourceForbidden("source_scope_denied", "mutation tenant mismatch")
            if self._reviews is not None and "item_id" in parameters:
                _check_value(self._reviews.get(parameters["item_id"]), self._tenant_id)
            if self._reviews is not None and name in {"enqueue", "enqueue_once"}:
                _check_value(self._reviews.get(parameters["item"].id), self._tenant_id)
            # Arguments and results are copied so a caller cannot change a
            # record after it was checked, or change what the memory adapter
            # holds by editing what it was handed back.
            result = deepcopy(method(*deepcopy(args), **deepcopy(kwargs)))
            _check_value(result, self._tenant_id)
            return result
        return scoped


def scoped_context(context: MutationContext, graph, reviews, tenant_id: str) -> MutationContext:
    """Reject unbound adapters before any write, then constrain their public calls."""
    # Every collaborator must sit on the same transaction as the graph: the
    # same Neo4j driver handle, or the same memory store. One on another
    # connection would commit on its own and survive this transaction's
    # rollback, which is how a half-applied update would come to exist.
    driver = getattr(graph, "_driver", None)
    history_events = getattr(context.history, "_admin_events", None)
    for collaborator in (context.graph, context.reviews, context.sources, context.history, context.events,
                         history_events):
        actual_driver = getattr(collaborator, "_driver", None)
        backing = getattr(collaborator, "_store", getattr(collaborator, "store", None))
        if actual_driver is not None and actual_driver is not driver:
            raise ValueError("Mutation collaborator uses another transaction")
        if backing is not None:
            if driver is not None and getattr(backing, "_driver", None) is not driver:
                raise ValueError("Mutation collaborator uses another transaction")
            if driver is None and backing is not graph:
                raise ValueError("Mutation collaborator uses another memory store")
    if context.reviews is not reviews and driver is None:
        raise ValueError("Mutation must use the rollback-bound review queue")
    return replace(context,
        graph=ScopedCollaborator(context.graph, tenant_id, graph=graph),
        reviews=ScopedCollaborator(context.reviews, tenant_id, reviews=reviews),
        sources=ScopedCollaborator(context.sources, tenant_id),
        history=ScopedCollaborator(context.history, tenant_id, graph=graph),
        events=ScopedCollaborator(context.events, tenant_id))


def policy_for(service, bound):
    using = getattr(service.policy, 'using', None)
    return using(bound.graph, bound.reviews) if callable(using) else service.policy


def with_admission(service, check):
    # Admission runs inside the transaction, after the collaborators are bound
    # and before the generation guards are compared, so a check added here sees
    # the state the write will act on and refuses before anything is written.
    protected = copy(service)
    original = service.factory_for

    def factory_for(context, action, skills):
        factory = original(context, action, skills)

        def bind(graph, reviews):
            bound = factory(graph, reviews)

            def admit():
                bound.admit()
                check(bound)
            return replace(bound, admit=admit)
        return bind
    protected.factory_for = factory_for
    return protected
