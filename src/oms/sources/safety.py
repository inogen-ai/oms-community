"""The deterministic safety checks a source update has to pass.

Nothing here calls a model. Text goes through the regex sanitiser and the deterministic
injection screen, files are checked as the exact bytes in custody, and a check that
cannot run says so rather than passing, which the plan turns into a hold (spec §7.7).
The live holds stop an update from touching rules that are already held, flagged or
tainted, whatever the upstream text looks like.
"""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import logging
from typing import Literal, TYPE_CHECKING

from oms.domain.types import Plane, RuleStatus, SignalType
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext
from oms.ports.blob_store import BlobStore
from oms.security.constraint_patterns import shared_danger, targets_the_system
from oms.security.injection import DeterministicScreen
from oms.sources.errors import SourceConflict
from oms.sources.excerpts import file_shows, part_view_fits
from oms.sources.files import verified_bytes
from oms.sources.findings import ACCEPTABLE_CODES, acceptable_hold, checked_findings
from oms.sources.models import (
    CheckFinding, CheckResult, LocalState, ManifestEntry, MergePlan, PartKind, PlanFlag, ProjectionPart, SourceSnapshot,
)

if TYPE_CHECKING:
    from oms.ports.graph_store import GraphStore
    from oms.ports.review_queue import ReviewQueue

logger = logging.getLogger(__name__)

MAX_FINDINGS = 100
# The reason a hold carries when the card does not show the words of one of its located
# AI-instruction findings: no reviewer may take it as it is.
NOT_SHOWN = 'flagged_text_not_shown'


def not_shown(result: CheckResult) -> CheckResult:
    """The result, marked when it holds a located AI-instruction finding the card does not show."""
    if (result.state != 'held' or NOT_SHOWN in result.reasons
            or not any(row.category.startswith('injection:') and row.line is not None for row in result.findings)):
        return result
    return result.model_copy(update={'reasons': (*result.reasons, NOT_SHOWN)})


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
    # Skill packages hold timestamps, so a bare Unix time is kept here; a correction keeps redacting it.
    sanitiser: object = field(default_factory=lambda: RegexSanitiser(keep_unix_times=True))
    # One pass, because the screen weighs the whole text together and a cut could split a
    # phrase. At this limit, hostile text with tens of thousands of addresses and numbers
    # measured 2 to 6 s; the time grows linearly with the text and its findings.
    max_text_chars: int = 1_000_000
    # The largest file a package may keep; a larger one is held, and the hold cannot be accepted.
    # The upload gate's default; a host may set it per tenant (the paid console does).
    max_file_bytes: int = 25_000_000

    def __post_init__(self):
        if type(self.max_text_chars) is not int or self.max_text_chars <= 0:
            raise ValueError('text check budget must be a positive integer')
        if type(self.max_file_bytes) is not int or self.max_file_bytes <= 0:
            raise ValueError('file size limit must be a positive integer')

    def check_text(self, text: str, *, path: str | None = None, part_id: str | None = None) -> CheckResult:
        """Sanitiser and screen over one text. With a `path`, the text is a file the card shows in
        its bounded preview and the excerpts around its flagged lines."""
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
        if not reasons:
            return CheckResult(state='passed', code='source_text_checked')
        findings, truncated, spans = checked_findings(text, clean.redactions, screened, path=path, part_id=part_id)
        if truncated:
            # Lines the card cannot show are unknown to the reviewer, so the hold is never acceptable.
            reasons.append('findings_truncated')
        # An AI-instruction flag is taken as it is only when the card shows its words. The card
        # shows SKILL.md through its parts, which carry that rule, never as a file preview.
        if (path is not None and path != "SKILL.md"
                and not file_shows(text, (row.line for row in findings if row.line is not None), spans)):
            reasons.append(NOT_SHOWN)
        return CheckResult(state='held', code='source_text_hold', reasons=tuple(reasons), findings=findings)

    def check_part(self, value: object, *, part_id: str,
                   strings: Iterable[str] | None = None) -> tuple[CheckResult, ...]:
        """Each text of a section, rule, example or field part (by default, every string in its
        value), checked. The card shows the part as one bounded view; when the part is longer, an
        AI-instruction flag's words may be past its end, so none is taken as it is."""
        results = tuple(self.check_text(text, part_id=part_id) for text in (texts(value) if strings is None else strings))
        if all(result.state != 'held' for result in results) or part_view_fits(value):
            return results
        return tuple(not_shown(result) for result in results)

    def check_file(self, body: bytes, *, path: str | None = None,
                   maximum_bytes: int | None = None) -> CheckResult:
        if len(body) > (self.max_file_bytes if maximum_bytes is None else maximum_bytes):
            return CheckResult(state='held', code='file_size_limit',
                               findings=(CheckFinding(path=path, category='file_size'),))
        # A binary file gets size and structure checks only; the code says so,
        # and the preview shows it, instead of implying its content was read.
        try:
            text = body.decode('utf-8')
        except UnicodeError:
            return CheckResult(state='passed', code='binary_structure_only')
        if '\x00' in text:
            return CheckResult(state='passed', code='binary_structure_only')
        return self.check_text(text, path=path)


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
    # Every file and part is checked so the review can name each one that held.
    first: CheckResult | None = None
    unavailable = False
    # Per-file state is derived from the findings, so the invariant is: a file
    # that did not pass always contributes a finding naming its path, and those
    # are kept ahead of the display cap. If even they overflow the cap, the
    # result says "file_findings_truncated" and every file takes the whole result.
    guaranteed: list[CheckFinding] = []
    extra: list[CheckFinding] = []
    # Reasons no reviewer may take as they are (a cut list, flagged text the card does not show):
    # any check's mark the whole result, and a file's own travel with that file alone.
    blocking: list[str] = []
    file_reasons: dict[str, list[str]] = {}

    def note(result: CheckResult, *, path: str | None = None, part_id: str | None = None) -> CheckResult:
        nonlocal first, unavailable
        if result.state == "passed":
            return result
        unavailable = unavailable or result.state == "unavailable"
        # Only the first result's reasons are kept whole, so a later check's own are added here.
        own = [reason for reason in result.reasons if reason not in ACCEPTABLE_CODES]
        blocking.extend(own)
        if path is not None:
            file_reasons.setdefault(path, []).extend(own)
        found = result.findings
        if not found:
            category = (f"check_unavailable:{result.code}" if result.state == "unavailable"
                        else f"policy:{result.code}")
            found = (CheckFinding(path=path, part_id=part_id, category=category),)
        if first is None:
            first = result
        if path is not None:
            guaranteed.append(found[0])
            extra.extend(found[1:])
        else:
            extra.extend(found)
        return result

    for entry in snapshot.manifest:
        try:
            body = verified_bytes(blobs, entry)
        except (SourceConflict, OSError):
            note(CheckResult(state="unavailable", code="package_custody_unavailable"), path=entry.path)
            continue
        note(checks.check_file(body, path=entry.path), path=entry.path)
    for part in snapshot.effective_projection:
        if part.evidence.kind == "known":
            for result in checks.check_part(part.evidence.value, part_id=part.part_id):
                note(result, part_id=part.part_id)
    if first is None:
        return CheckResult(state="passed", code="package_bytes_checked")
    # Any dropped finding, here or in a check that cut its own list, marks the whole result
    # truncated: what was cut is unknown, so it is never acceptable as it is. A file is marked
    # only when its own list was cut or the cap dropped one of its findings, so the files beside
    # it keep theirs. Only an overflow of the per-file findings stops files being told apart.
    listed = guaranteed + extra
    if len(listed) > MAX_FINDINGS:
        blocking.append("findings_truncated")
        for path in dict.fromkeys(finding.path for finding in listed[MAX_FINDINGS:] if finding.path is not None):
            file_reasons.setdefault(path, []).append("findings_truncated")
    reasons = tuple(dict.fromkeys(first.reasons + tuple(blocking)
                                  + (("file_findings_truncated",) if len(guaranteed) > MAX_FINDINGS else ())))
    return CheckResult(state="unavailable" if unavailable else "held", code=first.code,
                       reasons=reasons, findings=tuple(listed[:MAX_FINDINGS]),
                       file_reasons=tuple((path, tuple(dict.fromkeys(own))) for path, own in file_reasons.items()
                                          if own))


def texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from texts(item)


def acceptable_hold_result(result: CheckResult) -> bool:
    """A held result a reviewer may take as it is: only personal data and visible instructions
    to an AI model (spec D2, ruling R6, widened on 14 October 2026)."""
    return result.state == "held" and acceptable_hold((result.code, *result.reasons), result.findings)


# The earlier name, kept for callers that still import it.
personal_data_hold = acceptable_hold_result


def acknowledges(result: CheckResult, categories: frozenset[str] | None) -> bool:
    """Whether a reviewer accepted this held result: held only for findings they confirmed (spec D2)."""
    return (bool(categories) and acceptable_hold_result(result)
            and all(finding.category in categories for finding in result.findings))


def screen_selected(snapshot: SourceSnapshot, writes: tuple[ProjectionPart, ...],
                    blobs: BlobStore, safety: SourceSafety | None = None, *,
                    manifest: tuple[ManifestEntry, ...] | None = None,
                    acknowledged: Mapping[str, frozenset[str]] | None = None) -> CheckResult:
    checks = safety or SourceSafety()
    accepted = acknowledged or {}
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
                result = checks.check_file(verified_bytes(blobs, entry), path=entry.path)
            except (SourceConflict, OSError):
                return CheckResult(state="unavailable", code="package_custody_unavailable")
            if result.state != "passed" and not acknowledges(result, accepted.get(part.part_id)):
                return result
        else:
            for result in checks.check_part(part.evidence.value, part_id=part.part_id):
                if result.state != "passed" and not acknowledges(result, accepted.get(part.part_id)):
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


def screen_policy(graph: "GraphStore | None", reviews: "ReviewQueue | None", blobs: BlobStore,
                  snapshot: SourceSnapshot, local: LocalState, *,
                  writes: tuple[ProjectionPart, ...] | None = None,
                  manifest: tuple[ManifestEntry, ...] | None = None,
                  acknowledged: Mapping[str, frozenset[str]] | None = None) -> CheckResult:
    # A graph or queue without the methods the holds need cannot say the
    # update is safe; it says so, and the plan holds (spec §7.7).
    required = ('active_constraints', 'rules_for_skill', 'sections_for_skill', 'blocks_for_section', 'get_rule',
              'rules_conflicting_with_constraints', 'get_publish_block', 'blocks_revised_for_rule', 'lineage')
    if (graph is None or reviews is None or local.skill != snapshot.ref.origin.skill
            or any(not callable(getattr(graph, name, None)) for name in required)
            or any(not callable(getattr(reviews, name, None)) for name in ('pending', 'history'))):
        return CheckResult(state='unavailable', code='source_policy_state_unavailable')
    checked = (screen_snapshot(snapshot, blobs) if writes is None else
             screen_selected(snapshot, writes, blobs, manifest=manifest, acknowledged=acknowledged))
    # The plan may release a whole-package hold a reviewer could take as it is, on parts it keeps
    # (ruling F1), so that hold must not stand in for the stages after it. They still run, and
    # any hold they find is the result; only when they all pass does the acceptable hold stand.
    if (checked.state != 'passed' and not (writes is None and acceptable_hold_result(checked))) or writes == ():
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
