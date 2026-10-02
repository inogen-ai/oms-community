"""Publishing a chosen subset of a tenant's skills.

An extension can publish one tenant to several destinations, each holding the
skills chosen for it. Two things have to hold for that to be safe. The tree
and its Tier 2 manifest name only the chosen skills, so a destination never
discloses a skill it does not carry. And the gate refuses a destination only
over what that destination would publish: a broken skill in one destination
must not stop another from shipping, while anything the gate cannot attribute
to a skill, and the stored publish block, still stop every one.

With no selection, everything here behaves exactly as it did before selection
existed. Several tests below assert that half, because it is the half every
existing deployment runs.
"""
from pathlib import Path

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import (
    Artefact, Constraint, ContentBlock, Edge, Rule, Section, Skill,
)
from oms.domain.types import ArtefactKind, EdgeType, Mutability, SectionKind
from oms.publish.publisher import Publisher

TENANT = "acme"


def _skill(store: InMemoryGraphStore, skill_id: str, domain: str | None) -> None:
    store.upsert_skill(Skill(id=skill_id, name=skill_id.replace("-", " ").title(),
                             description=f"Guidance for {skill_id}.", domain=domain,
                             tenant_id=TENANT))
    rule_id = f"{skill_id}-rule"
    store.upsert_rule(Rule(id=rule_id, body=f"Follow the {skill_id} checklist.",
                           tenant_id=TENANT))
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule_id, to_id=skill_id))


def _store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    _skill(store, "revenue-recognition", "finance")
    _skill(store, "campaign-briefs", "marketing")
    store.upsert_constraint(Constraint(id="c1", body="Honour GDPR.", tenant_id=TENANT))
    return store


def _finance(skill: Skill) -> bool:
    return skill.domain == "finance"


def _marketing(skill: Skill) -> bool:
    return skill.domain == "marketing"


def _conflict(store: InMemoryGraphStore, rule_id: str) -> None:
    store.attach_edge(Edge(type=EdgeType.CONFLICTS_WITH, from_id=rule_id, to_id="c1"))


def _authorial_section(store: InMemoryGraphStore, skill_id: str, *bodies: str) -> None:
    section_id = f"{skill_id}-notes"
    store.upsert_section(Section(id=section_id, skill_id=skill_id, kind=SectionKind.PROSE,
                                 heading="Notes", order=1,
                                 mutability=Mutability.AUTHORIAL_PASSTHROUGH,
                                 tenant_id=TENANT))
    for index, body in enumerate(bodies):
        block = ContentBlock(id=f"{section_id}-block-{index}",
                             content_ref=f"sha256-{index}", kind=SectionKind.PROSE,
                             tenant_id=TENANT,
                             source_ref=f"skills/{skill_id}/SKILL.md#notes", body=body)
        store.upsert_content_block(block)
        store.attach_block(block, section_id)


# -- The tree and the manifest ----------------------------------------------------

def test_a_selection_limits_the_published_skills(tmp_path):
    out = tmp_path / "out"
    gate = Publisher(_store()).publish(TENANT, out, select=_finance)
    assert gate.passed, gate.reasons
    assert (out / "skills" / "revenue-recognition" / "SKILL.md").is_file()
    assert not (out / "skills" / "campaign-briefs").exists()


def test_a_selection_keeps_unchosen_skills_out_of_the_manifest_and_the_index(tmp_path):
    out = tmp_path / "out"
    Publisher(_store()).publish(TENANT, out, select=_finance)
    manifest = (out / "tier2" / "AGENTS.md").read_text(encoding="utf-8")
    root = (out / "CLAUDE.md").read_text(encoding="utf-8")
    assert "revenue-recognition" in manifest
    for text in (manifest, root):
        assert "campaign-briefs" not in text
        assert "Campaign Briefs" not in text


def test_publishable_skills_takes_the_same_selection():
    publisher = Publisher(_store())
    chosen = [skill.id for skill, _ in publisher.publishable_skills(TENANT, select=_marketing)]
    assert chosen == ["campaign-briefs"]
    everything = {skill.id for skill, _ in publisher.publishable_skills(TENANT)}
    assert everything == {"revenue-recognition", "campaign-briefs"}


# -- The gate, scoped to what is published ----------------------------------------

def test_a_conflict_in_an_unselected_skill_does_not_refuse(tmp_path):
    store = _store()
    _conflict(store, "campaign-briefs-rule")
    publisher = Publisher(store)
    assert publisher.check(TENANT, select=_finance).passed
    assert publisher.publish(TENANT, tmp_path / "finance", select=_finance).passed


def test_a_conflict_in_a_selected_skill_refuses(tmp_path):
    store = _store()
    _conflict(store, "campaign-briefs-rule")
    publisher = Publisher(store)
    gate = publisher.check(TENANT, select=_marketing)
    assert not gate.passed
    assert any("campaign-briefs-rule" in reason for reason in gate.reasons)
    assert not publisher.publish(TENANT, tmp_path / "marketing", select=_marketing).passed


def test_with_no_selection_any_conflict_still_refuses():
    store = _store()
    _conflict(store, "campaign-briefs-rule")
    assert not Publisher(store).check(TENANT).passed


def test_a_conflict_that_belongs_to_no_skill_refuses_every_selection():
    store = _store()
    store.upsert_rule(Rule(id="loose-rule", body="Never share figures.", tenant_id=TENANT))
    _conflict(store, "loose-rule")
    gate = Publisher(store).check(TENANT, select=_finance)
    assert not gate.passed
    assert any("loose-rule" in reason for reason in gate.reasons)


def test_a_conflict_in_a_selected_skill_that_publishes_nothing_still_refuses():
    """Selected means chosen by the selector, not merely rendered: the gate has
    always refused over every skill in the tenant, published or not, and a
    destination keeps that for the skills it owns."""
    store = _store()
    store.upsert_skill(Skill(id="ledger-close", name="Ledger Close",
                             description="Month end.", domain="finance",
                             tenant_id=TENANT, publish_enabled=False))
    store.upsert_rule(Rule(id="ledger-rule", body="Close on day three.", tenant_id=TENANT))
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="ledger-rule", to_id="ledger-close"))
    _conflict(store, "ledger-rule")
    assert not Publisher(store).check(TENANT, select=_finance).passed
    assert Publisher(store).check(TENANT, select=_marketing).passed


def test_the_stored_publish_block_refuses_every_selection():
    store = _store()
    store.set_publish_block(TENANT, ["held for review"], "compile")
    publisher = Publisher(store)
    for select in (_finance, _marketing, None):
        gate = publisher.check(TENANT, select=select)
        assert not gate.passed
        assert any("held for review" in reason for reason in gate.reasons)


def test_a_section_with_two_active_blocks_refuses_only_its_own_skill():
    store = _store()
    _authorial_section(store, "campaign-briefs", "First body.", "Second body.")
    publisher = Publisher(store)
    assert publisher.check(TENANT, select=_finance).passed
    gate = publisher.check(TENANT, select=_marketing)
    assert not gate.passed
    assert any("campaign-briefs-notes" in reason for reason in gate.reasons)
    assert not publisher.check(TENANT).passed


def test_a_block_with_no_text_refuses_only_its_own_skill():
    store = _store()
    _authorial_section(store, "campaign-briefs", "")
    publisher = Publisher(store)
    assert publisher.check(TENANT, select=_finance).passed
    gate = publisher.check(TENANT, select=_marketing)
    assert not gate.passed
    assert any("campaign-briefs-notes-block-0" in reason for reason in gate.reasons)


def test_a_block_with_no_text_and_no_skill_refuses_every_selection():
    store = _store()
    store.upsert_content_block(ContentBlock(
        id="stray-block", content_ref="sha256-x", kind=SectionKind.PROSE,
        tenant_id=TENANT, source_ref="skills/gone/SKILL.md#x", body=""))
    gate = Publisher(store).check(TENANT, select=_finance)
    assert not gate.passed
    assert any("stray-block" in reason for reason in gate.reasons)


def test_an_unrestorable_artefact_refuses_only_its_own_skill():
    store = _store()
    store.upsert_artefact(Artefact(id="a1", content_ref="sha256-missing",
                                   kind=ArtefactKind.SCRIPT, name="run.sh", size=10,
                                   tenant_id=TENANT,
                                   source_ref="skills/campaign-briefs/scripts/run.sh"),
                          "campaign-briefs", "scripts/run.sh")
    publisher = Publisher(store)
    assert publisher.check(TENANT, select=_finance).passed
    gate = publisher.check(TENANT, select=_marketing)
    assert not gate.passed
    assert any("skills/campaign-briefs/scripts/run.sh" in reason for reason in gate.reasons)


def test_a_selection_that_chooses_nothing_publishes_an_empty_tree(tmp_path):
    out = tmp_path / "out"
    gate = Publisher(_store()).publish(TENANT, out, select=lambda skill: False)
    assert gate.passed, gate.reasons
    assert not (out / "skills").exists() or not any((out / "skills").iterdir())
    assert (out / "install.sh").is_file()
