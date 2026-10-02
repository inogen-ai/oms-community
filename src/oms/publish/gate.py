from collections.abc import Callable
from dataclasses import dataclass, field

from oms.domain.models import Skill
from oms.ports.blob_store import BlobStore
from oms.ports.graph_store import GraphStore


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _has_frontmatter(text: str) -> bool:
    if not text.startswith("---\n"):
        return False
    rest = text[4:]
    return "\n---" in rest and "name:" in rest and "description:" in rest


class _Owners:
    """Which of the tenant's skills a gate finding belongs to.

    Built only when a selection asks for it and only once there is a finding
    to place: a healthy tenant never pays for the walk. A rule belongs to a
    skill through its BELONGS_TO edge or through a section of that skill, the
    two routes `Publisher._render` reads it by; a section and its blocks
    belong to the section's skill.
    """

    def __init__(self, store: GraphStore, tenant_id: str,
                 select: Callable[[Skill], bool]) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._select = select
        self._index: tuple[dict[str, Skill], dict[str, set[str]],
                           dict[str, str], dict[str, str]] | None = None

    def _build(self) -> tuple[dict[str, Skill], dict[str, set[str]],
                              dict[str, str], dict[str, str]]:
        if self._index is None:
            skills = {s.id: s for s in self._store.skills_for_tenant(self._tenant_id)}
            rules: dict[str, set[str]] = {}
            sections: dict[str, str] = {}
            blocks: dict[str, str] = {}
            for skill_id in skills:
                for rule in self._store.rules_for_skill(skill_id):
                    rules.setdefault(rule.id, set()).add(skill_id)
                for section in self._store.sections_for_skill(skill_id):
                    sections[section.id] = skill_id
                    for rule in self._store.rules_for_section(section.id):
                        rules.setdefault(rule.id, set()).add(skill_id)
                    for block in self._store.blocks_for_section(section.id):
                        blocks[block.id] = skill_id
            self._index = (skills, rules, sections, blocks)
        return self._index

    def _refuses(self, owner_ids: set[str]) -> bool:
        """A finding refuses this destination when it belongs to a skill the
        selection chose, or to no skill at all. Unattributable is not
        harmless: it is the case the gate cannot reason about, so it refuses
        every destination, as the whole gate did before selections existed."""
        skills = self._build()[0]
        owners = [skills[i] for i in owner_ids if i in skills]
        return not owners or any(self._select(skill) for skill in owners)

    def rule(self, rule_id: str) -> bool:
        return self._refuses(self._build()[1].get(rule_id, set()))

    def section(self, section_id: str) -> bool:
        owner = self._build()[2].get(section_id)
        return self._refuses({owner} if owner else set())

    def block(self, block_id: str) -> bool:
        owner = self._build()[3].get(block_id)
        return self._refuses({owner} if owner else set())


def _unwritable_artefacts(store: GraphStore, tenant_id: str,
                          blob_store: BlobStore | None,
                          select: Callable[[Skill], bool] | None = None) -> list[str]:
    """Artefact paths that publish could not restore: every artefact when no
    blob store is wired, else those whose content_ref does not resolve. With a
    selection, only the selected skills' artefacts: an artefact always belongs
    to a skill, so there is no unattributable case here."""
    missing: list[str] = []
    for skill in store.skills_for_tenant(tenant_id):
        if select is not None and not select(skill):
            continue
        for artefact, path in store.artefacts_for_skill(skill.id):
            if blob_store is None or not blob_store.exists(artefact.content_ref):
                missing.append(f"skills/{skill.id}/{path}")
    return missing


def run_publish_gate(store: GraphStore, tenant_id: str,
                     rendered_skill_texts: list[str],
                     blob_store: BlobStore | None = None, *,
                     select: Callable[[Skill], bool] | None = None) -> GateResult:
    """Everything that refuses a publish.

    `select` is the destination's skill selection (`Publisher.publish`). With
    none, every finding refuses, exactly as before selections existed. With
    one, a conflicting rule, a section with several active blocks, a block
    with no text or an artefact that cannot be restored refuses only when it
    belongs to a selected skill or to no skill at all, so a broken skill in
    one destination does not stop another. The stored publish block is not a
    finding about a skill: it refuses every destination."""
    reasons: list[str] = []
    owners = _Owners(store, tenant_id, select) if select is not None else None

    # Honour a stored publication block without changing its policy or source.
    block = store.get_publish_block(tenant_id)
    if block is not None:
        detail = "; ".join(block.reasons) if block.reasons else "no reason recorded"
        reasons.append(f"publishing is blocked by the {block.source} check: {detail}")

    # Recorded constraint conflicts block publication independently of the flag.
    conflicting = store.rules_conflicting_with_constraints(tenant_id)
    if owners is not None:
        conflicting = [rule for rule in conflicting if owners.rule(rule.id)]
    if conflicting:
        ids = ", ".join(r.id for r in conflicting)
        reasons.append(f"rules conflict with a constraint and must not publish: {ids}")

    # Custody invariant (Section 7.3): each authorial section must hold exactly
    # one active ContentBlock. Any section with more breaks supersession custody.
    multi_block = store.sections_with_multiple_active_blocks(tenant_id)
    if owners is not None:
        multi_block = [section_id for section_id in multi_block if owners.section(section_id)]
    if multi_block:
        ids = ", ".join(multi_block)
        reasons.append(
            f"authorial sections must have exactly one active block; "
            f"these have more than one: {ids}"
        )

    # Block bodies live on the node. An active block with no text publishes its
    # section blank, which is a silent deletion from the skill rather than a
    # visible failure - so it is a visible failure here instead.
    bodyless = store.active_blocks_without_body(tenant_id)
    if owners is not None:
        bodyless = [block_id for block_id in bodyless if owners.block(block_id)]
    if bodyless:
        ids = ", ".join(bodyless)
        reasons.append(
            f"these active content blocks have no text and would publish their "
            f"section empty; run scripts/migrate_block_bodies.py: {ids}"
        )

    for i, text in enumerate(rendered_skill_texts):
        if not _has_frontmatter(text):
            reasons.append(f"skill #{i} has malformed frontmatter")

    # Artefact fidelity: a skill's reference docs and scripts must be
    # restorable byte-for-byte, or the published tree silently ships without
    # files the source package had. Blocking beats a quiet partial publish.
    unwritable = _unwritable_artefacts(store, tenant_id, blob_store, select)
    if unwritable:
        cause = ("no blob store is wired to the publisher"
                 if blob_store is None else "their blobs are missing")
        reasons.append(
            f"artefacts cannot be restored ({cause}): {', '.join(unwritable)}"
        )

    return GateResult(passed=not reasons, reasons=reasons)
