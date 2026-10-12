"""Local skill custody and manual learning HTTP routes."""
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import ZipFile, ZIP_STORED

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from oms.community.workflow import Decision
from oms.community.documents import document_routes
from oms.community.publication import DESTINATION_KEYS, destination_settings, publish_destination
from oms.publish.git import CheckoutBusyError
from oms.domain.ids import constraint_id
from oms.domain.models import Constraint
from oms.domain.types import ConstraintSource, ConstraintStatus, RuleStatus, SkillVersionCause, SourceRuntime
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.schema import ExecutionContext
from oms.ingestion.sanitiser import RegexSanitiser
from oms.security.injection import DeterministicScreen
from oms.publish.publisher import Publisher
from oms.skills.history import SkillHistory
from oms.web.capabilities import RouteBundle
from oms.settings.publication import publication_settings


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalContributionRequest(ContributionRequest):
    model_config = ConfigDict(extra="forbid")


class SkillCreate(Body):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    domain: str = Field(default="general", max_length=200)


class SkillPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    domain: str | None = Field(default=None, max_length=200)
    publish_enabled: bool | None = Field(default=None, strict=True)


class TextBody(Body):
    text: str = Field(min_length=1, max_length=1_000_000)


class RulePatch(Body):
    action: Literal["retract", "restore"]
    body: str | None = Field(default=None, min_length=1, max_length=10_000)


class ConstraintBody(Body):
    body: str = Field(min_length=1, max_length=10_000)


class ConstraintPatch(Body):
    body: str | None = Field(default=None, min_length=1, max_length=10_000)
    status: Literal["active", "retired"] | None = None


class ImportDecision(Body):
    action: Literal["accept", "reject"]
    body: str | None = Field(default=None, min_length=1, max_length=1_000_000)


class SettingsPatch(Body):
    body_budget: int | None = Field(default=None, ge=20, le=10_000, strict=True)
    skill_history_keep: int | None = Field(default=None, ge=1, le=10_000, strict=True)
    root_instruction_files: list[Literal["AGENTS.md", "CLAUDE.md"]] | None = None
    publication_target: Literal["local", "git"] | None = None
    publication_folder: str | None = Field(default=None, max_length=1000)
    publication_git_url: str | None = Field(default=None, max_length=2000)
    publication_git_branch: str | None = Field(default=None, max_length=200)


def _safe_text(body):
    if not body.strip():
        raise ValueError("the wording must not be empty")
    sanitised = RegexSanitiser().sanitise(ExecutionContext(
        user_input="", agent_raw_output="", user_correction=body)).context.user_correction
    if sanitised != body:
        raise ValueError("remove personal information before saving this wording")
    if DeterministicScreen().screen(body).score >= 0.5:
        raise ValueError("this wording requires a safety review in the correction inbox")
    return body


def build_routes(services):
    s = services
    tenant = s.settings.tenant_id
    actor = s.principal_resolver.resolve().id
    router = APIRouter(prefix="/api")
    manual = APIRouter(prefix="/api")

    def require_skill(skill_id):
        skill = s.store.get_skill(skill_id, tenant_id=tenant)
        if skill is None or skill.tenant_id != tenant:
            raise HTTPException(404, "skill not found in this workspace")
        return skill

    def rule_view(rule):
        return {**asdict(rule), "skill_ids": [sk.id for sk in s.store.skills_for_rule(rule.id, tenant_id=tenant)],
                "transaction_ids": [t.id for t in s.store.lineage(rule.id)]}

    def publisher(store=None):
        config = local_settings()
        return Publisher(store or s.store, blob_store=s.blobs,
                         body_budget=config["body_budget"],
                         root_instruction_files=config["root_instruction_files"],
                         contribution_endpoint=s.settings.contribution_endpoint,
                         mcp_endpoint=s.settings.mcp_endpoint,
                         distribution_repo=(config["publication_git_url"] if config["publication_target"] == "git" else None))

    def history(store):
        return SkillHistory(store=store, publisher=publisher(store),
                            keep=lambda _: local_settings()["skill_history_keep"])

    def mutate_skill(skill_id, change, cause=SkillVersionCause.CONSOLE_EDIT):
        def apply(store, queue):
            result = change(s.skills.using(store, queue))
            history(store).capture_required(skill_id, tenant, cause=cause, actor=actor)
            return result
        return s.repository.atomic(f"edit-{skill_id}", tenant, apply)

    @router.get("/health")
    def health():
        return {"status": "ok", "edition": "community", "workspace": tenant,
                "notifications": s.notifier.status, "retention": s.retention.status,
                "network_exposure_acknowledged": s.settings.acknowledge_network_exposure,
                "skills": len(s.store.skills_for_tenant(tenant)),
                "manual_inbox": len(s.manual.inbox(tenant))}

    @router.get("/skills")
    def skills():
        rows = s.store.skills_for_tenant(tenant)
        linked = s.source_reads.skill_sources(s.sources.policy.context(), [sk.id for sk in rows]) if s.source_reads is not None else {}
        return [{**asdict(sk), "source": linked[sk.id].model_dump(mode="json") if sk.id in linked else None} for sk in rows]

    @router.get("/skill-changes")
    def skill_changes():
        return [asdict(change) for change in s.store.recent_skill_changes(tenant, limit=8)]

    @router.post("/skills", status_code=201)
    def create_skill(body: SkillCreate):
        def apply(store, queue):
            result = s.skills.using(store, queue).create_skill(tenant, actor=actor, **body.model_dump())
            history(store).capture_required(result.id, tenant, cause=SkillVersionCause.CREATED, actor=actor)
            return result
        return s.repository.atomic("create-skill", tenant, apply)

    @router.get("/skills/{skill_id}")
    def skill(skill_id: str):
        found = require_skill(skill_id)
        return {**asdict(found), "body": publisher().render_package(found).skill_md,
                "rules": [rule_view(r) for r in s.store.rules_for_skill(skill_id, tenant_id=tenant)],
                "sections": [asdict(section) for section in s.skills.sections(skill_id, tenant)],
                "artefacts": [{"path": path, "size": artefact.size,
                                "digest": artefact.content_ref} for artefact, path in s.store.artefacts_for_skill(skill_id, tenant_id=tenant)],
                "versions": [asdict(v) for v in s.store.skill_versions(tenant, skill_id)]}

    @router.patch("/skills/{skill_id}")
    def update_skill(skill_id: str, body: SkillPatch):
        require_skill(skill_id)
        def apply(store, queue):
            result = s.skills.using(store, queue).update_metadata(skill_id, tenant, actor=actor,
                **body.model_dump(exclude_unset=True, exclude={"publish_enabled"}))
            if body.publish_enabled is not None:
                result.publish_enabled = body.publish_enabled
                store.upsert_skill(result)
            history(store).capture_required(skill_id, tenant, cause=SkillVersionCause.CONSOLE_EDIT, actor=actor)
            return result
        return s.repository.atomic(f"edit-{skill_id}", tenant, apply)

    @router.delete("/skills/{skill_id}")
    def delete_skill(skill_id: str):
        def apply(store, queue):
            service = s.skills.using(store, queue)
            service.skill(skill_id, tenant)
            sections = {section.id for section in store.sections_for_skill(skill_id, tenant_id=tenant)}
            report = service.delete_skill(skill_id, tenant, actor=actor)
            # These reviews cannot be decided once their section is gone.
            # Rule reviews stay: their rules and any other memberships survive.
            for item in queue.pending(tenant):
                if item.kind == "block_revision" and item.other_id in sections:
                    queue.resolve(item.id, "skill_deleted", decided_by=actor)
            return {"deleted": report.skill_id, "name": report.name,
                    "rules_detached": report.rules_detached}
        return s.repository.atomic(f"edit-{skill_id}", tenant, apply)

    @router.put("/skills/{skill_id}/sections/{section_id}")
    def update_section(skill_id: str, section_id: str, body: TextBody):
        require_skill(skill_id)
        _safe_text(body.text)
        mutate_skill(skill_id, lambda service: service.revise_section(
            skill_id, section_id, tenant, body.text, actor=actor))
        return {"saved": True}

    @router.get("/rules")
    def rules():
        return [rule_view(r) for r in s.store.rules_for_tenant(tenant, limit=10_000)]

    @router.patch("/rules/{rule_id}")
    def update_rule(rule_id: str, body: RulePatch):
        def apply(store, queue):
            rule = store.get_rule(rule_id)
            if rule is None or rule.tenant_id != tenant:
                raise HTTPException(404, "rule not found in this workspace")
            if body.body is not None:
                rule.body = _safe_text(body.body.strip())
            if body.action == "restore" and rule.plane.value != "data":
                raise ValueError("a control-plane rule cannot be published")
            rule.status = RuleStatus.RETIRED if body.action == "retract" else RuleStatus.ACTIVE
            store.upsert_rule(rule)
            for item in queue.pending(tenant):
                if item.subject_id == rule_id and item.kind in ("injection", "removal", "polarity_conflict"):
                    queue.resolve(item.id, body.action, decided_by=actor)
            for selected in store.skills_for_rule(rule.id, tenant_id=tenant):
                history(store).capture_required(selected.id, tenant,
                    cause=SkillVersionCause.RULE_EDIT, actor=actor, detail=body.action)
            return rule
        return s.repository.atomic(f"rule-edit-{rule_id}", tenant, apply)

    @router.get("/constraints")
    def constraints():
        return s.store.all_constraints(tenant)

    @router.post("/constraints", status_code=201)
    def add_constraint(body: ConstraintBody):
        text = _safe_text(body.body.strip())
        result = Constraint(id=constraint_id(tenant, text, console=True), body=text,
                            tenant_id=tenant, source=ConstraintSource.CONSOLE)
        s.store.upsert_constraint(result)
        return result

    @router.patch("/constraints/{constraint_id}")
    def update_constraint(constraint_id: str, body: ConstraintPatch):
        def apply(store, queue):
            found = next((item for item in store.all_constraints(tenant) if item.id == constraint_id), None)
            if found is None:
                raise HTTPException(404, "constraint not found in this workspace")
            if found.source is ConstraintSource.IMPORT:
                raise HTTPException(409, "edit imported constraints in their source file and import it again")
            if body.body is not None:
                found.body = _safe_text(body.body.strip())
            if body.status is not None:
                found.status = ConstraintStatus(body.status)
            store.upsert_constraint(found)
            return found
        return s.repository.atomic(f"constraint-edit-{constraint_id}", tenant, apply)

    @router.get("/import-review")
    def import_review():
        return s.import_review.inbox(tenant)

    @router.post("/import-review/{item_id}/decision")
    def decide_import(item_id: str, body: ImportDecision):
        return s.import_review.decide(item_id, tenant, actor, **body.model_dump())

    @router.post("/import")
    async def import_archive(file: UploadFile = File(...)):
        archive = await file.read(25_000_001)
        if len(archive) > 25_000_000:
            raise HTTPException(413, "archive exceeds 25 MB")
        staged = s.uploads.stage(archive, tenant, filename=file.filename or "upload.zip")
        if s.sources is not None:
            from oms.sources.local_upload import stage_view
            return stage_view(staged)
        # The local operator's upload is itself the explicit import command.
        return asdict(s.uploads.apply(staged.id, tenant))

    @router.post("/import/directory")
    async def import_directory(files: list[UploadFile] = File(...)):
        if not files or len(files) > 1000:
            raise HTTPException(413, "choose a directory containing at most 1,000 files")
        # Keep browser relative paths, then use the ZIP validation and custody
        # path. Nothing from an uploaded script is executed.
        buffer = BytesIO()
        total = 0
        with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
            for file in files:
                content = await file.read(25_000_001 - total)
                total += len(content)
                if total > 25_000_000:
                    raise HTTPException(413, "directory exceeds 25 MB")
                archive.writestr(file.filename or "", content)
        staged = s.uploads.stage(buffer.getvalue(), tenant, filename="directory.zip")
        if s.sources is not None:
            from oms.sources.local_upload import stage_view
            return stage_view(staged)
        return asdict(s.uploads.apply(staged.id, tenant))

    @manual.post("/ingest", status_code=202)
    def ingest(body: LocalContributionRequest):
        txn = s.coordinator.ingest(correction_payload(body, s.principal_resolver.resolve(), SourceRuntime.HTTP),
                                   s.principal_resolver.resolve())
        # `status` is the receipt every other door gives (the MCP tools
        # answer the same), and the bundled helper's command line reads it
        # to tell an accepted contribution from a failed one.
        return {"transaction_id": txn.id, "status": "accepted", "state": txn.workflow_state}

    @manual.get("/review")
    def review():
        s.coordinator.reconcile(tenant)
        return s.manual.inbox(tenant)

    @manual.post("/review/{transaction_id}/decision")
    def decide(transaction_id: str, body: Decision):
        return s.manual.decide(transaction_id, tenant, actor, body)

    @router.get("/publish/preview")
    def preview(skill_id: str):
        selected = require_skill(skill_id)
        return {"skill_id": selected.id, "body": publisher().render_package(selected).skill_md}

    @router.post("/publish")
    def publish():
        try:
            gate, result = publish_destination(s, publisher())
        except CheckoutBusyError:
            raise HTTPException(409, "another publication is in progress; try again when it finishes") from None
        except OSError:
            raise HTTPException(422, "the publication folder is not writable; check its server mount and permissions") from None
        if not gate.passed:
            raise HTTPException(409, "publication held: " + "; ".join(gate.reasons))
        return result

    def graph_node(node, kind):
        return {"id": node.id, "name": getattr(node, "name", getattr(node, "body", node.id)),
                "kind": kind, "properties": asdict(node)}

    @router.get("/graph")
    def graph(q: str = "", skill_id: str | None = None):
        if skill_id:
            return {"nodes": [graph_node(require_skill(skill_id), "skill")], "edges": []}
        skills = s.store.skills_for_tenant(tenant)
        rules = s.store.rules_for_tenant(tenant, limit=1000)
        nodes = [graph_node(n, "skill") for n in skills] + [graph_node(n, "rule") for n in rules]
        selected = [n for n in nodes if q.casefold() in str(n["name"]).casefold()][:500]
        ids = {n["id"] for n in selected}
        edges = [{"source": rule.id, "target": skill.id, "type": "BELONGS_TO"}
                 for rule in rules if rule.id in ids
                 for skill in s.store.skills_for_rule(rule.id, tenant_id=tenant) if skill.id in ids]
        return {"nodes": selected, "edges": edges}

    @router.get("/graph/nodes/{node_id}")
    def node(node_id: str):
        found = s.store.graph_node(node_id, tenant)
        if found is not None:
            return found
        raise HTTPException(404, "node not found in this workspace")

    @router.get("/graph/nodes/{node_id}/neighbours")
    def neighbours(node_id: str, offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=100, ge=1, le=200)):
        node(node_id)
        return s.store.graph_neighbours(node_id, tenant, offset=offset, limit=limit)

    @router.get("/settings")
    def local_settings():
        return {**destination_settings(s.settings, s.settings_store, tenant),
                **publication_settings(s.settings_store, tenant)}

    @router.patch("/settings")
    def update_settings(body: SettingsPatch):
        changes = body.model_dump(exclude_unset=True)
        destination_changes = {key: value for key, value in changes.items() if key in DESTINATION_KEYS}
        if any(value is None for value in destination_changes.values()):
            raise ValueError("publication destination values cannot be null")
        if destination_changes:
            validated = destination_settings(s.settings, s.settings_store, tenant, destination_changes)
            changes.update({key: validated[key] for key in DESTINATION_KEYS})
        s.settings_store.update(tenant, changes)
        return local_settings()

    # Child routes already carry /api/skills; include without the /api prefix.
    documents = document_routes(s, publisher, history, _safe_text)
    custody = APIRouter()
    custody.include_router(router)
    custody.include_router(documents)
    bundles = (RouteBundle("local_custody", custody, required_services=("store", "skills", "history", "publisher", "uploads", "settings_store")),
               RouteBundle("manual_learning", manual, frozenset({"manual_learning"}), ("coordinator", "manual")))
    if s.sources is not None:
        from oms.sources.api import source_routes
        bundles += (source_routes(s.sources, s.source_reads,
                    lambda request, action: s.sources.policy.context(), uploads=s.uploads),)
    return bundles
