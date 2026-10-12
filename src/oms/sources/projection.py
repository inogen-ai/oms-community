"""Build a source snapshot from the retained bytes of a package.

SKILL.md is parsed in a separate, bounded interpreter, and every part is given an
identity derived from the origin and its own content, so the same package projects
to the same snapshot wherever it is built and a later revision meets its earlier
self part by part.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
import unicodedata

from pydantic import TypeAdapter

from oms.domain.ids import slug
from oms.import_skills.parser import ParsedSkill, is_os_debris
from oms.ports.blob_store import BlobStore
from oms.publish.render import REFERENCES_FILE
from oms.sources.credentials import isolated_environment
from oms.sources.files import file_evidence
from oms.sources.limits import AcquisitionLimits, Budget, run_bounded
from oms.sources.models import (
    AcquiredPackage, Evidence, GraphMapping, LocalPackage, ManifestEntry, OriginRef,
    PartKind, ProjectionPart, SnapshotRef, SourceSnapshot,
)
from oms.sources.primitives import exact_digest, example_value, rule_value, section_value


class ProjectionBuilder:
    def __init__(self, blobs: BlobStore, work_dir: Path, *, policy_version: str = "1",
                 projection_version: str = "1", limits: AcquisitionLimits | None = None):
        self.blobs = blobs
        self.work_dir = Path(work_dir)
        self.policy_version = policy_version
        self.projection_version = projection_version
        self.limits = limits or AcquisitionLimits()

    def _read(self, entry: ManifestEntry, budget: Budget) -> bytes:
        # Verified against the manifest before anything is parsed: a snapshot
        # describes the bytes in custody, and a blob that does not match its
        # entry is a custody fault, not a package to project.
        if entry.size > self.limits.file_bytes:
            raise ValueError("Projection file exceeds its byte budget")
        body = self.blobs.get(entry.blob_ref)
        if len(body) != entry.size or sha256(body).hexdigest() != entry.digest:
            raise ValueError("Package manifest digest or size does not match retained bytes")
        budget.expand(len(body))
        return body

    def build(self, acquired: AcquiredPackage | LocalPackage, origin: OriginRef, *, snapshot_id: str,
              mappings: tuple[GraphMapping, ...] = (), base: SourceSnapshot | None = None) -> SourceSnapshot:
        """`base` pins each section it installed to its installed kind, by heading identity.

        A refreshed section keeps the kind it was installed with (decision of 7
        October 2026). Classifying it afresh from its new wording could move the
        content between section kinds, and the plan would then read a reworded
        section as one part removed and another added.
        """
        budget = Budget(self.limits)
        if len(acquired.manifest) > self.limits.tree_entries:
            raise ValueError("Projection inventory exceeds its entry budget")
        nested = tuple(entry.path[:-len("/SKILL.md")] + "/" for entry in acquired.manifest
                       if entry.path.endswith("/SKILL.md"))
        selected = [entry for entry in acquired.manifest
                    if not any(entry.path.startswith(prefix) for prefix in nested)
                    and not any(is_os_debris(component) for component in entry.path.split("/"))]
        paths = [unicodedata.normalize("NFC", entry.path).casefold() for entry in selected]
        if len(paths) != len(set(paths)):
            raise ValueError("Colliding normalized package paths")
        if len(selected) > self.limits.files or sum(entry.size for entry in selected) > self.limits.package_bytes:
            raise ValueError("Projection package exceeds its content budget")
        manifest = []
        primary = overflow = None
        for entry in sorted(selected, key=lambda row: row.path):
            budget.check()
            body = self._read(entry, budget)
            if entry.path == "SKILL.md":
                primary = body
                primary_entry = entry
            elif entry.path == REFERENCES_FILE:
                overflow = body.decode("utf-8")
                entry = entry.model_copy(update={"published": False, "exclusion_reason": "generated_rule_overflow"})
            elif entry.path.casefold() == "claude.md":
                entry = entry.model_copy(update={"published": False, "exclusion_reason": "reserved_instruction_file"})
            manifest.append(entry)
        if primary is None:
            raise ValueError("Package requires SKILL.md")
        # The document is parsed in a child interpreter under the same budget
        # as a fetch, with no site packages and a closed environment: upstream
        # text is untrusted input, and a pathological document may cost time
        # and memory but cannot take the service down with it.
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = {"text": primary.decode("utf-8"), "overflow": overflow,
                   "source_ref": "/".join(filter(None, (acquired.package_path, "SKILL.md"))),
                   "tenant_id": origin.skill.tenant_id, "mode": primary_entry.mode,
                   "kinds": installed_kinds(base)}
        code, output = run_bounded([sys.executable, "-I", "-m", "oms.sources.projection_parser"],
            isolated_environment(self.work_dir), self.work_dir, budget,
            stdin=json.dumps(request, ensure_ascii=False).encode())
        if code:
            raise ValueError("Package document could not be projected")
        budget.expand(len(output))
        parsed = json.loads(output)
        skill = TypeAdapter(ParsedSkill).validate_python(parsed["skill"])
        raw = Evidence(kind="known", value=parsed["frontmatter"], source_digest=primary_entry.digest,
                       policy_version=self.policy_version) if parsed["frontmatter"] is not None else self._absent()
        return self._snapshot(skill, acquired, origin, snapshot_id, tuple(manifest), raw,
                              parsed["license"], mappings)

    def _absent(self) -> Evidence:
        return Evidence(kind="absent", policy_version=self.policy_version)

    def _known(self, value) -> Evidence:
        return Evidence(kind="known", value=value, source_digest=exact_digest(value),
                        policy_version=self.policy_version)

    def _snapshot(self, skill, acquired, origin, snapshot_id, manifest, raw, licence, mappings):
        supplied = {mapping.part_id: mapping for mapping in mappings}
        if len(supplied) != len(mappings):
            raise ValueError("Duplicate source graph mapping")
        if any(owner.tenant_id != origin.skill.tenant_id for mapping in mappings for owner in mapping.owner_skills):
            raise ValueError("Source graph mapping crosses tenant ownership")
        parsed, effective, mapped = [], [], []
        seen = set()

        # Graph identities come from the base's mappings when there is one, so
        # a part keeps its rule or section across revisions; a new part gets an
        # identity derived from the origin, so two tenants installing the same
        # package never share a graph node.
        def identity(part_id, kind):
            if part_id in supplied:
                existing = supplied[part_id]
                if not existing.entity_ids:
                    raise ValueError("Source mapping lacks a graph identity")
                return existing
            if kind == PartKind.FIELD:
                identifiers = (origin.skill.skill_id,)
            else:
                key = [origin.skill.storage_key, origin.origin_id, part_id]
                identifiers = (f"{kind.value}-source-{exact_digest(key)[:32]}",)
            return GraphMapping(part_id=part_id, entity_ids=identifiers, owner_skills=(origin.skill,))

        def add(part_id, kind, source_value, effective_value=None, *, order=0, section_id=None, evidence=None):
            if part_id in seen:
                raise ValueError("Ambiguous duplicate source unit identity")
            seen.add(part_id)
            if len(seen) > 10000:
                raise ValueError("Projection part budget exceeded")
            mapping = identity(part_id, kind)
            mapping = mapping.model_copy(update={"section_id": section_id, "custody_digest": None})
            source_evidence = evidence or self._known(source_value)
            part = ProjectionPart(part_id=part_id, kind=kind, evidence=source_evidence,
                                  order=order, owner_origins=(origin.origin_id,))
            parsed.append(part)
            effective.append(part.model_copy(update={"evidence": evidence or self._known(
                source_value if effective_value is None else effective_value)}))
            mapped.append(mapping)
            return mapping

        for name, value in (("name", skill.name), ("domain", skill.domain),
                            ("description", skill.description), ("tags", sorted(set(skill.tags)))):
            add("field:" + name, PartKind.FIELD, value)
        license_evidence = Evidence(kind=licence["kind"], value=licence["value"],
                                    source_digest=raw.source_digest, policy_version=self.policy_version)
        add("field:license", PartKind.FIELD, None, evidence=license_evidence)
        mode_evidence = (self._known(skill.source_mode) if skill.source_mode is not None else
                         Evidence(kind="unknown", policy_version=self.policy_version))
        add("field:document_mode", PartKind.FIELD, None, evidence=mode_evidence)
        headings = [slug(section.heading)[:64] or "intro" for section in skill.sections]
        if len(headings) != len(set(headings)):
            raise ValueError("Colliding source heading identities")
        rule_counts = Counter()
        for section in skill.sections:
            section_part = "section:" + exact_digest(section.heading)[:32]
            section_map = identity(section_part, PartKind.SECTION)
            section_id = section_map.entity_ids[0]
            value = section_value(section, section.body)
            raw_section = {**value, "body": section.body}
            add(section_part, PartKind.SECTION, raw_section, value, order=section.order)
            for order, rule in enumerate(section.rules):
                source_value = rule_value(rule, section_part, order, rule.group)
                key = [section_part, rule.body, rule.polarity.value, rule.reference_only, rule.group]
                part_id = "rule:" + exact_digest(key)[:32]
                rule_map = add(part_id, PartKind.RULE, source_value,
                               rule_value(rule, section_id, order, rule.group), order=order, section_id=section_id)
                rule_counts[self._rule_key(rule)] += 1
                for example_order, example in enumerate(rule.examples):
                    self._example(add, example, part_id, rule_map.entity_ids[0], "rule", example_order, section_id)
            for order, example in enumerate(section.examples):
                self._example(add, example, section_part, section_id, "section", order, section_id)
        # Rules the parser found outside any section (the generated overflow
        # file, mostly) are kept as unplaced parts; one that duplicates a
        # placed rule is the same rule seen twice, not a second part.
        for order, rule in enumerate(skill.rules):
            key = self._rule_key(rule)
            if rule_counts[key]:
                rule_counts[key] -= 1
                continue
            part_id = "rule:" + exact_digest(["overflow", *key])[:32]
            value = rule_value(rule, None, order, rule.group)
            mapping = add(part_id, PartKind.RULE, value, order=order)
            for example_order, example in enumerate(rule.examples):
                self._example(add, example, part_id, mapping.entity_ids[0], "rule", example_order, None)
        for entry in manifest:
            if entry.path == "SKILL.md" or entry.exclusion_reason == "generated_rule_overflow":
                continue
            add("file:" + entry.path, PartKind.FILE, None, evidence=file_evidence(entry, self.policy_version))
        revision = acquired.revision if isinstance(acquired, LocalPackage) else acquired.resolved_ref.commit
        return SourceSnapshot(ref=SnapshotRef(snapshot_id=snapshot_id, origin=origin), revision=revision,
            manifest=manifest, raw_frontmatter=raw, parsed_projection=tuple(parsed),
            effective_projection=tuple(effective),
            graph_mappings=tuple(mapped), projection_version=self.projection_version,
            policy_version=self.policy_version)

    @staticmethod
    def _rule_key(rule):
        return rule.body, rule.polarity.value, rule.reference_only, rule.group

    def _example(self, add, example, parent_part, parent_id, kind, order, section_id):
        key = [parent_part, example.body, example.kind.value, example.name, example.original_label]
        part_id = "example:" + exact_digest(key)[:32]
        source_args = {f"{kind}_id": parent_part}
        actual_args = {f"{kind}_id": parent_id}
        add(part_id, PartKind.EXAMPLE, example_value(example, **source_args, order=order),
            example_value(example, **actual_args, order=order), order=order, section_id=section_id)


def installed_kinds(base: SourceSnapshot | None) -> dict[str, str]:
    """Section kind by heading, the source section identity, as the base installed it."""
    if base is None:
        return {}
    return {part.evidence.value["heading"]: part.evidence.value["kind"] for part in base.effective_projection
            if part.kind == PartKind.SECTION and part.evidence.kind == "known"}
