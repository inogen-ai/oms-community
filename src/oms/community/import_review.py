"""Explicit local decisions about imported rules and authorial source edits."""
import hashlib
import json

from oms.community.workflow import DecisionConflict, ManualDecisionError
from oms.domain.types import RuleStatus, SkillVersionCause
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext
from oms.security.injection import DeterministicScreen
from oms.skills.service import SkillAdminService

KINDS = frozenset({"block_revision", "removal", "injection", "polarity_conflict"})


class ImportReviewService:
    def __init__(self, repository, history_factory):
        self.repository, self.history_factory = repository, history_factory

    def inbox(self, tenant_id):
        store = self.repository.store
        rows = []
        for item in self.repository.queue.pending(tenant_id):
            if item.kind not in KINDS:
                continue
            section = store.get_section(item.other_id) if item.kind == "block_revision" else None
            rule = store.get_rule(item.subject_id) if item.kind != "block_revision" else None
            if (section is not None and section.tenant_id != tenant_id
                    or rule is not None and rule.tenant_id != tenant_id):
                continue
            skills = store.skills_for_rule(rule.id) if rule and rule.tenant_id == tenant_id else []
            rows.append({"id": item.id, "kind": item.kind, "subject_id": item.subject_id,
                "other_id": item.other_id, "reason": item.reason,
                "proposed_body": item.proposed_body or (rule.body if rule else ""),
                "skill_id": section.skill_id if section and section.tenant_id == tenant_id else
                             (skills[0].id if skills else None)})
        return rows

    def decide(self, item_id, tenant_id, actor, *, action, body=None):
        if action not in ("accept", "reject"):
            raise ManualDecisionError("choose accept or reject")
        fingerprint = "local-import-" + hashlib.sha256(json.dumps(
            {"action": action, "body": body}, sort_keys=True).encode()).hexdigest()

        def apply(store, queue):
            item = queue.get(item_id)
            if item is None or item.tenant_id != tenant_id or item.kind not in KINDS:
                raise ManualDecisionError("import review not found in this workspace")
            if item.resolved:
                if item.resolution != fingerprint:
                    raise DecisionConflict("this import review already has a different decision")
                return {"id": item.id, "resolved": True}
            affected = []
            if item.kind == "block_revision":
                section = store.get_section(item.other_id)
                if section is None or section.tenant_id != tenant_id:
                    raise ManualDecisionError("the reviewed section no longer exists")
                if action == "accept":
                    active = store.blocks_for_section(section.id)
                    if len(active) != 1 or active[0].id != item.subject_id:
                        raise DecisionConflict("the section changed; review its current text before editing")
                    wording = item.proposed_body if body is None else body
                    self._safe_edit(wording)
                    SkillAdminService(store=store).revise_section(section.skill_id,
                        section.id, tenant_id, wording, actor=actor)
                    affected = [section.skill_id]
            else:
                rule = store.get_rule(item.subject_id)
                if rule is None or rule.tenant_id != tenant_id:
                    raise ManualDecisionError("the reviewed rule no longer exists")
                if body is not None and body != rule.body:
                    self._safe_edit(body)
                    rule.body = body.strip()
                if item.kind == "removal":
                    rule.status = RuleStatus.RETIRED if action == "accept" else RuleStatus.ACTIVE
                else:
                    # Injection: accept the screened original explicitly, or
                    # retire it. Polarity: keep the earlier rule, or retire it
                    # in favour of the other imported rule. No semantic choice
                    # is made by this service.
                    rule.status = RuleStatus.ACTIVE if action == "accept" else RuleStatus.RETIRED
                if rule.status is RuleStatus.ACTIVE and rule.plane.value != "data":
                    raise ManualDecisionError("a control-plane rule cannot be published")
                store.upsert_rule(rule)
                affected = [skill.id for skill in store.skills_for_rule(rule.id)]
            for skill_id in affected:
                self.history_factory(store).capture_required(skill_id, tenant_id,
                    cause=SkillVersionCause.RULE_EDIT, actor=actor,
                    detail=f"local import review: {item.kind} {action}")
            queue.resolve(item.id, fingerprint, decided_by=actor)
            return {"id": item.id, "resolved": True}

        return self.repository.atomic(f"import-review-{item_id}", tenant_id, apply)

    @staticmethod
    def _safe_edit(body):
        if not body or not body.strip():
            raise ManualDecisionError("the reviewed wording must not be empty")
        clean = RegexSanitiser().sanitise(ExecutionContext(
            user_input="", agent_raw_output="", user_correction=body)).context.user_correction
        if clean != body or DeterministicScreen().screen(body).score >= 0.5:
            raise ManualDecisionError("remove personal information or unsafe instructions from edited wording")
