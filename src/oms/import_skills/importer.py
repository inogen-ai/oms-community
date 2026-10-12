from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from oms.domain.identity import SkillRef
from oms.sources.models import Generations
from oms.sources.errors import SourceConflict, StaleMutation
from oms.sources.mutation import source_repository, watch_changes
from oms.ports.injection_screen import CLEAN, ScreenResult
from oms.domain.models import (
    Constraint, Edge, Skill, Transaction, Section, ContentBlock, Artefact, Example,
    TenantVocabulary,
)
from oms.domain.types import (
    SignalType, SourceRuntime, ArtefactKind, ExampleKind,
    EdgeType, SkillStatus, SkillOrigin, SkillVersionCause, mutability_for,
)
from oms.import_skills.classifier import DefaultSectionClassifier
from oms.domain.ids import block_id as _block_id
from oms.domain.ids import constraint_id as _constraint_id
from oms.domain.ids import example_id as _example_id
from oms.domain.ids import normalise as _normalise
from oms.domain.ids import slug as _slug
from oms.import_skills.merge import MergeStep
from oms.import_skills.identity import (
    ImportIdentityConflict, canonical_source, import_transaction_id, resolve_identities,
)
from oms.import_skills.parser import (
    ParsedConstraint, ParsedRule, ParsedSection, ParsedSkill, parse_directory,
    parse_skill_file,
)
from oms.import_skills.vocabulary import load_default_vocabulary
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import Sanitiser
from oms.ingestion.schema import ExecutionContext
from oms.ingestion.service import summarise
from oms.settings.thresholds import tenant_threshold
from oms.skills.history import SkillHistory
from oms.publish.manifest import is_package_echo
from oms.ports.blob_store import BlobStore
from oms.ports.import_extension import ImportExtension
from oms.ports.graph_store import GraphStore
from oms.ports.injection_screen import InjectionScreen
from oms.ports.review_queue import ReviewQueue
from oms.ports.section_classifier import SectionClassifier
from oms.ports.settings_store import SettingsStore
from oms.ports.vocabulary_store import VocabularyStore
from oms.ports.workflow import WorkflowRepository

logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    skills: int = 0
    rules_created: int = 0
    rules_corroborated: int = 0
    rules_flagged_removed: int = 0
    constraints: int = 0
    sections: int = 0
    artefacts: int = 0
    cross_skill_edges: int = 0
    identities: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportState:
    tenant_id: str
    skills: tuple[SkillRef, ...]
    generations: tuple[Generations, ...]


@dataclass(frozen=True)
class PreparedImport:
    state: ImportState
    vocabulary: TenantVocabulary
    original_skills: tuple[ParsedSkill, ...]
    skills: tuple[ParsedSkill, ...]
    constraints: tuple[ParsedConstraint, ...]
    transactions: dict[str, Transaction]
    screens: dict[str, ScreenResult]
    blob_refs: dict[str, str]
    injection_threshold: float
    filename: str | None = None
    extension: ImportExtension | None = None


class _PreparedScreen:
    def __init__(self, results):
        self.results = results

    def screen(self, body):
        return self.results[body]


def _revision(skill: ParsedSkill) -> str:
    return hashlib.sha256(json.dumps({
        "frontmatter": skill.frontmatter_body, "name": skill.name,
        "rules": [(r.body, r.polarity.value, r.reference_only) for r in skill.rules],
        "sections": [(section.heading, section.kind.value, section.body) for section in skill.sections],
        "artefacts": [(a.path, hashlib.sha256(a.body).hexdigest(), a.mode) for a in skill.artefacts],
        "document_mode": getattr(skill, "source_mode", None),
    }, sort_keys=True).encode()).hexdigest()[:16]


def _section_id(skill_id: str, heading: str) -> str:
    return f"section-{skill_id}-{_slug(heading)[:64] or 'intro'}"


def _artefact_id(content_hash: str) -> str:
    return f"artefact-{content_hash[:16]}"


class SkillImporter:
    def __init__(
        self,
        store: GraphStore,
        sanitiser: Sanitiser,
        payloads: FilePayloadStore,
        queue: ReviewQueue,
        blob_store: BlobStore | None = None,
        vocabulary_store: VocabularyStore | None = None,
        classifier: SectionClassifier | None = None,
        injection_screen: InjectionScreen | None = None,
        settings_store: SettingsStore | None = None,
        *,
        history: SkillHistory | None = None,
        extension: ImportExtension | None = None,
        repository: WorkflowRepository | None = None,
        prepare_extension: Callable[[tuple[ParsedSkill, ...], GraphStore, str], ImportExtension] | None = None,
    ) -> None:
        self._store = store
        self._sanitiser = sanitiser
        self._payloads = payloads
        self._queue = queue
        # Snapshots each skill this importer touches, cause `upload_import`.
        # Keyword-only and defaulted so every existing constructor call keeps
        # working unchanged - this is None for the two web upload routes
        # (`skills_api.py`), which already capture their own version straight
        # after `apply()` with the upload record's own filename; wiring the
        # same history in here too would just capture the identical content a
        # second time on the same request. It is for a direct caller - the
        # `oms import` CLI and a test load a tree with no such route in front
        # of it.
        self._history = history
        # The import write boundary's screen and threshold source, passed
        # through rather than left to MergeStep's constructor defaults - the
        # configuration gap design §1.3 names as its third defect.
        self._merge = MergeStep(store=store, queue=queue, extension=extension,
                                injection_screen=injection_screen,
                                settings_store=settings_store)
        self._blob_store = blob_store
        self._vocab_store = vocabulary_store
        self._settings_store = settings_store
        self._classifier = classifier or DefaultSectionClassifier()
        self._extension = extension
        self._repository = repository
        self._prepare_extension = prepare_extension
        self._bound = False
        self._prepared: PreparedImport | None = None

    def using(self, store, queue):
        """Bind an import to the caller's atomic store/queue transaction."""
        bound = copy(self)
        bound._store, bound._queue, bound._repository = store, queue, None
        bound._bound = True
        bound._merge = copy(self._merge)
        bound._merge._store, bound._merge._queue = store, queue
        if self._history is not None:
            bound._history = copy(self._history)
            bound._history._store = store
            bound._history._publisher = copy(self._history._publisher)
            bound._history._publisher._store = store
            events = self._history._admin_events
            if events is not None and hasattr(events, "_driver"):
                bound._history._admin_events = copy(events)
                bound._history._admin_events._driver = store._driver
        return bound

    def prepare_directory(self, root: Path, tenant_id: str):
        parsed = parse_directory(Path(root), vocabulary=self._vocabulary(tenant_id),
                                 classifier=self._classifier)
        parsed.skills = resolve_identities(parsed.skills, self._store, self._queue, tenant_id)
        return parsed

    def _vocabulary(self, tenant_id: str) -> TenantVocabulary:
        if self._vocab_store is not None:
            vocab = self._vocab_store.get_or_seed_default(tenant_id)
        else:
            # No vocabulary store wired: synthesise a defaults-backed
            # vocabulary so the parser still has something sensible to use.
            vocab = TenantVocabulary(tenant_id=tenant_id, version=0,
                                     body=load_default_vocabulary())
        # The section classifier reads its uncertainty threshold from the
        # vocabulary body it is handed, but the VALUE lives in tenant settings
        # now (design §5.3: the threshold keys moved out of the vocabulary).
        # Overlaying the resolved number onto a copy keeps the classifier
        # protocol unchanged while giving the settings screen the last word.
        body = dict(vocab.body)
        body["classifier_uncertainty_threshold"] = tenant_threshold(
            self._settings_store, tenant_id, "classifier_uncertainty_threshold", 0.55)
        return TenantVocabulary(tenant_id=vocab.tenant_id, version=vocab.version,
                                body=body)

    def capture_state(self, tenant_id: str) -> ImportState:
        """Capture content and binding fences before parsing or external checks."""
        def read(store, queue):
            sources = source_repository(store)
            generations = {row.skill: row for row in sources.generations_for_tenant(tenant_id=tenant_id)}
            skills = tuple(sorted(SkillRef(tenant_id, skill.id) for skill in store.skills_for_tenant(tenant_id)))
            for ref in skills:
                generations.setdefault(ref, Generations(skill=ref, content=0, binding=0))
            return ImportState(tenant_id, skills, tuple(generations[key] for key in sorted(generations)))
        if self._repository is not None:
            # Under the workspace lock, so an in-flight write commits before the fences are read.
            return self._repository.metadata("prepare-import", tenant_id, read)
        return read(self._store, self._queue)

    def prepare_import_directory(self, root: Path, tenant_id: str, *, limit: int | None = None,
                                 filename: str | None = None,
                                 expected_identities: dict[str, str] | None = None,
                                 state: ImportState | None = None) -> PreparedImport:
        if self._bound:
            raise ValueError("Prepare an import before entering the write transaction")
        if state is not None and state.tenant_id != tenant_id:
            raise ValueError("Import preparation crosses tenant ownership")
        if self._extension is not None and self._repository is not None and self._prepare_extension is None:
            raise ValueError("Guarded import enrichment requires a prepared extension")
        state = state or self.capture_state(tenant_id)
        vocabulary = self._vocabulary(tenant_id)
        parsed = parse_directory(Path(root), vocabulary=vocabulary, classifier=self._classifier)
        skills = parsed.skills if limit is None else parsed.skills[:limit]
        return self._prepare(skills, parsed.constraints, vocabulary, state,
                             filename, expected_identities)

    def prepare_import_file(self, path: Path, tenant_id: str, *, filename: str | None = None,
                            state: ImportState | None = None) -> PreparedImport:
        if self._bound:
            raise ValueError("Prepare an import before entering the write transaction")
        if state is not None and state.tenant_id != tenant_id:
            raise ValueError("Import preparation crosses tenant ownership")
        if self._extension is not None and self._repository is not None and self._prepare_extension is None:
            raise ValueError("Guarded import enrichment requires a prepared extension")
        state = state or self.capture_state(tenant_id)
        vocabulary = self._vocabulary(tenant_id)
        path = Path(path)
        skill = parse_skill_file(path.read_text(encoding="utf-8"), source_ref=path.as_posix(),
                                vocabulary=vocabulary, classifier=self._classifier)
        return self._prepare([skill] if skill is not None else [], [], vocabulary, state, filename, None)

    def _prepare(self, skills, constraints, vocabulary, state, filename, expected_identities):
        tenant_id = vocabulary.tenant_id
        if state.tenant_id != tenant_id:
            raise ValueError("Import preparation crosses tenant ownership")
        original = tuple(deepcopy(skills))
        resolved = tuple(resolve_identities(skills, self._store, self._queue, tenant_id))
        targets = {skill.source_path or skill.source_ref: skill.id for skill in resolved}
        if expected_identities is not None and targets != expected_identities:
            raise ImportIdentityConflict("Import targets changed after preview; upload the package again to review its current targets.")
        transactions, screens, blobs = {}, {}, {}
        sources = source_repository(self._store)
        for skill in resolved:
            ref = SkillRef(tenant_id, skill.id)
            if sources.get_binding(ref) is not None or sources.get_local_stream(ref) is not None:
                raise SourceConflict("reconciliation_required", "source reconciliation required")
        for skill in resolved:
            txn_id = import_transaction_id(tenant_id, skill.source_ref, _revision(skill))
            existing = self._store.get_transaction(txn_id)
            if existing is None:
                context = self._sanitiser.sanitise(ExecutionContext(
                    user_input=f"skill import: {skill.source_ref}", agent_raw_output="",
                    user_correction="\n".join(rule.body for rule in skill.rules))).context
                payload = self._payloads.put(txn_id, context)
                transactions[skill.id] = Transaction(id=txn_id, signal_type=SignalType.SKILL_IMPORT,
                    source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref=payload,
                    timestamp=datetime.now(timezone.utc), tenant_id=tenant_id,
                    source_ref=skill.source_ref, summary=summarise(context))
            else:
                if existing.tenant_id != tenant_id:
                    raise ImportIdentityConflict("Import transaction belongs to another workspace")
                if existing.signal_type is not SignalType.SKILL_IMPORT or existing.source_ref != skill.source_ref:
                    raise ImportIdentityConflict("Import transaction has incompatible provenance")
                transactions[skill.id] = existing
            for rule in skill.rules:
                if rule.body not in screens:
                    try:
                        screens[rule.body] = self._merge._screen.screen(rule.body)
                    except Exception:
                        logger.exception("Import screening failed during preparation")
                        screens[rule.body] = CLEAN
            for artefact in skill.artefacts:
                digest = hashlib.sha256(artefact.body).hexdigest()
                blobs[digest] = (self._blob_store.put(artefact.body) if self._blob_store is not None
                                 else f"sha256-{digest}")
        extension = self._extension
        if extension is not None and self._prepare_extension is not None:
            extension = self._prepare_extension(resolved, self._store, tenant_id)
        elif extension is not None and self._repository is not None:
            raise ValueError("Guarded import enrichment requires a prepared extension")
        threshold = tenant_threshold(self._settings_store, tenant_id, "injection_threshold_import",
                                     self._merge._injection_threshold)
        return PreparedImport(state, vocabulary, original, resolved, tuple(constraints),
                              transactions, screens, blobs, threshold, filename, extension)

    def apply_prepared(self, prepared: PreparedImport,
                       on_skill: Callable[[int, int, str], None] | None = None) -> ImportReport:
        """Write a prepared import in one transaction.

        `on_skill(index, total, name)` runs inside that transaction, immediately
        before each skill, so it must be fast: it holds the write lock. A retried
        transaction does not report an index again, so progress only advances.
        """
        tenant_id = prepared.state.tenant_id
        reported = 0
        generations = {row.skill: row for row in prepared.state.generations}
        for skill in prepared.skills:
            ref = SkillRef(tenant_id, skill.id)
            generations.setdefault(ref, Generations(skill=ref, content=0, binding=0))
        expected = tuple(generations[key] for key in sorted(generations))
        def apply(store, queue):
            nonlocal reported
            bound = self.using(store, queue)
            sources = source_repository(store)
            sources.compare_generations(expected)
            current = tuple(sorted(SkillRef(tenant_id, skill.id) for skill in store.skills_for_tenant(tenant_id)))
            if current != prepared.state.skills:
                raise StaleMutation("content_changed", "import inventory changed")
            resolved = resolve_identities(deepcopy(list(prepared.original_skills)), store, queue, tenant_id)
            if {skill.source_ref: skill.id for skill in resolved} != {skill.source_ref: skill.id for skill in prepared.skills}:
                raise StaleMutation("content_changed", "import identity changed")
            for skill in prepared.skills:
                if sources.open_update(SkillRef(tenant_id, skill.id)) is not None:
                    raise SourceConflict("reconciliation_required", "skill update requires reconciliation")
            content_changes = watch_changes(store, tenant_id) if bound._history is not None else None
            bound._prepared = prepared
            bound._extension = prepared.extension
            if prepared.extension is not None and hasattr(prepared.extension, "using"):
                bound._extension = prepared.extension.using(store, queue)
            bound._merge._extension = bound._extension
            bound._merge._screen = _PreparedScreen(prepared.screens)
            bound._merge._settings_store = None
            bound._merge._injection_threshold = prepared.injection_threshold
            report = ImportReport(identities={skill.source_ref: skill.id for skill in prepared.skills})
            for constraint in prepared.constraints:
                bound._upsert_constraint(constraint, tenant_id)
                report.constraints += 1
            for index, skill in enumerate(prepared.skills, start=1):
                if on_skill is not None and index > reported:
                    reported = index
                    on_skill(index, len(prepared.skills), skill.name)
                bound._import_skill(skill, tenant_id, prepared.vocabulary, report, filename=prepared.filename)
            for skill in prepared.skills:
                bound._import_routing(skill, tenant_id)
            if bound._history is not None:
                from oms.skills.upload import clean_filename
                sources_by_skill = {skill.id: skill.source_ref for skill in prepared.skills}
                default_source = ", ".join(sorted(sources_by_skill.values()))
                for ref in content_changes():
                    if store.get_skill(ref.skill_id, tenant_id=tenant_id) is None:
                        continue
                    source = sources_by_skill.get(ref.skill_id, default_source)
                    detail = (f"uploaded {clean_filename(prepared.filename)}" if prepared.filename
                              else f"imported {source}")
                    bound._history.capture_required(ref.skill_id, tenant_id,
                        cause=SkillVersionCause.UPLOAD_IMPORT, detail=detail)
            return report
        if self._repository is not None:
            participants = (self._history,) if self._history is not None else ()
            if prepared.extension is not None and hasattr(prepared.extension, "rollback_participants"):
                participants += prepared.extension.rollback_participants()
            for participant in participants:
                if hasattr(participant, "bind_workflow_lock") and hasattr(self._repository, "_mutex"):
                    participant.bind_workflow_lock(self._repository._mutex)
            return self._repository.atomic("skill-import", tenant_id, apply,
                expected_generations=expected, rollback_participants=participants)
        return apply(self._store, self._queue)

    def import_directory(self, root: Path, tenant_id: str, limit: int | None = None,
                         on_skill: Callable[[int, int, str], None] | None = None,
                         filename: str | None = None,
                         expected_identities: dict[str, str] | None = None,
                         *, state: ImportState | None = None) -> ImportReport:
        prepared = self.prepare_import_directory(root, tenant_id, limit=limit, filename=filename,
                                                 expected_identities=expected_identities, state=state)
        return self.apply_prepared(prepared, on_skill)

    def import_file(self, path: Path, tenant_id: str, filename: str | None = None,
                    *, state: ImportState | None = None) -> ImportReport:
        return self.apply_prepared(self.prepare_import_file(path, tenant_id, filename=filename, state=state))

    def _upsert_constraint(self, pc: ParsedConstraint, tenant_id: str) -> None:
        cid = _constraint_id(tenant_id, pc.body)
        self._store.upsert_constraint(Constraint(
            id=cid, body=pc.body, tenant_id=tenant_id, polarity=pc.polarity,
        ))

    def _import_skill(
        self, skill: ParsedSkill, tenant_id: str, vocab: TenantVocabulary,
        report: ImportReport, filename: str | None = None,
    ) -> None:
        if self._prepared is None:
            raise ValueError("Import content must be prepared before writing")
        # Publication-ledger no-op: if the source file's hash matches the last
        # publication we wrote, skip entirely.
        if self._is_unchanged_against_ledger(skill, tenant_id):
            return

        # Source files may update metadata only where the user has not curated
        # it. Re-import must preserve both those edits and the review status.
        existing = self._store.get_skill(skill.id, tenant_id=tenant_id)
        curated = existing.curated if existing is not None else frozenset()

        def _keep(field: str, from_file: str) -> str:
            return getattr(existing, field) if field in curated else from_file

        self._store.upsert_skill(Skill(
            id=skill.id, name=_keep("name", skill.name),
            description=_keep("description", skill.description),
            domain=_keep("domain", skill.domain), tenant_id=tenant_id,
            # Re-importing a skill does not resolve its review items.
            status=existing.status if existing is not None else SkillStatus.ACTIVE,
            curated=curated,
            # Preserved on re-import for the same reason `status` is: a package
            # re-imported over a skill OMS minted does not turn it into an
            # imported one. What the file is authority for is the content, not
            # the history.
            origin=existing.origin if existing is not None else SkillOrigin.IMPORTED,
            publish_enabled=existing.publish_enabled if existing is not None else True,
            repo=existing.repo if existing is not None else None,
            import_source_ref=existing.import_source_ref if existing and existing.import_source_ref else skill.source_ref,
            import_name=existing.import_name if existing and existing.import_name else skill.import_name or skill.id,
            declared_license=getattr(skill, "declared_license", None),
            document_mode=getattr(skill, "source_mode", None),
        ))
        self._import_tags(skill, tenant_id, curated)
        transaction = self._prepared.transactions[skill.id]
        txn_id = transaction.id
        if self._store.get_transaction(txn_id) is None:
            self._store.upsert_transaction(transaction)

        # Run the existing merge step (rule-level dedup, polarity, examples).
        # Only rules actually matched or created by this import earn a source
        # observation. The merge records each distinct source once without
        # resetting manual or inherited corroboration already on the rule.
        merged = self._merge.merge_skill(skill, transaction_id=txn_id, tenant_id=tenant_id,
                                        observe_sources=True)
        report.skills += 1
        report.rules_created += merged.created
        report.rules_corroborated += merged.corroborated
        report.rules_flagged_removed += merged.flagged_removed

        # Section-aware ingestion: create Section nodes, attach rules/blocks/examples.
        self._import_sections(skill, tenant_id, txn_id, report)

        # Artefacts: store bytes in BlobStore, upsert Artefact nodes.
        self._import_artefacts(skill, tenant_id, report)

        # Cross-skill detection (informational, embedding-gated, never blocks publish).
        self._detect_cross_skill_relationships(skill, tenant_id, vocab, report)

    def _import_tags(self, skill: ParsedSkill, tenant_id: str,
                     curated: frozenset[str] = frozenset()) -> None:
        """Put the file's `tags:` on the SKILL, as the file meant them.

        A diff rather than an append: the source file is the authority on what
        a skill is filed under, so a tag dropped from frontmatter has to leave
        the graph. Appending would make the tag list grow-only, and a skill
        would keep answering `skills_by_tags` for a topic its own file no
        longer claims.

        Unless a human set the tags, in which case the file is not authority
        and the diff would not merely overwrite their list but delete it.
        """
        if "tags" in curated:
            return
        wanted = list(dict.fromkeys(skill.tags))
        current = set(self._store.tags_for_skill(skill.id, tenant_id=tenant_id))
        for tag in wanted:
            if tag in current:
                continue
            self._store.upsert_tag(tag, tag)
            self._store.attach_edge(Edge(type=EdgeType.TAGGED_WITH,
                                         from_id=skill.id, to_id=tag), tenant_id=tenant_id)
        for stale in sorted(current - set(wanted)):
            # The Tag node itself stays: other skills and rules may still point
            # at it, and nothing here can see whether they do.
            self._store.detach_edge(Edge(type=EdgeType.TAGGED_WITH,
                                         from_id=skill.id, to_id=stale), tenant_id=tenant_id)

    def _is_unchanged_against_ledger(self, skill: ParsedSkill, tenant_id: str) -> bool:
        """A no-op requires proof for the complete package, modes and policy."""
        publication = self._store.get_publication(tenant_id, skill.source_ref)
        if publication is None:
            return False
        return is_package_echo(publication, skill)

    def _reconstitute_skill_text(self, skill: ParsedSkill) -> str:
        # Used only by the ledger check; matches what publish would write so
        # the hash is stable across publish->import round trips.
        parts: list[str] = []
        if skill.frontmatter_body:
            parts.append("---\n" + skill.frontmatter_body + "\n---\n")
        parts.append(f"# {skill.name}\n")
        for sec in skill.sections:
            parts.append(f"\n## {sec.heading}\n\n{sec.body}\n")
        return "".join(parts)

    def _import_sections(
        self, skill: ParsedSkill, tenant_id: str, txn_id: str, report: ImportReport,
    ) -> None:
        """Create Section nodes for each parsed section, attaching rules to
        system_aggregated sections and content blocks to authorial sections."""
        # Build a lookup of stored rules by (normalised body, polarity) so we
        # can wire each parsed rule to its corresponding stored Rule.
        stored_by_key = {
            (_normalise(r.body), r.polarity): r
            for r in self._store.rules_for_skill(skill.id, tenant_id=tenant_id)
        }

        for psec in skill.sections:
            sec = Section(
                id=_section_id(skill.id, psec.heading),
                skill_id=skill.id, kind=psec.kind, heading=psec.heading,
                order=psec.order, mutability=psec.mutability, tenant_id=tenant_id,
            )
            self._store.upsert_section(sec)
            report.sections += 1

            if psec.mutability.value == "system_aggregated":
                # Attach rules to the section, recording each rule's authorial
                # placement (sequence + ### subsection) so publish can
                # reconstruct the author's structure.
                for idx, prule in enumerate(psec.rules):
                    key = (_normalise(prule.body), prule.polarity)
                    stored = stored_by_key.get(key)
                    if stored is not None:
                        self._store.attach_rule(stored, section_id=sec.id,
                                                order=idx, group=prule.group, tenant_id=tenant_id)
                # Section-level examples, keeping their authorial sequence.
                for idx, pex in enumerate(psec.examples):
                    example = Example(
                        id=_example_id(sec.id, pex.body),
                        body=pex.body, kind=pex.kind, tenant_id=tenant_id,
                        name=pex.name, original_label=pex.original_label,
                        parent_section_id=sec.id, source_ref=skill.source_ref,
                        order=idx,
                    )
                    self._store.upsert_example(example, tenant_id=tenant_id)
            else:
                # Authorial section: content-hash supersession path. The text
                # lives on the node whatever its length; `content_ref` is the
                # hash that block ids and supersession are keyed on, not a
                # pointer into storage. The blob store keeps artefacts, whose
                # bytes genuinely cannot sit on a node.
                content_hash = hashlib.sha256(psec.body.encode("utf-8")).hexdigest()
                block = ContentBlock(
                    id=_block_id(sec.id, content_hash),
                    content_ref=f"sha256-{content_hash}", kind=psec.kind,
                    tenant_id=tenant_id,
                    source_ref=f"{skill.source_ref}#{_slug(psec.heading)}",
                    body=psec.body,
                )
                # Block id is content-addressed: an unchanged body yields the same
                # id. If the section already has a different active block, this is
                # a source-file edit and supersedes it (provenance: the import
                # transaction). Same id is an idempotent no-op; no active block
                # means a first import, so attach as today.
                active = self._store.blocks_for_section(sec.id, tenant_id=tenant_id)
                current = active[0] if active else None
                if current is None:
                    self._store.upsert_content_block(block)
                    self._store.attach_block(block, section_id=sec.id, tenant_id=tenant_id)
                elif current.id != block.id:
                    if canonical_source(current.source_ref.partition("#")[0],
                                        skill.import_name or skill.id) != skill.source_ref:
                        # Two-writer policy (spec 6.5): the active block carries
                        # revision/review provenance (system-applied) AND the
                        # source file changed. A three-way merge is a human
                        # decision, so escalate instead of guessing.
                        from oms.domain.models import ReviewItem
                        from oms.domain.types import Verdict
                        review_id = f"review-threeway-{hashlib.sha256(tenant_id.encode()).hexdigest()[:16]}-{sec.id}-{block.id[-16:]}"
                        # A review addresses this incoming source revision.
                        # Upload retries must preserve its original baseline,
                        # timestamps and any explicit decision, including a
                        # rejection or acceptance with merged wording. A new
                        # incoming body gets a distinct review. If a pending
                        # baseline changes, approval must refuse the stale
                        # review rather than silently rewriting its subject.
                        self._queue.enqueue_once(ReviewItem(
                            id=review_id,
                            kind="block_revision", subject_id=current.id,
                            other_id=sec.id, verdict=Verdict.AMBIGUOUS,
                            reason=("source file edited while a system-applied "
                                    "revision is active (three-way merge)"),
                            tenant_id=tenant_id, proposed_body=psec.body,
                            transaction_id=txn_id,
                        ))
                    else:
                        self._store.supersede_block(
                            current.id, block, section_id=sec.id, transaction_id=txn_id,
                            tenant_id=tenant_id,
                        )

                # Routing / reference edges, when the section type carries referents.
                for ref in psec.reference_targets:
                    self._safe_attach_edge(
                        EdgeType.HAS_REFERENCE, skill.id, ref.path, {"prose": ref.prose}, tenant_id
                    )

    def _import_routing(self, skill, tenant_id):
        for section in skill.sections:
            for target in section.routing_targets:
                self._safe_attach_edge(EdgeType.ROUTES_TO, skill.id, target.skill_id,
                                       {"prose": target.prose}, tenant_id)

    def _safe_attach_edge(self, edge_type, from_id, to_id, properties, tenant_id):
        from oms.domain.models import Edge
        try:
            self._store.attach_edge(Edge(type=edge_type, from_id=from_id, to_id=to_id, properties=properties), tenant_id=tenant_id)
        except KeyError:
            # Edge target may not exist yet (forward reference); ignore.
            pass

    def _import_artefacts(
        self, skill: ParsedSkill, tenant_id: str, report: ImportReport,
    ) -> None:
        for pa in skill.artefacts:
            if self._prepared is None:
                raise ValueError("Import files must be staged before writing")
            content_ref = self._prepared.blob_refs[hashlib.sha256(pa.body).hexdigest()]
            artefact = Artefact(
                id=_artefact_id(content_ref.split("-", 1)[1]),
                content_ref=content_ref, kind=pa.kind, name=pa.name,
                size=len(pa.body), tenant_id=tenant_id, source_ref=pa.source_ref,
                mode=pa.mode,
            )
            self._store.upsert_artefact(artefact, skill_id=skill.id, path=pa.path, tenant_id=tenant_id)
            report.artefacts += 1

    def _detect_cross_skill_relationships(self, skill, tenant_id, vocab, report):
        if self._extension is not None:
            report.cross_skill_edges += self._extension.skill_imported(skill.id, tenant_id)
