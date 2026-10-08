"""Community document editing over the same outline and history as publication."""
from dataclasses import asdict
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from oms.domain.types import Plane, RuleStatus, SkillVersionCause
from oms.publish.parts import document_revision
from oms.skills.compare import compare_parts, version_markdown
from oms.community.placement import insertion_sections


class PartEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=1_000_000)


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    parts: list[PartEdit] = Field(min_length=1, max_length=1000)


class RestoreParts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    anchors: list[str] = Field(min_length=1, max_length=1000)


def document_routes(services, publisher, history, safe_text):
    s = services
    tenant = s.settings.tenant_id
    actor = s.principal_resolver.resolve().id
    router = APIRouter(prefix="/api/skills")

    def require_skill(store, skill_id):
        found = store.get_skill(skill_id, tenant_id=tenant)
        if found is None or found.tenant_id != tenant:
            raise HTTPException(404, "skill not found in this workspace")
        return found

    def outline(store, skill_id):
        require_skill(store, skill_id)
        parts, path = publisher(store).outline_skill(skill_id, tenant)
        return {"skill_id": skill_id, "revision": document_revision(parts),
                "rule_sections": insertion_sections(store, skill_id, tenant_id=tenant),
                "path": path, "parts": [{**asdict(part), "affected_skills": [
                    {"id": skill.id, "name": skill.name} for skill in
                    (store.skills_for_rule(part.source_id, tenant_id=tenant) if part.kind in ("rule", "overflow-rule")
                     else [store.get_skill(skill_id, tenant_id=tenant)]) if skill.tenant_id == tenant]}
                    for part in parts]}

    def require_revision(document, revision):
        if document["revision"] != revision:
            raise HTTPException(409, "This document has changed. Reload it before saving your edits.")

    def edit_part(service, skill_id, anchor, text):
        safe_text(text)
        if anchor == "title":
            service.update_metadata(skill_id, tenant, name=text, actor=actor)
        elif anchor == "description":
            service.update_metadata(skill_id, tenant, description=text, actor=actor)
        elif anchor.startswith("section:"):
            service.rename_section(skill_id, anchor[8:], tenant, text, actor=actor)
        elif anchor.startswith("block:"):
            service.revise_block(skill_id, anchor[6:], tenant, text, actor=actor)
        elif anchor.startswith("rule:"):
            service.edit_rule(skill_id, anchor[5:], tenant, text, actor=actor)
        else:
            raise HTTPException(422, "This document part is generated and cannot be edited.")

    def capture(store, skill_id, rule_ids, **kwargs):
        affected = {skill_id}
        for rule_id in rule_ids:
            affected.update(sk.id for sk in store.skills_for_rule(rule_id, tenant_id=tenant) if sk.tenant_id == tenant)
        for affected_id in affected:
            history(store).capture_required(affected_id, tenant, actor=actor, **kwargs)

    @router.get("/{skill_id}/document")
    def document(skill_id: str):
        return outline(s.store, skill_id)

    @router.put("/{skill_id}/document")
    def save_document(skill_id: str, body: DocumentEdit):
        def apply(store, queue):
            current = outline(store, skill_id)
            require_revision(current, body.revision)
            editable = {p["anchor"] for p in current["parts"] if p["editable"]}
            anchors = [p.anchor for p in body.parts]
            if len(set(anchors)) != len(anchors) or not set(anchors) <= editable:
                raise HTTPException(422, "Choose each editable document part once.")
            service = s.skills.using(store, queue)
            problems = service.check_document(skill_id, tenant, [(p.anchor, p.text) for p in body.parts])
            if problems:
                raise HTTPException(422, "; ".join(message for _, message in problems))
            for part in body.parts:
                edit_part(service, skill_id, part.anchor, part.text)
            capture(store, skill_id, [a[5:] for a in anchors if a.startswith("rule:")],
                    cause=SkillVersionCause.CONSOLE_EDIT, detail="edited document")
            return outline(store, skill_id)
        return s.repository.atomic(f"edit-{skill_id}", tenant, apply)

    def version(store, skill_id, version_id):
        require_skill(store, skill_id)
        found = store.get_skill_version(version_id, tenant_id=tenant)
        if found is None or found.tenant_id != tenant or found.skill_id != skill_id:
            raise HTTPException(404, "version not found for this skill")
        return found

    def comparison(store, skill_id, version_id):
        saved = version(store, skill_id, version_id)
        current = outline(store, skill_id)
        def status(rule_id):
            rule = store.get_rule(rule_id)
            if rule is None or rule.tenant_id != tenant or rule.plane is not Plane.DATA:
                return None
            if skill_id not in {sk.id for sk in store.skills_for_rule(rule_id, tenant_id=tenant)}:
                return None
            return rule.status.value
        rows = compare_parts(json.loads(saved.parts_json), current["parts"],
                             rule_status=status, staging=True)
        return {"version": asdict(saved), "revision": current["revision"],
                "markdown": version_markdown(json.loads(saved.parts_json)),
                "rows": [asdict(row) for row in rows]}

    @router.get("/{skill_id}/versions")
    def versions(skill_id: str):
        require_skill(s.store, skill_id)
        # Import and every console write already capture. A safety-net read also
        # records edits made by older clients before comparing today's document.
        s.repository.atomic(f"edit-{skill_id}", tenant, lambda store, queue:
            history(store).capture_required(skill_id, tenant, cause=SkillVersionCause.UNATTRIBUTED))
        return [asdict(v) for v in s.store.skill_versions(tenant, skill_id, limit=10_000)]

    @router.get("/{skill_id}/versions/{version_id}/compare")
    def compare_version(skill_id: str, version_id: str):
        return comparison(s.store, skill_id, version_id)

    @router.post("/{skill_id}/versions/{version_id}/restore")
    def restore(skill_id: str, version_id: str, body: RestoreParts):
        def apply(store, queue):
            compared = comparison(store, skill_id, version_id)
            require_revision(compared, body.revision)
            rows = {row["anchor"]: row for row in compared["rows"]}
            anchors = set(body.anchors)
            if len(anchors) != len(body.anchors) or any(a not in rows or not rows[a]["restorable"] for a in anchors):
                raise HTTPException(422, "One or more selected changes cannot be restored.")
            service = s.skills.using(store, queue)
            touched = []
            for row in compared["rows"]:
                if row["anchor"] not in anchors:
                    continue
                rule_id = row["rule_id"]
                if rule_id and row["state"] in ("only_old", "only_new"):
                    rule = store.get_rule(rule_id)
                    if rule is None or rule.tenant_id != tenant or rule.plane is not Plane.DATA:
                        raise HTTPException(422, "The selected rule cannot be restored.")
                    if skill_id not in {sk.id for sk in store.skills_for_rule(rule_id, tenant_id=tenant)}:
                        raise HTTPException(422, "The selected rule no longer belongs to this skill.")
                    rule.status = RuleStatus.ACTIVE if row["state"] == "only_old" else RuleStatus.RETIRED
                    if row["state"] == "only_old":
                        rule.body = safe_text(row["old_text"])
                        rule.embedding = None
                    store.upsert_rule(rule)
                else:
                    edit_part(service, skill_id, row["anchor"], row["old_text"])
                if rule_id:
                    touched.append(rule_id)
            capture(store, skill_id, touched, cause=SkillVersionCause.RESTORE,
                    restore_source=version_id,
                    restore_taken=f"{len(anchors)}/{sum(row['state'] != 'same' for row in rows.values())}")
            return outline(store, skill_id)
        return s.repository.atomic(f"edit-{skill_id}", tenant, apply)

    @router.get("/{skill_id}/files")
    def preview_file(skill_id: str, path: str):
        require_skill(s.store, skill_id)
        found = next((a for a, p in s.store.artefacts_for_skill(skill_id, tenant_id=tenant) if p == path), None)
        if found is None:
            raise HTTPException(404, "file not found in this skill")
        if found.size > 200_000:
            return {"path": path, "size": found.size, "text": None, "reason": "This file is too large to preview (200 KB limit)."}
        data = s.blobs.get(found.content_ref)
        try:
            text = data.decode("utf-8")
            if "\x00" in text:
                raise ValueError("binary file")
        except (UnicodeError, ValueError):
            return {"path": path, "size": len(data), "text": None, "reason": "Binary file. It remains available in the published skill folder."}
        return {"path": path, "size": len(data), "text": text}

    return router
