from oms.sources.models import LocalImportRequest, LocalPackage
from oms.sources.primitives import exact_digest


def local_request(world, *, create=False, description="Local description", approve=True):
    package = world.package("c", description)
    local = LocalPackage(revision=exact_digest(package.model_dump(mode="json")), package_path="skills/expenses",
                         manifest=package.manifest, raw_frontmatter=package.raw_frontmatter)
    guard = world.sources.get_generations(world.skill)
    world.policy.admit_local = lambda context, request: world.policy.admit("local_import", context, (request.skill,))
    return LocalImportRequest(skill=world.skill, package=local, expected_content_generation=guard.content,
        expected_binding_generation=guard.binding, create=create, local_name="expenses" if create else None,
        domain="finance" if create else None, transport="zip", filename="expenses.zip", approve=approve)


def test_new_local_import_captures_exact_stream_and_replay_has_no_second_write(source_world):
    world = source_world
    request = local_request(world, create=True)
    result = world.service.submit_local(world.context, request, idempotency_key="local-new")
    assert result.state == "complete" and result.committed
    stream = world.sources.get_local_stream(world.skill)
    snapshot = world.sources.get_snapshot(stream.baseline)
    assert snapshot.revision == request.package.revision
    assert snapshot.manifest == request.package.manifest
    assert world.sources.get_binding(world.skill) is None
    before = world.snapshot()
    assert world.service.submit_local(world.context, request, idempotency_key="local-new") == result
    assert world.snapshot() == before


def test_approved_local_change_uses_same_stream_across_transport_and_retains_undo(source_world):
    world = source_world
    world.service.submit_local(world.context, local_request(world, create=True), idempotency_key="local-new")
    origin = world.sources.get_local_stream(world.skill).origin
    request = local_request(world, description="New local description").model_copy(update={"transport": "cli", "filename": None})
    result = world.service.submit_local(world.context, request, idempotency_key="local-cli")
    assert result.state == "complete" and result.committed
    assert world.description() == "New local description"
    assert world.sources.get_local_stream(world.skill).origin == origin
    assert world.sources.get_undo(result.operation_id + ":undo", tenant_id="acme") is not None
    assert not world.store.pending_transactions(tenant_id="acme")


def test_unchanged_local_submission_creates_no_card_or_content_version(source_world):
    world = source_world
    world.service.submit_local(world.context, local_request(world, create=True), idempotency_key="local-new")
    history = world.store.skill_versions("acme", "expenses")
    generation = world.sources.get_generations(world.skill)
    result = world.service.submit_local(world.context, local_request(world), idempotency_key="local-again")
    assert result.state == "complete" and not result.committed
    assert result.outcomes[0].state == "unchanged"
    assert world.sources.open_update(world.skill) is None
    assert world.sources.get_generations(world.skill) == generation
    assert world.store.skill_versions("acme", "expenses") == history


def test_first_local_import_into_github_skill_keeps_github_base_and_requires_review(source_world):
    world = source_world
    world.install()
    github_base = world.sources.get_binding(world.skill).baseline
    before = world.description()
    result = world.service.submit_local(world.context, local_request(world), idempotency_key="first-local")
    assert result.state == "awaiting_review" and not result.committed
    assert world.description() == before
    assert world.sources.get_binding(world.skill).baseline == github_base
    assert world.sources.get_local_stream(world.skill).baseline is None
    assert world.sources.open_update(world.skill).plan.origin.kind == "local"


def test_competing_input_is_visible_refusal_and_preserves_current_card(source_world):
    world = source_world
    candidate = world.open_update()
    before = world.snapshot()
    result = world.service.submit_local(world.context, local_request(world), idempotency_key="competing-local")
    assert result.state == "blocked" and not result.committed
    assert result.outcomes[0].code == "another_update_open"
    assert world.snapshot() == before
    assert world.sources.open_update(world.skill) == candidate.update
    assert world.sources.blocked_attempts(world.skill)[0].action == "local_import"


def test_local_reimport_keeps_installed_section_kind(source_world):
    from hashlib import sha256
    from oms.domain.types import Mutability, SectionKind
    from oms.sources.models import ManifestEntry
    from tests.sources.test_apply import TINY_V1, TINY_V2
    world = source_world

    def tiny(body, **update):
        request = local_request(world, **update)
        entry = ManifestEntry(path="SKILL.md", blob_ref=world.blobs.put(body), digest=sha256(body).hexdigest(), size=len(body), mode=0o100644)
        package = request.package.model_copy(update={"manifest": (entry,), "revision": sha256(body).hexdigest()})
        return request.model_copy(update={"package": package})
    assert world.service.submit_local(world.context, tiny(TINY_V1, create=True), idempotency_key="local-new").committed
    installed, = world.store.sections_for_skill("expenses", tenant_id="acme")
    assert installed.kind is SectionKind.PROSE
    result = world.service.submit_local(world.context, tiny(TINY_V2), idempotency_key="local-v2")
    assert result.state == "complete" and result.committed
    section, = world.store.sections_for_skill("expenses", tenant_id="acme")
    assert (section.id, section.kind, section.mutability) == (installed.id, SectionKind.PROSE, Mutability.AUTHORIAL_PASSTHROUGH)
    assert [block.body for block in world.store.blocks_for_section(section.id, tenant_id="acme")] == [
        "- Answer in one short sentence.\n- End every answer with a full stop.\n- Use British spelling."]
    assert world.store.rules_for_section(section.id, tenant_id="acme") == []
