"""Storage and mutation boundaries for source reconciliation."""
import inspect
import json

import pytest
from pydantic import ValidationError

from oms.domain.identity import SkillRef
from oms.ports.graph_store import GraphStore
from oms.ports.mutation import MutationRequest
from oms.sources.models import (
    Evidence, Generations, MergePlan, OriginRef, PlanFingerprint,
    SnapshotRef, SourceSnapshot,
)


def origin(tenant="one", origin_id="github-1", generation=1):
    return OriginRef(skill=SkillRef(tenant, "expenses"), origin_id=origin_id,
                     kind="github", generation=generation)


def snapshot_ref(**kwargs):
    return SnapshotRef(snapshot_id="snapshot-1", origin=origin(**kwargs))


def fingerprint():
    return PlanFingerprint(content_generation=2, binding_generation=1,
                           update_generation=1, policy_version="1",
                           local_digest="local", candidate_digest="incoming")


def test_skill_key_is_reversible_and_cannot_alias_tenants_or_delimiters():
    refs = [SkillRef("a:b", "c"), SkillRef("a", "b:c"),
            SkillRef("one", "expenses"), SkillRef("two", "expenses"),
            SkillRef('x"[', "ø\\z")]
    assert len({ref.storage_key for ref in refs}) == len(refs)
    for ref in refs:
        assert SkillRef.from_storage_key(ref.storage_key) == ref
    assert json.loads(refs[2].storage_key) == ["one", "expenses"]


@pytest.mark.parametrize("key", ['["one"]', '[1,"skill"]', '{}', 'not-json'])
def test_skill_key_rejects_malformed_identity(key):
    with pytest.raises(ValueError):
        SkillRef.from_storage_key(key)


def test_certainty_survives_json_without_conflating_null_absence_and_unknown():
    states = [Evidence(kind="known", value=None, source_digest="raw", policy_version="1"),
              Evidence(kind="absent", policy_version="1"),
              Evidence(kind="unknown", policy_version="1")]
    restored = [Evidence.model_validate_json(item.model_dump_json()) for item in states]
    assert [item.kind for item in restored] == ["known", "absent", "unknown"]
    assert len({item.model_dump_json() for item in restored}) == 3
    with pytest.raises(ValidationError):
        Evidence(kind="known", policy_version="1")
    with pytest.raises(ValidationError):
        Evidence(kind="absent", value="present", policy_version="1")


def test_snapshot_is_frozen_and_retained_evidence_cannot_be_mutated():
    evidence = Evidence(kind="known", value={"tags": ["safe"]}, policy_version="1")
    snapshot = SourceSnapshot(ref=snapshot_ref(), revision="a" * 40,
                              raw_frontmatter=evidence, projection_version="1",
                              policy_version="1")
    restored = SourceSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
    with pytest.raises(ValidationError):
        restored.revision = "b" * 40
    with pytest.raises(TypeError):
        restored.raw_frontmatter.value["tags"].append("changed")
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate({**snapshot.model_dump(), "credential": "not-a-token"})
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate({**snapshot.model_dump(), "format_version": 2})


@pytest.mark.parametrize("other", [dict(tenant="two"), dict(origin_id="local-1"),
                                    dict(generation=2)])
def test_plan_rejects_a_base_from_another_tenant_origin_or_generation(other):
    with pytest.raises(ValidationError):
        MergePlan(skill=SkillRef("one", "expenses"), origin=origin(),
                  base=snapshot_ref(**other), incoming=snapshot_ref(),
                  fingerprint=fingerprint())


def test_mutation_guards_cover_exact_sorted_unique_affected_skills():
    first, second = SkillRef("one", "a"), SkillRef("one", "b")
    kwargs = dict(operation_id="op", tenant_id="one", actor_id="reviewer",
                  request_digest="digest", affected_skills=(second, first),
                  expected_generations=(Generations(skill=second, content=2, binding=1),
                                        Generations(skill=first, content=0, binding=0)))
    request = MutationRequest(**kwargs)
    assert request.affected_skills == (first, second)
    assert tuple(g.skill for g in request.expected_generations) == (first, second)
    for changed in [dict(tenant_id="two"), dict(affected_skills=(first, first)),
                    dict(expected_generations=kwargs["expected_generations"][:1])]:
        with pytest.raises(ValidationError):
            MutationRequest(**(kwargs | changed))


@pytest.mark.parametrize("method", [
    "get_skill", "delete_skill", "tags_for_skill", "rules_for_skill",
    "sections_for_skill", "artefacts_for_skill", "examples_for_skill",
    "attach_edge", "detach_edge", "get_section", "get_content_block",
    "section_for_block", "blocks_for_section", "rules_for_section",
    "rule_placements_for_section", "examples_for_section", "get_skill_version",
    "skills_for_rule", "skills_by_rule", "examples_for_rule", "inherit_placements",
    "blocks_revised_for_rule", "supersede_block",
])
def test_owned_graph_contract_requires_explicit_tenant(method):
    parameter = inspect.signature(getattr(GraphStore, method)).parameters["tenant_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_evidence_remains_immutable_during_transaction_snapshot_copy():
    from copy import deepcopy

    value = Evidence(kind="known", value={"nested": [1]}, policy_version="1")
    copied = deepcopy(value)
    assert copied == value
    with pytest.raises(TypeError):
        copied.value["nested"][0] = 2


def test_discovery_and_leases_reject_ambiguous_naive_expiry():
    from datetime import datetime
    from oms.sources.models import DiscoveryResult, Lease, ResolvedRef

    ref = ResolvedRef(canonical_url="https://github.com/example/skills", kind="branch",
                      name="main", commit="a" * 40)
    with pytest.raises(ValidationError):
        DiscoveryResult(discovery_id="d", tenant_id="one", actor_id="actor", resolved_ref=ref,
                        expires_at=datetime(2026, 1, 1), packages=())
    with pytest.raises(ValidationError):
        Lease(tenant_id="one", resource_id="r", owner_id="owner", run_id="run", fencing_token=1,
              expires_at=datetime(2026, 1, 1))


def test_acquired_package_requires_explicit_history_certainty():
    from oms.sources.models import AcquiredPackage, ResolvedRef

    ref = ResolvedRef(canonical_url="https://github.com/example/skills", kind="tag",
                      name="v1", commit="a" * 40)
    with pytest.raises(ValidationError):
        AcquiredPackage(resolved_ref=ref, package_path="", manifest=(),
                        raw_frontmatter=Evidence(kind="absent", policy_version="1"))


def test_committed_outcomes_cannot_claim_fetching_or_cross_tenant_operation():
    from oms.sources.models import ActionContext, Operation, OperationResult, SkillOutcome

    with pytest.raises(ValidationError):
        OperationResult(operation_id="op", state="fetching", committed=True)
    result = OperationResult(operation_id="op", state="complete", committed=True,
                             outcomes=(SkillOutcome(skill=SkillRef("two", "x"), state="applied"),))
    with pytest.raises(ValidationError):
        Operation(operation_id="op", context=ActionContext(tenant_id="one", actor_id="actor",
                  domain_scope=("example",), capabilities=frozenset()), idempotency_key="key",
                  request_digest="digest", result=result)


def test_retarget_preserves_older_baseline_evidence_without_relabeling_it():
    from oms.sources.models import Binding, ResolvedRef

    prior = snapshot_ref()
    current = origin(generation=2)
    incoming = SnapshotRef(snapshot_id="new", origin=current)
    guard = fingerprint().model_copy(update={"binding_generation": 2})
    plan = MergePlan(skill=current.skill, origin=current, base=prior,
                     incoming=incoming, fingerprint=guard)
    binding = Binding(origin=current, source_id="source", package_path="",
                      ref=ResolvedRef(canonical_url="https://github.com/example/skills",
                                      kind="branch", name="next", commit="a" * 40),
                      baseline=prior)
    assert plan.base.origin.generation == binding.baseline.origin.generation == 1
    assert plan.incoming.origin.generation == 2


def test_undo_requires_same_origin_baselines_and_complete_tenant_safe_guards():
    from oms.sources.models import GraphMapping, PartOwnership, UndoRecord, UndoWrite

    current = origin(generation=2)
    prior = snapshot_ref()
    incoming = SnapshotRef(snapshot_id="incoming", origin=current)
    evidence = Evidence(kind="known", value="text", policy_version="1")
    write = UndoWrite(skill=current.skill, part_id="description", kind="field",
                      before=evidence, after=evidence)
    guard = Generations(skill=current.skill, content=3, binding=2)
    values = dict(undo_id="undo", operation_id="op", origin=current,
                  previous_base=prior, resulting_base=incoming, writes=(write,),
                  expected_generations=(guard,), policy_version="1")
    assert UndoRecord(**values).previous_base == prior
    claim = PartOwnership(skill=current.skill, part_id="description", origin_ids=(current.origin_id,),
                          entity_ids=("description",), proof_digest="proof")
    receipt = UndoRecord(**values, previous_ownership=(claim,),
                         previous_mappings=(GraphMapping(part_id="description", entity_ids=("description",),
                                                        owner_skills=(current.skill,)),))
    assert UndoRecord.model_validate_json(receipt.model_dump_json()) == receipt
    foreign = Generations(skill=SkillRef("two", "expenses"), content=3, binding=2)
    for changed in [dict(previous_base=snapshot_ref(origin_id="other")),
                    dict(resulting_base=prior), dict(expected_generations=()),
                    dict(expected_generations=(guard, guard)),
                    dict(expected_generations=(guard, foreign)),
                    dict(writes=(write.model_copy(update={"skill": foreign.skill}),)),
                    dict(writes=(write.model_copy(update={"skill": SkillRef("one", "other")}),)),
                    dict(previous_ownership=(claim.model_copy(update={"skill": foreign.skill}),)),
                    dict(resulting_mappings=(GraphMapping(part_id="description", entity_ids=("description",),
                                                         owner_skills=(foreign.skill,)),))]:
        with pytest.raises(ValidationError):
            UndoRecord(**(values | changed))


def test_durable_event_rejects_foreign_or_duplicate_skill_scope():
    from oms.sources.models import DurableEvent

    for skills in [(SkillRef("two", "expenses"),),
                   (SkillRef("one", "expenses"), SkillRef("one", "expenses"))]:
        with pytest.raises(ValidationError):
            DurableEvent(event_id="event", tenant_id="one", operation_id="op",
                         action="applied", skills=skills)
