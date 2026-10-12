"""Checks and apply consume real projections with captured generation guards."""
import pytest
from types import SimpleNamespace

from oms.sources.errors import CheckThrottled, StaleMutation


def test_check_then_apply_updates_content_and_one_baseline(source_world):
    candidate = source_world.open_update()
    assert source_world.description() == "First description"
    result = source_world.apply(candidate)
    assert result.committed
    assert source_world.description() == "Second description"
    assert source_world.baseline_revision() == "b" * 40
    assert source_world.apply(candidate) == result


def test_apply_outcome_names_the_update_it_applied(source_world):
    candidate = source_world.open_update()
    result = source_world.apply(candidate)
    assert [row.update_id for row in result.outcomes] == [candidate.update.update_id]


def test_local_edit_after_check_refuses_stale_apply(source_world):
    candidate = source_world.open_update()
    source_world.edit_description("Local correction")
    with pytest.raises(StaleMutation):
        source_world.apply(candidate)
    assert source_world.description() == "Local correction"
    assert source_world.baseline_revision() == "a" * 40


def test_manual_check_throttle_is_durable_and_replay_does_not_fetch(source_world):
    source_world.open_update()
    reads = source_world.reader.reads
    source_world.check()
    assert source_world.reader.reads == reads
    with pytest.raises(CheckThrottled) as error:
        source_world.check(key="another-check")
    assert error.value.retry_after_seconds == 60
    assert source_world.reader.reads == reads


def test_source_rule_fork_preserves_other_owner_example_and_old_vector(source_world):
    from dataclasses import replace
    from oms.domain.models import Edge, Example, Skill
    from oms.domain.types import EdgeType, ExampleKind
    from oms.sources.apply import apply_parts
    from oms.sources.local_projection import project_local
    source_world.install()
    graph = source_world.store
    binding = source_world.sources.get_binding(source_world.skill)
    snapshot = source_world.sources.get_snapshot(binding.baseline)
    part = next(part for part in snapshot.effective_projection if part.kind == "rule")
    mapping = next(row for row in snapshot.graph_mappings if row.part_id == part.part_id)
    old_id = mapping.entity_ids[0]
    graph.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
    graph.attach_edge(Edge(EdgeType.BELONGS_TO, old_id, "other"), tenant_id="acme")
    graph.upsert_example(Example(id="shared-example", body="Keep example", kind=ExampleKind.POSITIVE,
        tenant_id="acme", parent_rule_id=old_id), tenant_id="acme")
    old = graph.get_rule(old_id)
    old.embedding = [1.0, 0.0]
    graph.upsert_rule(old)
    if hasattr(graph, "_driver"):
        with graph._driver.session() as session:
            session.run("MATCH (r:Rule {id:$id,tenant_id:'acme'}) SET r.embedding=$vector", id=old_id, vector=[1.0, 0.0]).consume()
    incoming_part = part.model_copy(update={"evidence": part.evidence.model_copy(update={
        "value": dict(part.evidence.value) | {"body": "Keep itemized receipts."}})})
    incoming = snapshot.model_copy(update={"effective_projection": tuple(incoming_part if row.part_id == part.part_id else row for row in snapshot.effective_projection)})

    def write(store, queue):
        bound = source_world.case.factory(store, queue)
        local = project_local(store, source_world.skill, baseline=snapshot)
        return apply_parts(bound, incoming, (incoming_part,), now=source_world.now, operation_id="fork", local=local)
    resolved = source_world.repository.atomic("fork", "acme", write)
    new_id = next(row.entity_ids[0] for row in resolved.graph_mappings if row.part_id == part.part_id)
    assert new_id != old_id
    assert graph.get_rule(old_id).body == "Keep receipts."
    assert graph.get_rule(new_id).embedding is None
    if hasattr(graph, "_driver"):
        with graph._driver.session() as session:
            vectors = session.run("MATCH (r:Rule {tenant_id:'acme'}) WHERE r.id IN $ids RETURN r.id AS id, r.embedding AS vector", ids=[old_id, new_id]).data()
        assert {row["id"]: row["vector"] for row in vectors} == {old_id: [1.0, 0.0], new_id: None}
    else:
        assert graph.get_rule(old_id).embedding == [1.0, 0.0]
    assert [row.id for row in graph.rules_for_skill("other", tenant_id="acme")] == [old_id]
    assert graph.examples_for_rule(old_id, tenant_id="acme")[0].body == "Keep example"
    assert graph.examples_for_rule(new_id, tenant_id="acme")[0].body == "Keep example"


def test_apply_retains_exact_undo_ownership_and_generations(source_world):
    candidate = source_world.open_update()
    previous = source_world.sources.ownership_for_skill(source_world.skill)
    result = source_world.apply(candidate)
    undo = source_world.sources.get_undo(result.operation_id + ":undo", tenant_id="acme")
    assert undo.previous_ownership == previous
    assert undo.resulting_ownership == source_world.sources.ownership_for_skill(source_world.skill)
    assert undo.expected_generations == (source_world.sources.get_generations(source_world.skill),)
    description = next(write for write in undo.writes if write.part_id == "field:description")
    assert description.before.value == "First description"
    assert description.after.value == "Second description"


@pytest.mark.parametrize("change", ["source_generation", "unlink"])
def test_check_replay_survives_later_source_state_change(source_world, change):
    from oms.sources.models import CheckRequest
    source_world.install()
    binding = source_world.sources.get_binding(source_world.skill)
    source = source_world.sources.get_source(binding.source_id, tenant_id="acme")
    request = CheckRequest(source_id=source.source_id, expected_source_generation=source.generation, skills=(source_world.skill,))
    result = source_world.service.check(source_world.context, request, idempotency_key="check")
    if change == "source_generation":
        source_world.sources.put_source(source.model_copy(update={"generation": source.generation + 1}))
    else:
        source_world.sources.retire_skill(source_world.skill)
    assert source_world.service.check(source_world.context, request, idempotency_key="check") == result
    assert source_world.reader.reads == 1


def test_source_metadata_write_never_enters_compilation_queue(source_world):
    candidate = source_world.open_update()
    source_world.apply(candidate)
    assert source_world.store.pending_transactions(tenant_id="acme") == []


def test_applied_file_update_keeps_the_existing_occurrence_kind(source_world):
    from dataclasses import replace
    from types import SimpleNamespace
    from oms.domain.types import ArtefactKind
    from oms.sources.models import UpdateRequest
    candidate = source_world.open_file_update("README.md")

    def classify(graph, queue):
        artefact, path = graph.artefacts_for_skill("expenses", tenant_id="acme")[0]
        graph.upsert_artefact(replace(artefact, kind=ArtefactKind.REFERENCE_MD), "expenses", path, tenant_id="acme")
    source_world.repository.atomic("classify", "acme", classify)
    update = candidate.update
    source_world.service.recheck(source_world.context, UpdateRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="recheck")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    assert source_world.apply(SimpleNamespace(update=update)).committed
    (artefact, path), = source_world.store.artefacts_for_skill("expenses", tenant_id="acme")
    assert source_world.blobs.get(artefact.content_ref) == b"Updated supporting document\n"
    assert artefact.kind is ArtefactKind.REFERENCE_MD


def _rules_world(mutation_case, tmp_path, *, upstream, base=("Rule A.", "Rule B.", "Rule C.")):
    from tests.sources.source_world import SourceWorld

    class World(SourceWorld):
        def package(self, revision, description, **kwargs):
            rules = base if revision == "a" else upstream
            return super().package(revision, "Same description", rule="\n- ".join(rules))
    world = World(mutation_case, tmp_path)
    world.install()
    section = next(row for row in world.store.sections_for_skill("expenses", tenant_id="acme") if row.heading == "Rules")

    def placed():
        rows = world.store.rule_placements_for_section(section.id, tenant_id="acme")
        return [(world.store.get_rule(row.rule_id).body, row.order) for row in rows]

    def rule(body):
        row = next(row for row in world.store.rule_placements_for_section(section.id, tenant_id="acme")
                   if world.store.get_rule(row.rule_id).body == body)
        return world.store.get_rule(row.rule_id), row

    def apply():
        update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id="acme")
        case.result = world.apply(SimpleNamespace(update=update))
        assert case.result.committed
        return update.plan
    case = SimpleNamespace(world=world, section=section, placed=placed, rule=rule, apply=apply)
    return case


def test_rule_inserted_mid_section_upstream_applies_in_upstream_order(mutation_case, tmp_path):
    case = _rules_world(mutation_case, tmp_path, upstream=("Rule A.", "Rule X.", "Rule B.", "Rule C."))
    plan = case.apply()
    assert plan.conflicts == () and plan.flags == ()
    assert [change.action for change in plan.changes if change.kind == "rule"].count("keep") == 3
    assert [change.incoming.value["body"] for change in plan.changes if change.action != "keep"] == ["Rule X."]
    assert case.placed() == [("Rule A.", 0), ("Rule X.", 1), ("Rule B.", 2), ("Rule C.", 3)]
    from oms.sources.requests import UndoRequest
    record = case.world.sources.get_undo(case.result.operation_id + ":undo", tenant_id="acme")
    case.world.service.undo(case.world.context, UndoRequest(undo_id=record.undo_id, skill=case.world.skill,
        expected_generations=record.expected_generations, policy_version=record.policy_version), idempotency_key="undo")
    assert case.placed() == [("Rule A.", 0), ("Rule B.", 1), ("Rule C.", 2)]


def test_local_correction_keeps_its_body_and_takes_the_upstream_sequence(mutation_case, tmp_path):
    from dataclasses import replace
    case = _rules_world(mutation_case, tmp_path, upstream=("Rule A.", "Rule X.", "Rule B.", "Rule C."))
    rule, _ = case.rule("Rule B.")
    case.world.store.upsert_rule(replace(rule, body="Rule B, corrected."))
    plan = case.apply()
    assert plan.conflicts == () and plan.flags == ()
    assert case.placed() == [("Rule A.", 0), ("Rule X.", 1), ("Rule B, corrected.", 2), ("Rule C.", 3)]


def _two_rewords(mutation_case, tmp_path, choices):
    """Upstream rewords B and C in one region and B is locally corrected: the
    merge links both additions to both withdrawals. `choices` maps a body to
    its draft choice, by the withdrawn text or the incoming text."""
    from dataclasses import replace
    from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest, UpdateRequest
    from oms.sources.review import part_fingerprint
    case = _rules_world(mutation_case, tmp_path, upstream=("Rule A.", "Rule B2.", "Rule C2."))
    rule, _ = case.rule("Rule B.")
    case.world.store.upsert_rule(replace(rule, body="Rule B, corrected."))
    update = case.world.sources.get_update(case.world.check().outcomes[0].update_id, tenant_id="acme")
    by_text = {(change.base.value if change.incoming.kind == "absent" else change.incoming.value)["body"]: change
               for change in update.plan.changes if change.kind == "rule" and change.action != "keep"}
    assert set(by_text) == {"Rule B.", "Rule C.", "Rule B2.", "Rule C2."}
    request = UpdateRequest(update_id=update.update_id, skill=update.plan.skill, fingerprint=update.plan.fingerprint)
    case.world.service.save_draft(case.world.context, SaveDraftRequest(**request.model_dump(), choices=tuple(
        DraftChoice(part_id=by_text[body].part_id, choice=choice, part_fingerprint=part_fingerprint(update, by_text[body].part_id),
                    merged_text="Rule B, merged." if choice == "merged_text" else None)
        for body, choice in choices.items())), idempotency_key="draft")
    request = UpdateRequest(update_id=update.update_id, skill=update.plan.skill,
        fingerprint=case.world.sources.get_update(update.update_id, tenant_id="acme").plan.fingerprint)

    def apply(*consents):
        return case.world.service.apply(case.world.context, ApplyRequest(**request.model_dump(), removal_consents=tuple(
            by_text[body].part_id for body in consents)), idempotency_key="apply")
    case.apply_with = apply
    case.bodies = lambda: sorted(row.body for row in case.world.store.rules_for_skill("expenses", tenant_id="acme"))
    return case


@pytest.mark.parametrize("keep,kept_body", [("keep_oms", "Rule B, corrected."), ("merged_text", "Rule B, merged.")])
def test_kept_linked_removal_does_not_block_an_unrelated_reword(mutation_case, tmp_path, keep, kept_body):
    case = _two_rewords(mutation_case, tmp_path, {"Rule B.": keep, "Rule B2.": "keep_oms",
                                                   "Rule C.": "use_upstream", "Rule C2.": "use_upstream"})
    assert case.apply_with("Rule C.").committed
    assert case.bodies() == sorted(["Rule A.", kept_body, "Rule C2."])


@pytest.mark.parametrize("choices,consents", [
    # Keeping B does not excuse consent for the removal of C.
    ({"Rule B.": "keep_oms", "Rule B2.": "keep_oms", "Rule C.": "use_upstream", "Rule C2.": "use_upstream"}, ()),
    # Accepting both removals still needs consent for both.
    ({"Rule B.": "use_upstream", "Rule B2.": "use_upstream", "Rule C.": "use_upstream", "Rule C2.": "use_upstream"}, ("Rule C.",)),
    ({"Rule B.": "use_upstream", "Rule B2.": "use_upstream", "Rule C.": "use_upstream", "Rule C2.": "use_upstream"}, ()),
])
def test_unkept_linked_removal_still_needs_consent(mutation_case, tmp_path, choices, consents):
    from oms.sources.errors import SourceConflict
    case = _two_rewords(mutation_case, tmp_path, choices)
    with pytest.raises(SourceConflict, match="consent"):
        case.apply_with(*consents)
    assert case.bodies() == ["Rule A.", "Rule B, corrected.", "Rule C."]


def test_local_reorder_and_local_addition_keep_their_anchors_against_upstream_insertion(mutation_case, tmp_path):
    from oms.domain.models import Rule
    case = _rules_world(mutation_case, tmp_path, upstream=("Rule A.", "Rule X.", "Rule B.", "Rule C."))
    store = case.world.store
    for body, order in (("Rule C.", 0), ("Rule A.", 1), ("Rule B.", 3)):
        rule, row = case.rule(body)
        store.attach_rule(rule, case.section.id, order=order, group=row.group, tenant_id="acme")
    store.upsert_rule(Rule(id="local-rule", body="Local rule.", tenant_id="acme"))
    store.attach_rule(store.get_rule("local-rule"), case.section.id, order=2, tenant_id="acme")
    plan = case.apply()
    assert plan.conflicts == ()
    assert case.placed() == [("Rule C.", 0), ("Rule A.", 1), ("Local rule.", 2), ("Rule X.", 3), ("Rule B.", 4)]


def _undo(case):
    from oms.sources.requests import UndoRequest
    record = case.world.sources.get_undo(case.result.operation_id + ":undo", tenant_id="acme")
    case.world.service.undo(case.world.context, UndoRequest(undo_id=record.undo_id, skill=case.world.skill,
        expected_generations=record.expected_generations, policy_version=record.policy_version), idempotency_key="undo")


def test_learned_rules_stay_unordered_after_an_upstream_insertion(mutation_case, tmp_path):
    from oms.domain.models import Rule
    case = _rules_world(mutation_case, tmp_path, upstream=("Rule A.", "Rule X.", "Rule B.", "Rule C."))
    store = case.world.store
    for rule_id, body in (("learned-one", "Learned one."), ("learned-two", "Learned two.")):
        store.upsert_rule(Rule(id=rule_id, body=body, tenant_id="acme"))
        store.attach_rule(store.get_rule(rule_id), case.section.id, order=None, tenant_id="acme")
    case.apply()
    learned = [("Learned one.", None), ("Learned two.", None)]
    assert case.placed() == [("Rule A.", 0), ("Rule X.", 1), ("Rule B.", 2), ("Rule C.", 3), *learned]
    _undo(case)
    assert case.placed() == [("Rule A.", 0), ("Rule B.", 1), ("Rule C.", 2), *learned]


def test_unordered_examples_stay_unordered_after_an_upstream_insertion(mutation_case, tmp_path):
    from oms.domain.models import Example
    from oms.domain.types import ExampleKind
    case = _rules_world(mutation_case, tmp_path, base=("Rule A.\n  Example: Example one.\n  Example: Example two.",),
                        upstream=("Rule A.\n  Example: Example one.\n  Example: Example new.\n  Example: Example two.",))
    store = case.world.store
    rule, _ = case.rule("Rule A.")

    def examples():
        return [(row.body, row.order) for row in store.examples_for_rule(rule.id, tenant_id="acme")]
    assert examples() == [("Example one.", 0), ("Example two.", 1)]
    store.upsert_example(Example(id="learned-example", body="Learned example.", kind=ExampleKind.POSITIVE,
                                 tenant_id="acme", parent_rule_id=rule.id), tenant_id="acme")
    case.apply()
    assert examples() == [("Example one.", 0), ("Example new.", 1), ("Example two.", 2), ("Learned example.", None)]
    _undo(case)
    assert examples() == [("Example one.", 0), ("Example two.", 1), ("Learned example.", None)]


# hwigge/oms-skill-refresh-test skills/tiny-test at 5891570 and 300b857: the
# added "Use" bullet makes the classifier read the '(intro)' section as rules.
TINY_V1 = (b"---\nname: tiny-test\ndescription: Minimal skill for testing OMS GitHub skill add and edit.\n---\n\n"
           b"# Tiny test\n\n- Answer in one sentence.\n- End every answer with a full stop.\n")
TINY_V2 = (b"---\nname: tiny-test\ndescription: Minimal skill for testing OMS GitHub skill add and edit.\n---\n\n"
           b"# Tiny test\n\n- Answer in one short sentence.\n- End every answer with a full stop.\n- Use British spelling.\n")


def _document_world(mutation_case, tmp_path, first, second):
    from hashlib import sha256
    from tests.sources.source_world import SourceWorld

    class World(SourceWorld):
        def package(self, revision, description, **kwargs):
            package = super().package(revision, description, **kwargs)
            body = first if revision == "a" else second
            digest = sha256(body).hexdigest()
            entry = package.manifest[0].model_copy(update={"blob_ref": self.blobs.put(body), "digest": digest, "size": len(body)})
            return package.model_copy(update={"manifest": (entry,)})
    world = World(mutation_case, tmp_path)
    world.install()
    return world


def _intro(world):
    section, = world.store.sections_for_skill("expenses", tenant_id="acme")
    blocks = world.store.blocks_for_section(section.id, tenant_id="acme")
    return section, [block.body for block in blocks], world.store.rules_for_section(section.id, tenant_id="acme")


def test_refreshed_prose_section_keeps_its_installed_kind(mutation_case, tmp_path):
    from oms.domain.types import Mutability, SectionKind
    world = _document_world(mutation_case, tmp_path, TINY_V1, TINY_V2)
    installed, _, _ = _intro(world)
    assert installed.kind is SectionKind.PROSE
    update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id="acme")
    assert update.plan.conflicts == () and update.plan.flags == ()
    changed = [change for change in update.plan.changes if change.action != "keep"]
    assert [change.kind for change in changed] == ["section"]
    assert changed[0].incoming.value["kind"] == "prose"
    assert world.apply(SimpleNamespace(update=update)).committed
    section, bodies, rules = _intro(world)
    assert (section.id, section.kind, section.mutability) == (installed.id, SectionKind.PROSE, Mutability.AUTHORIAL_PASSTHROUGH)
    assert bodies == ["- Answer in one short sentence.\n- End every answer with a full stop.\n- Use British spelling."]
    assert rules == []


def test_refreshed_rules_section_keeps_its_installed_kind(mutation_case, tmp_path):
    from oms.domain.types import SectionKind
    from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest
    from oms.sources.review import part_fingerprint
    # Without "Use British spelling." no bullet has an imperative lead, so a fresh
    # import would read the section as prose; the installed rules section stays.
    world = _document_world(mutation_case, tmp_path, TINY_V2, TINY_V2.replace(b"- Use British spelling.\n", b""))
    installed, _, rules = _intro(world)
    assert installed.kind is SectionKind.RULES and len(rules) == 3
    update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id="acme")
    removed, = [change for change in update.plan.changes if change.action != "keep"]
    assert (removed.kind, removed.base.value["body"], removed.incoming.kind) == ("rule", "Use British spelling.", "absent")
    assert [(row.part_id, row.reason) for row in update.plan.conflicts] == [(removed.part_id, "deletion_consent")]
    choice = DraftChoice(part_id=removed.part_id, choice="use_upstream", part_fingerprint=part_fingerprint(update, removed.part_id))
    world.service.save_draft(world.context, SaveDraftRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint, choices=(choice,)), idempotency_key="draft")
    update = world.sources.get_update(update.update_id, tenant_id="acme")
    assert world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint, removal_consents=(removed.part_id,)), idempotency_key="apply").committed
    section, bodies, _ = _intro(world)
    assert (section.id, section.kind, bodies) == (installed.id, SectionKind.RULES, [])
    placed = world.store.rule_placements_for_section(section.id, tenant_id="acme")
    assert sorted((row.order, world.store.get_rule(row.rule_id).body) for row in placed) == [
        (0, "Answer in one short sentence."), (1, "End every answer with a full stop.")]


def test_new_section_is_classified_on_refresh(mutation_case, tmp_path):
    from oms.domain.types import SectionKind
    world = _document_world(mutation_case, tmp_path, TINY_V1, TINY_V1 + b"\n## Style\n\n- Use British spelling.\n")
    update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id="acme")
    assert world.apply(SimpleNamespace(update=update)).committed
    kinds = {row.heading: row.kind for row in world.store.sections_for_skill("expenses", tenant_id="acme")}
    assert kinds == {"(intro)": SectionKind.PROSE, "Style": SectionKind.RULES}


def test_headingless_package_renders_its_rules_in_package_order(mutation_case, tmp_path):
    from oms.publish.publisher import Publisher
    # Rule ids derive from the text; this order is the reverse of the id order.
    bodies = ["Use British spelling.", "End every answer with a full stop.", "Answer in one short sentence."]
    package = TINY_V1.split(b"- Answer")[0] + "".join(f"- {body}\n" for body in bodies).encode()
    world = _document_world(mutation_case, tmp_path, package, package)
    text = Publisher(world.store).render_skill("expenses", "acme")
    assert sorted(bodies, key=text.index) == bodies
