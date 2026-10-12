"""Resolve imported skills by source before any part of an import is written.

A frontmatter name is local to a package. Keeping package directories in the
source key prevents unrelated `run` skills from becoming versions of each other.
"""
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
from pathlib import PurePosixPath

from oms.domain.ids import slug
from oms.domain.types import SignalType


class ImportIdentityConflict(ValueError):
    """An import cannot identify its target without guessing."""


def canonical_source(source_ref: str, name: str) -> str:
    path = PurePosixPath(source_ref)
    # A bare skill folder and the publisher's skills/<folder> layout mean the
    # same thing. Never strip an enclosing package: agenthub/skills/run and
    # autoresearch-agent/skills/run must retain different identities.
    if not path.is_absolute():
        if len(path.parts) == 1:
            return f"skills/{slug(name)}/{path.name}"
        if len(path.parts) == 2:
            return f"skills/{path.as_posix()}"
    return path.as_posix()


def source_skill_id(name: str, source_ref: str) -> str:
    digest = hashlib.sha256(f"{name}\0{source_ref}".encode()).hexdigest()[:12]
    return f"{slug(name)[:64] or 'skill'}--{digest}"


def import_transaction_id(tenant_id: str, source_ref: str, revision: str) -> str:
    # Slugging alone aliases paths such as a_b and a-b, corrupting provenance.
    digest = hashlib.sha256(source_ref.encode()).hexdigest()[:12]
    return f"import-{tenant_id}-{slug(source_ref)[:96]}-{digest}-{revision}"


def recorded_sources(store, queue, skill) -> set[str]:
    """Recover pre-binding provenance, including ruleless imported skills."""
    if skill.import_source_ref:
        return {skill.import_source_ref}
    sources = set()
    for rule in store.rules_for_skill(skill.id, tenant_id=skill.tenant_id):
        sources.update(t.source_ref for t in store.lineage(rule.id)
                       if t.tenant_id == skill.tenant_id
                       and t.signal_type is SignalType.SKILL_IMPORT and t.source_ref)
    sections = store.sections_for_skill(skill.id, tenant_id=skill.tenant_id)
    for section in sections:
        for block in store.blocks_for_section(section.id, tenant_id=skill.tenant_id):
            source = block.source_ref.partition("#")[0]
            if source.endswith("/SKILL.md") or source == "SKILL.md":
                sources.add(source)
    # A second, different prose-only source was parked rather than installed.
    section_ids = {section.id for section in sections}
    for item in [*queue.pending(skill.tenant_id), *queue.history(skill.tenant_id)]:
        if item.kind == "block_revision" and item.other_id in section_ids and item.transaction_id:
            txn = store.get_transaction(item.transaction_id)
            if txn and txn.tenant_id == skill.tenant_id and txn.signal_type is SignalType.SKILL_IMPORT:
                if txn.source_ref:
                    sources.add(txn.source_ref)
    return {canonical_source(source, skill.id) for source in sources}


def resolve_identities(skills, store, queue, tenant_id):
    """Read-only, whole-batch preflight shared by upload preview and apply."""
    stored = store.skills_for_tenant(tenant_id)
    bindings = {}
    for existing in stored:
        if existing.import_source_ref:
            bindings.setdefault(existing.import_source_ref, []).append(existing)
    counts = Counter(skill.id for skill in skills)
    resolved, claimed, claimed_sources = [], set(), set()
    for parsed in skills:
        source = canonical_source(parsed.source_ref, parsed.id)
        matches = bindings.get(source, [])
        existing = store.get_skill(parsed.id, tenant_id=tenant_id)
        target = None
        if len(matches) > 1:
            raise ImportIdentityConflict(f"More than one skill owns {source!r}; resolve its source bindings before importing.")
        if matches:
            target = matches[0].id
        elif existing and existing.tenant_id == tenant_id:
            if (existing.import_source_ref and
                    source == f"skills/{existing.id}/SKILL.md"):
                # Publisher writes the resolved ID as name. This exact export
                # alias is explicit; matching a suffix or title would not be.
                target, source = existing.id, existing.import_source_ref
            elif not existing.import_source_ref:
                sources = recorded_sources(store, queue, existing)
                if len(sources) > 1:
                    raise ImportIdentityConflict(
                        f"Skill {existing.id!r} contains multiple imported sources: {', '.join(sorted(sources))}. "
                        "Import these sources into a clean workspace to keep them separate. No changes were applied.")
                if sources == {source} or (not sources and counts[parsed.id] == 1
                                          and source == f"skills/{slug(parsed.id)}/SKILL.md"):
                    target = existing.id
                elif not sources and counts[parsed.id] > 1:
                    raise ImportIdentityConflict(
                        f"Existing skill {existing.id!r} has no source binding and this import contains that name more than once. "
                        "Give the incoming skills distinct frontmatter names before importing.")
        if target is None:
            conventional = (source == f"skills/{slug(parsed.id)}/SKILL.md"
                            or PurePosixPath(source).is_absolute())
            candidate = parsed.id if conventional else source_skill_id(parsed.id, source)
            owner = store.get_skill(candidate, tenant_id=tenant_id)
            if owner is not None:
                # Never overwrite another source in the admitted workspace.
                candidate = source_skill_id(parsed.id, source)
                owner = store.get_skill(candidate, tenant_id=tenant_id)
                if owner is not None:
                    raise ImportIdentityConflict(
                        f"Import identity for {source!r} is already occupied; give this skill a distinct frontmatter name.")
            target = candidate
        if target in claimed or source in claimed_sources:
            raise ImportIdentityConflict(
                f"More than one file would update skill {target!r}. Import each source only once; no changes were applied.")
        claimed.add(target)
        claimed_sources.add(source)
        resolved.append(replace(parsed, id=target, source_ref=source,
                                import_name=parsed.id))
    resolve_routes(resolved, stored)
    return resolved


def resolve_routes(skills, stored):
    # Routing names inside a package refer to that package's skills. Resolve
    # them after the whole batch is known, including forward references.
    local_targets = defaultdict(set)
    for existing in stored:
        if existing.import_source_ref and existing.import_name:
            scope = str(PurePosixPath(existing.import_source_ref).parent.parent)
            local_targets[(scope, existing.import_name)].add(existing.id)
    for skill in skills:
        scope = str(PurePosixPath(skill.source_ref).parent.parent)
        local_targets[(scope, skill.import_name)].add(skill.id)
    for skill in skills:
        scope = str(PurePosixPath(skill.source_ref).parent.parent)
        def resolve_target(target):
            candidates = local_targets[(scope, target.skill_id)]
            if len(candidates) > 1:
                raise ImportIdentityConflict(
                    f"Routing target {target.skill_id!r} in {skill.source_ref!r} matches more than one skill in that package. "
                    "Give the targets distinct frontmatter names or use their resolved skill IDs; no changes were applied.")
            return replace(target, skill_id=next(iter(candidates)) if candidates else target.skill_id)
        skill.sections = [replace(section, routing_targets=[resolve_target(target)
                           for target in section.routing_targets]) for section in skill.sections]
