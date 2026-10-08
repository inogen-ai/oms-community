"""The deterministic safety checks a source update has to pass.

Nothing here calls a model. Text goes through the regex sanitiser and the deterministic
injection screen, files are checked as the exact bytes in custody, and a check that
cannot run says so rather than passing, which the plan turns into a hold (spec §7.7).
The live holds stop an update from touching rules that are already held, flagged or
tainted, whatever the upstream text looks like.
"""
from dataclasses import dataclass, field
import logging
from typing import Literal

from oms.domain.types import Plane, RuleStatus, SignalType
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext
from oms.ports.blob_store import BlobStore
from oms.security.constraint_patterns import shared_danger, targets_the_system
from oms.security.injection import DeterministicScreen
from oms.sources.errors import SourceConflict
from oms.sources.files import verified_bytes
from oms.sources.models import CheckResult, ManifestEntry, MergePlan, PartKind, PlanFlag, ProjectionPart, SourceSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyRestriction:
    part_id: str
    reason: str
    allowed_choices: tuple[Literal['keep_oms', 'use_upstream', 'merged_text'], ...] = ('merged_text',)


@dataclass(frozen=True)
class FileCheck:
    part_id: str
    digest: str
    result: CheckResult


@dataclass(frozen=True)
class SourceSafety:
    screen: object = field(default_factory=DeterministicScreen)
    sanitiser: object = field(default_factory=RegexSanitiser)
    max_text_chars: int = 100_000

    def __post_init__(self):
        if type(self.max_text_chars) is not int or self.max_text_chars <= 0:
            raise ValueError('text check budget must be a positive integer')

    def check_text(self, text: str) -> CheckResult:
        # Exact types, not isinstance: a subclass of either could be backed by
        # a model, and the promise that a source check never infers anything
        # (spec §7.7) is kept by refusing to run with anything but these two.
        if type(self.screen) is not DeterministicScreen or type(self.sanitiser) is not RegexSanitiser:
            return CheckResult(state='unavailable', code='deterministic_checks_unavailable')
        if len(text) > self.max_text_chars:
            return CheckResult(state='unavailable', code='text_check_limit')
        try:
            clean = self.sanitiser.sanitise(ExecutionContext(user_input='', agent_raw_output='', user_correction=text))
            screened = self.screen.screen_required(text)
        except Exception:
            logger.warning('Required source text check failed', extra={'code': 'source_check_unavailable'})
            return CheckResult(state='unavailable', code='source_check_unavailable')
        reasons = []
        if clean.context.user_correction != text:
            reasons.append('text_requires_sanitisation')
        if screened.score >= 0.5:
            reasons.append('injection_screen_hold')
        return (CheckResult(state='held', code='source_text_hold', reasons=tuple(reasons)) if reasons
                else CheckResult(state='passed', code='source_text_checked'))

    def check_file(self, body: bytes, *, maximum_bytes: int = 10 * 1024 * 1024) -> CheckResult:
        if len(body) > maximum_bytes:
            return CheckResult(state='held', code='file_size_limit')
        # A binary file gets size and structure checks only; the code says so,
        # and the preview shows it, instead of implying its content was read.
        try:
            text = body.decode('utf-8')
        except UnicodeError:
            return CheckResult(state='passed', code='binary_structure_only')
        if '\x00' in text:
            return CheckResult(state='passed', code='binary_structure_only')
        return self.check_text(text)


def eligibility(plan: MergePlan, *, mode: Literal['manual', 'automatic', 'bulk'],
                automatic_enabled: bool, edition: Literal['community', 'paid']) -> CheckResult:
    if mode not in ('manual', 'automatic', 'bulk') or edition not in ('community', 'paid'):
        raise ValueError('invalid source apply mode')
    reasons = []
    if plan.conflicts:
        reasons.append('unresolved_conflicts')
    if mode == 'automatic':
        if edition != 'paid':
            reasons.append('manual_edition')
        if not automatic_enabled:
            reasons.append('automation_disabled')
    # A clean script change is fine for a reviewer who can see it and for the
    # paid automatic path (spec §8.4); only "apply all clean updates" excludes
    # it, because nobody is looking at that particular skill (spec §8.6).
    blockers = set(plan.flags)
    if mode != 'bulk':
        blockers.discard(PlanFlag.SCRIPT_CHANGES)
    if mode == 'manual':
        # These flags require manual reconciliation but cannot authorise unsafe text.
        blockers -= {PlanFlag.FIRST_RECONCILIATION, PlanFlag.DECLARED_LICENCE_CHANGED,
                     PlanFlag.UNPROVEN_HISTORY, PlanFlag.REWRITTEN_HISTORY}
    reasons.extend(sorted(flag.value for flag in blockers))
    return (CheckResult(state='held', code='apply_ineligible', reasons=tuple(dict.fromkeys(reasons)))
            if reasons else CheckResult(state='passed', code='apply_eligible'))


def screen_snapshot(snapshot: SourceSnapshot, blobs: BlobStore,
                    safety: SourceSafety | None = None) -> CheckResult:
    checks = safety or SourceSafety()
    if not snapshot.manifest:
        return CheckResult(state="unavailable", code="package_manifest_unavailable")
    # The bytes are read back from custody and verified against the manifest
    # digest before anything looks at them, so what a pass attests is the
    # retained package itself, not whatever was in memory at fetch time.
    for entry in snapshot.manifest:
        try:
            body = verified_bytes(blobs, entry)
        except (SourceConflict, OSError):
            return CheckResult(state="unavailable", code="package_custody_unavailable")
        result = checks.check_file(body)
        if result.state != "passed":
            return result
    for part in snapshot.effective_projection:
        if part.evidence.kind == "known":
            for text in texts(part.evidence.value):
                result = checks.check_text(text)
                if result.state != "passed":
                    return result
    return CheckResult(state="passed", code="package_bytes_checked")


def texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from texts(item)


def screen_selected(snapshot: SourceSnapshot, writes: tuple[ProjectionPart, ...],
                    blobs: BlobStore, safety: SourceSafety | None = None, *,
                    manifest: tuple[ManifestEntry, ...] | None = None) -> CheckResult:
    checks = safety or SourceSafety()
    for part in writes:
        if part.evidence.kind == "unknown":
            return CheckResult(state="unavailable", code="write_evidence_unavailable")
        if part.evidence.kind == "absent":
            continue
        if part.kind == PartKind.FILE:
            path = part.part_id.removeprefix("file:")
            entries = snapshot.manifest if manifest is None else manifest
            entry = next((entry for entry in entries if entry.path == path), None)
            if entry is None or part.evidence.source_digest != entry.digest:
                return CheckResult(state="unavailable", code="file_manifest_mismatch")
            try:
                result = checks.check_file(verified_bytes(blobs, entry))
            except (SourceConflict, OSError):
                return CheckResult(state="unavailable", code="package_custody_unavailable")
            if result.state != "passed":
                return result
        else:
            for text in texts(part.evidence.value):
                result = checks.check_text(text)
                if result.state != "passed":
                    return result
    return CheckResult(state="passed", code="resolved_writes_checked")


def _publication_rules(graph, tenant):
    block = graph.get_publish_block(tenant)
    result = set()
    suffixes = (' derives from a suspected prompt injection',
              ' was rejected as a suspected prompt injection, and a paragraph written from it is still active')
    for reason in block.reasons if block is not None else ():
        for suffix in suffixes:
            if reason.startswith('rule ') and reason.endswith(suffix):
                identifier = reason[len('rule '):-len(suffix)]
                if identifier:
                    result.add(identifier)
    return result


def _created_by_injection(graph, item):
    if item.transaction_id:
        return item.subject_id == 'rule-' + item.transaction_id
    if item.id.startswith('injection-' + item.subject_id + '-'):
        return True
    transactions = graph.lineage(item.subject_id)
    if any(row.tenant_id != item.tenant_id for row in transactions):
        return True
    compiled = [row.id for row in transactions if row.signal_type is not SignalType.SKILL_IMPORT]
    return (not compiled or len(compiled) == 1 and item.subject_id == 'rule-' + compiled[0]
            or item.subject_id in _publication_rules(graph, item.tenant_id))


def _rejected_injection(graph, reviews, rule):
    return any(item.kind == 'injection' and _created_by_injection(graph, item) and
        (item.resolution == 'rejected' or (item.resolution or '').startswith('local-import-')
         and rule.status is not RuleStatus.ACTIVE)
        for item in reviews.history(rule.tenant_id, subject_id=rule.id))


def _selected(graph, snapshot, local, writes):
    mappings = {}
    for row in (*snapshot.graph_mappings, *local.graph_mappings):
        mappings.setdefault(row.part_id, set()).update(row.entity_ids)

    def identities(identifier):
        return mappings.get(identifier, {identifier})
    parts = {row.part_id: row for row in local.parts}
    sections = None if writes is None else set()
    rules = ({row.id for row in graph.rules_for_skill(local.skill.skill_id, tenant_id=local.skill.tenant_id)}
             if writes is None else set())
    for part in local.parts if writes is None else writes:
        if part.kind is PartKind.RULE:
            rules.update(identities(part.part_id))
        elif part.kind is PartKind.SECTION and sections is not None:
            sections.update(identities(part.part_id))
        elif part.kind is PartKind.EXAMPLE:
            for version in (part, parts.get(part.part_id)):
                value = version.evidence.value if version is not None and version.evidence.kind == 'known' else None
                if isinstance(value, dict):
                    if value.get('parent_rule_id'):
                        rules.update(identities(value['parent_rule_id']))
                    if value.get('parent_section_id') and sections is not None:
                        sections.update(identities(value['parent_section_id']))
    blocks = {block.id for section in graph.sections_for_skill(local.skill.skill_id, tenant_id=local.skill.tenant_id)
            if sections is None or section.id in sections
            for block in graph.blocks_for_section(section.id, tenant_id=local.skill.tenant_id)}
    return rules, blocks


def live_holds(graph, reviews, snapshot, local, writes=None):
    """Holds that come from the rules already in the graph rather than from the incoming text.

    An update that would rewrite a rule a reviewer is still deciding on, a
    rule in conflict with a constraint, or a rule created by a rejected
    injection, would quietly settle that question for them; and a paragraph
    revised from such a rule carries its taint. The existing safety release
    authority decides those, so the update waits (spec §7.7).
    """
    tenant = local.skill.tenant_id
    rule_ids, blocks = _selected(graph, snapshot, local, writes)
    rules = [row for identifier in sorted(rule_ids) if (row := graph.get_rule(identifier)) is not None]
    if any(row.tenant_id != tenant for row in rules):
        return CheckResult(state='held', code='foreign_rule_hold')
    if any(row.plane is not Plane.DATA for row in rules):
        return CheckResult(state='held', code='control_plane_source_hold')
    if any(row.status in {RuleStatus.PENDING, RuleStatus.FLAGGED} for row in rules):
        return CheckResult(state='held', code='existing_rule_hold')
    conflicts = {row.id for row in graph.rules_conflicting_with_constraints(tenant)}
    if conflicts & rule_ids:
        return CheckResult(state='held', code='existing_constraint_hold')
    pending = {item.subject_id for item in reviews.pending(tenant) if item.kind == 'injection'}
    rejected = {row.id for row in rules if _rejected_injection(graph, reviews, row)}
    if (pending | rejected) & rule_ids:
        return CheckResult(state='held', code='existing_injection_hold')
    lineage_rules = conflicts | _publication_rules(graph, tenant) | pending
    local_rule_parts = {part.part_id for part in local.parts if part.kind is PartKind.RULE}
    for mapping in local.graph_mappings:
        if mapping.part_id in local_rule_parts:
            for identifier in mapping.entity_ids:
                rule = graph.get_rule(identifier)
                if rule is not None and (rule.plane is not Plane.DATA
                                         or rule.status in {RuleStatus.PENDING, RuleStatus.FLAGGED}):
                    lineage_rules.add(identifier)
    for item in reviews.history(tenant):
        if item.kind == 'injection' and item.resolution == 'rejected' and _created_by_injection(graph, item):
            lineage_rules.add(item.subject_id)
    tainted = {block.id for identifier in lineage_rules if graph.get_rule(identifier) is not None
             for block in graph.blocks_revised_for_rule(identifier, tenant_id=tenant)}
    if blocks & tainted:
        return CheckResult(state='held', code='tainted_source_lineage')
    return None


def screen_policy(graph, reviews, blobs, snapshot, local, *, writes=None, manifest=None):
    # A graph or queue without the methods the holds need cannot say the
    # update is safe; it says so, and the plan holds (spec §7.7).
    required = ('active_constraints', 'rules_for_skill', 'sections_for_skill', 'blocks_for_section', 'get_rule',
              'rules_conflicting_with_constraints', 'get_publish_block', 'blocks_revised_for_rule', 'lineage')
    if (graph is None or reviews is None or local.skill != snapshot.ref.origin.skill
            or any(not callable(getattr(graph, name, None)) for name in required)
            or any(not callable(getattr(reviews, name, None)) for name in ('pending', 'history'))):
        return CheckResult(state='unavailable', code='source_policy_state_unavailable')
    checked = (screen_snapshot(snapshot, blobs) if writes is None else
             screen_selected(snapshot, writes, blobs, manifest=manifest))
    if checked.state != 'passed' or writes == ():
        return checked
    parts = snapshot.effective_projection if writes is None else writes
    entries = snapshot.manifest if manifest is None else manifest
    if writes is not None:
        paths = {row.part_id.removeprefix('file:') for row in writes if row.kind is PartKind.FILE
                 and row.evidence.kind == 'known'}
        entries = tuple(entry for entry in entries if entry.path in paths)
    try:
        constraints = tuple(graph.active_constraints(local.skill.tenant_id))
        values = [text for part in parts if part.evidence.kind == 'known' for text in texts(part.evidence.value)]
        for entry in entries:
            try:
                values.append(verified_bytes(blobs, entry).decode('utf-8'))
            except UnicodeDecodeError:
                continue
    except (OSError, SourceConflict):
        return CheckResult(state='unavailable', code='source_policy_state_unavailable')
    if any(shared_danger(text, constraint.body) for text in values for constraint in constraints):
        return CheckResult(state='held', code='source_constraint_conflict')
    if any(part.kind is PartKind.RULE and part.evidence.kind == 'known'
           and isinstance(part.evidence.value, dict)
           and targets_the_system(part.evidence.value.get('body', '')) for part in parts):
        return CheckResult(state='held', code='control_plane_source_hold')
    held = live_holds(graph, reviews, snapshot, local, writes)
    return held if held is not None else checked


def revision_state(graph, reviews, tenant, skills):
    """The live safety state a plan was judged against, as the input to the policy digest.

    A rule's status or plane, a block's status, the publication block, the
    constraint conflicts and the injection history can all change after a plan
    is built; any of them changing changes the digest, and the plan's
    fingerprint no longer matches, so it is rebuilt rather than applied.
    """
    rules = {}
    blocks = []
    for ref in skills:
        for row in graph.rules_for_skill(ref.skill_id, tenant_id=tenant):
            rules[row.id] = row
        for section in graph.sections_for_skill(ref.skill_id, tenant_id=tenant):
            for row in graph.rules_for_section(section.id, tenant_id=tenant):
                rules[row.id] = row
            blocks.extend((row.id, row.status.value, row.source_ref)
                          for row in graph.blocks_for_section(section.id, tenant_id=tenant))
    block = graph.get_publish_block(tenant)
    return {'rules': sorted((row.id, row.status.value, row.plane.value) for row in rules.values()),
            'blocks': sorted(blocks), 'publication': sorted(block.reasons) if block else (),
            'conflicts': sorted(row.id for row in graph.rules_conflicting_with_constraints(tenant)),
            'injection_history': sorted((item.id, item.subject_id, item.resolution, item.transaction_id)
                for item in reviews.history(tenant) if item.kind == 'injection')}
