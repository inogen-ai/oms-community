from dataclasses import dataclass, field

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


def _unwritable_artefacts(store: GraphStore, tenant_id: str,
                          blob_store: BlobStore | None) -> list[str]:
    """Artefact paths that publish could not restore: every artefact when no
    blob store is wired, else those whose content_ref does not resolve."""
    missing: list[str] = []
    for skill in store.skills_for_tenant(tenant_id):
        for artefact, path in store.artefacts_for_skill(skill.id):
            if blob_store is None or not blob_store.exists(artefact.content_ref):
                missing.append(f"skills/{skill.id}/{path}")
    return missing


def run_publish_gate(store: GraphStore, tenant_id: str,
                     rendered_skill_texts: list[str],
                     blob_store: BlobStore | None = None) -> GateResult:
    reasons: list[str] = []

    # Honour a stored publication block without changing its policy or source.
    block = store.get_publish_block(tenant_id)
    if block is not None:
        detail = "; ".join(block.reasons) if block.reasons else "no reason recorded"
        reasons.append(f"publishing is blocked by the {block.source} check: {detail}")

    # Recorded constraint conflicts block publication independently of the flag.
    conflicting = store.rules_conflicting_with_constraints(tenant_id)
    if conflicting:
        ids = ", ".join(r.id for r in conflicting)
        reasons.append(f"rules conflict with a constraint and must not publish: {ids}")

    # Custody invariant (Section 7.3): each authorial section must hold exactly
    # one active ContentBlock. Any section with more breaks supersession custody.
    multi_block = store.sections_with_multiple_active_blocks(tenant_id)
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
    unwritable = _unwritable_artefacts(store, tenant_id, blob_store)
    if unwritable:
        cause = ("no blob store is wired to the publisher"
                 if blob_store is None else "their blobs are missing")
        reasons.append(
            f"artefacts cannot be restored ({cause}): {', '.join(unwritable)}"
        )

    return GateResult(passed=not reasons, reasons=reasons)
