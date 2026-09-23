import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from oms.domain.ids import slug as _slug
from oms.domain.models import Publication, Rule, Skill
from oms.domain.repo import is_repo_skill
from oms.domain.types import ArtefactKind, SectionKind, SkillStatus
from oms.ports.blob_store import BlobStore
from oms.ports.graph_store import GraphStore
from oms.publish.gate import GateResult, run_publish_gate
from oms.publish.parts import DocPart
from oms.ports.publishing import PublishingRenderer
from oms.publish.render import (
    DEFAULT_ROOT_INSTRUCTION_FILES, REFERENCES_FILE, ROOT_RULE_FORMATS,
    LocalPublishingRenderer, ManifestEntry, RenderedSkill, describe,
    has_nothing_to_say, publishable,
    render_claude_md, render_cursor_mcp_json, render_mcp_json,
    outline_skill_flat, outline_skill_sectioned,
    render_skill_sectioned,
    render_skill_with_examples, render_tier2_manifest, rewrite_resource_links,
    version_of,
)

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(
        self,
        store: GraphStore,
        body_budget: int = 200,
        blob_store: BlobStore | None = None,
        contribution_endpoint: str | None = None,
        root_instruction_files: list[str] | None = None,
        root_rule_formats: list[str] | None = None,
        mcp_endpoint: str | None = None,
        tier2_manifest_path: str | None = "tier2/AGENTS.md",
        bundle_token: str | None = None,
        distribution_repo: str | None = None,
        renderer: PublishingRenderer | None = None,
    ) -> None:
        self._store = store
        self._body_budget = body_budget
        self._blob_store = blob_store
        # When set, the published root file carries the standing contribution
        # instruction pointing agents at this ingest endpoint (spec §6.1).
        self._contribution_endpoint = contribution_endpoint
        # When set, baked into the shim and the MCP configs (spec §8.2): a
        # shared low-privilege secret proving bundle possession, never
        # identity, admitted server-side by BundleGuard as the fallback when
        # no personal credential resolves. None (the default) leaves every
        # published artefact byte-identical to before this existed.
        self._bundle_token = bundle_token
        # When set, publish emits .mcp.json / .cursor/mcp.json pointing
        # harnesses at the OMS HTTP MCP endpoint, so log_correction sits in
        # the agent's tool list rather than living only in prose.
        self._mcp_endpoint = mcp_endpoint
        # The always-loaded root file (constraints + skill index + contribution
        # instruction) is emitted under each framework's conventional filename
        # with identical content, so it reaches any harness. Default covers the
        # AGENTS.md cross-tool convention and Claude Code's CLAUDE.md, and is
        # shared with render.py because the installer has to write an import
        # for one of these names.
        self._root_files = list(root_instruction_files
                                or DEFAULT_ROOT_INSTRUCTION_FILES)
        # Frameworks with a directory-and-format rule scheme (Cursor, Windsurf)
        # get the same content wrapped in their format. None falls back to the
        # supported set; pass [] to emit only the plain markdown files.
        self._root_rule_formats = (
            list(ROOT_RULE_FORMATS) if root_rule_formats is None else root_rule_formats
        )
        # The Tier 2 manifest (spec §9.2) for hosts without native skill
        # selection: constraints inlined, dispatch table, version map. Written
        # beside the Tier 1 tree so one publish serves both tiers; None turns
        # it off for a deployment that has no MCP consumers.
        self._tier2_manifest_path = tier2_manifest_path
        # The remote the organisation clones, for the README's clone line.
        # Credential-stripped before it arrives (WebSettings.public_repo_url),
        # and stripped again by the renderer. None renders the README with no
        # clone line rather than one naming a URL nobody chose.
        self._distribution_repo = distribution_repo
        self._renderer = renderer if renderer is not None else LocalPublishingRenderer()

    def render_root(self, tenant_id: str, *, skills: list[Skill] | None = None) -> str:
        """Render the root constraints and index through the shared projection.

        Extensions can project the same selected skills into another target.
        A supplied selection is checked against the requested tenant.
        """
        selected = ([skill for skill, _ in self.publishable_skills(tenant_id)]
                    if skills is None else skills)
        if any(skill.tenant_id != tenant_id for skill in selected):
            raise ValueError("root instructions cannot contain another tenant's skills")
        return render_claude_md(
            self._store.active_constraints(tenant_id), selected,
            contribution_endpoint=self._contribution_endpoint)

    def check(self, tenant_id: str) -> GateResult:
        """The gate alone: everything `publish` would refuse over, with nothing
        written. The console's dry run - an administrator reads the reasons
        while the publish button is still unpressed, rather than learning them
        from a failed publish. Renders the same skills `publish` would, because
        half the gate's checks are about the rendered text."""
        rendered = self.publishable_skills(tenant_id)
        return run_publish_gate(
            self._store, tenant_id,
            rendered_skill_texts=[r.skill_md for _, r in rendered],
            blob_store=self._blob_store,
        )

    def publish(self, tenant_id: str, out_dir: Path) -> GateResult:
        rendered = self.publishable_skills(tenant_id)
        skills = [skill for skill, _ in rendered]

        gate = run_publish_gate(
            self._store, tenant_id,
            rendered_skill_texts=[r.skill_md for _, r in rendered],
            blob_store=self._blob_store,
        )
        if not gate.passed:
            return gate

        constraints = self._store.active_constraints(tenant_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        ownership_path = out_dir / ".oms-ownership.json"
        owned_hashes = None
        if ownership_path.is_file():
            try:
                owned_hashes = json.loads(ownership_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise ValueError("the publication ownership manifest cannot be read") from None
            if (not isinstance(owned_hashes, dict) or
                    any(not isinstance(ref, str) or not isinstance(value, str)
                        for ref, value in owned_hashes.items())):
                raise ValueError("the publication ownership manifest is invalid")
        if owned_hashes is None:
            # Legacy outputs use the database until their first local
            # manifest is committed. Later destinations never share it.
            previous_ids = ["(org)"]
            if (out_dir / "skills").is_dir():
                previous_ids += [child.name for child in (out_dir / "skills").iterdir() if child.is_dir()]
            owned_hashes = {record.source_ref: record.content_hash
                            for skill_id in previous_ids
                            for record in self._store.publications_for_skill(tenant_id, skill_id)}
        self._recover_publication_writes(out_dir, owned_hashes)
        # Local refreshers only reconcile a completed publication. Keep the
        # pending marker after an interrupted write until the next success.
        pending = out_dir / ".oms-publishing"
        pending.write_text("Publication in progress\n", encoding="utf-8")

        # Every root-level artefact this publish wrote, collected as it is
        # written rather than restated as a list: the prune at the end diffs
        # the ledger against this set, and a second statement of "what publish
        # emits" would eventually disagree with the code above it.
        root_refs: set[str] = {".oms-publication", ".oms-ownership.json"}

        root_md = self.render_root(tenant_id, skills=skills)
        for filename in self._root_files:
            root_refs.add(self._write_with_ledger(
                out_dir / filename, root_md,
                tenant_id=tenant_id, skill_id="(org)",
                source_ref=filename,
            ))
        for fmt in self._root_rule_formats:
            rel_path, wrap = ROOT_RULE_FORMATS[fmt]
            target = out_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            root_refs.add(self._write_with_ledger(
                target, wrap(root_md),
                tenant_id=tenant_id, skill_id="(org)",
                source_ref=rel_path,
            ))

        # The Tier 2 manifest (spec §9.2): same constraints, but the skill
        # bodies are replaced by fetch instructions and a version map, for
        # hosts that cannot select skills from a directory themselves.
        if self._tier2_manifest_path:
            manifest = render_tier2_manifest(
                constraints,
                [ManifestEntry(id=s.id, name=s.name, description=s.description,
                               version=version_of(self.fetch_view(s, r)[0]))
                 for s, r in rendered],
                mcp_endpoint=self._mcp_endpoint,
            )
            manifest_path = out_dir / self._tier2_manifest_path
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            root_refs.add(self._write_with_ledger(
                manifest_path, manifest,
                tenant_id=tenant_id, skill_id="(org)",
                source_ref=self._tier2_manifest_path,
            ))

        # The bundled, dependency-free contribution tool (spec §6): a typed
        # function harnesses can call instead of hand-building a request. Only
        # emitted when an ingest endpoint is configured, so it can be baked in.
        if self._contribution_endpoint:
            root_refs.add(self._write_with_ledger(
                out_dir / "oms_contribute.py",
                self._renderer.contribute_tool(self._contribution_endpoint,
                                        bundle_token=self._bundle_token),
                tenant_id=tenant_id, skill_id="(org)",
                source_ref="oms_contribute.py",
            ))

        # The once-per-machine bootstrap and its human-facing README: user-scope
        # install (skills links, instruction import, user-scoped MCP), so every
        # project on the machine picks the tree up with no per-project setup.
        installer = out_dir / "install.sh"
        # The same list the root files above were written from. The installer
        # imports one of them into the user's ~/.claude/CLAUDE.md, so a name it
        # invented for itself would survive exactly as long as the default:
        # narrow the setting to AGENTS.md and the prune below deletes the
        # CLAUDE.md the installer was still pointing at.
        root_refs.add(self._write_with_ledger(
            installer,
            self._renderer.install_script(self._mcp_endpoint, root_files=self._root_files,
                                  bundle_token=self._bundle_token),
            tenant_id=tenant_id, skill_id="(org)", source_ref="install.sh",
        ))
        installer.chmod(0o755)
        # The Windows twin. Native Windows has no POSIX shell, so the same
        # contract ships a second time in the one language a stock machine
        # already has. Not chmod'ed: PowerShell is invoked by file path, and
        # the execute bit means nothing on the platform this is for.
        root_refs.add(self._write_with_ledger(
            out_dir / "install.ps1",
            self._renderer.install_ps1(self._mcp_endpoint, root_files=self._root_files,
                               bundle_token=self._bundle_token),
            tenant_id=tenant_id, skill_id="(org)", source_ref="install.ps1",
        ))
        root_refs.add(self._write_with_ledger(
            out_dir / "README.md", self._renderer.readme(self._mcp_endpoint,
                                        distribution_repo=self._distribution_repo),
            tenant_id=tenant_id, skill_id="(org)", source_ref="README.md",
        ))

        # MCP wiring for harnesses with project-scoped config (Claude Code,
        # Cursor): rides the same pull as the skills, so the tool appears in
        # the agent's tool list after a one-time approval.
        if self._mcp_endpoint:
            root_refs.add(self._write_with_ledger(
                out_dir / ".mcp.json",
                render_mcp_json(self._mcp_endpoint, bundle_token=self._bundle_token),
                tenant_id=tenant_id, skill_id="(org)",
                source_ref=".mcp.json",
            ))
            cursor_mcp = out_dir / ".cursor" / "mcp.json"
            cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
            root_refs.add(self._write_with_ledger(
                cursor_mcp,
                render_cursor_mcp_json(self._mcp_endpoint, bundle_token=self._bundle_token),
                tenant_id=tenant_id, skill_id="(org)",
                source_ref=".cursor/mcp.json",
            ))

        # Retired skills leave the tree: prune-on-publish, ledger-driven so a
        # directory OMS cannot prove it created is never touched.
        self._prune_stale_skill_dirs(tenant_id, out_dir,
                                     published_ids={s.id for s, _ in rendered},
                                     owned_hashes=owned_hashes)
        # Same for root artefacts a withdrawn setting has stopped producing.
        # Runs here because it needs the full set of this publish's root
        # writes, which the block above completes.
        self._prune_stale_root_files(tenant_id, out_dir, written_refs=root_refs,
                                    owned_hashes=owned_hashes)

        for skill, r in rendered:
            skill_id = skill.id
            skill_dir = out_dir / "skills" / skill_id
            skill_dir.mkdir(parents=True, exist_ok=True)
            self._write_with_ledger(
                skill_dir / "SKILL.md", r.skill_md,
                tenant_id=tenant_id, skill_id=skill_id,
                source_ref=f"skills/{skill_id}/SKILL.md",
            )
            if r.references_md is not None:
                ref_path = skill_dir / REFERENCES_FILE
                ref_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_with_ledger(
                    ref_path, r.references_md,
                    tenant_id=tenant_id, skill_id=skill_id,
                    source_ref=f"skills/{skill_id}/{REFERENCES_FILE}",
                )
            # Write artefacts back to disk from the BlobStore (when wired).
            self._write_artefacts(skill_id, skill_dir, tenant_id)

        # Each destination retains its own proof of ownership. The database
        # ledger describes the most recent publish, which may have gone to a
        # different folder/repository with different bytes. Without this,
        # returning to an older destination can keep a now-muted skill there.
        ownership = dict(owned_hashes or {})
        for skill_id in ("(org)", *(skill.id for skill, _ in rendered)):
            for record in self._store.publications_for_skill(tenant_id, skill_id):
                path = out_dir / record.source_ref
                if (path.resolve().is_relative_to(out_dir.resolve()) and path.is_file() and
                        hashlib.sha256(path.read_bytes()).hexdigest() == record.content_hash):
                    ownership[record.source_ref] = record.content_hash
        ownership_tmp = out_dir / ".oms-ownership.tmp"
        ownership_tmp.write_text(json.dumps(ownership, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        ownership_tmp.replace(ownership_path)
        (out_dir / ".oms-writes.jsonl").unlink(missing_ok=True)

        # A stable content revision means unchanged publications do not make
        # Git commits and a local refresher does not repeatedly reinstall.
        digest = hashlib.sha256()
        for directory, names, files in os.walk(out_dir):
            names[:] = sorted(name for name in names if name != ".git")
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(out_dir).as_posix()
                if relative in (".oms-publication", ".oms-publishing", ".oms-publication.tmp") or path.is_symlink():
                    continue
                digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
        revision = out_dir / ".oms-publication.tmp"
        revision.write_text(digest.hexdigest() + "\n", encoding="utf-8")
        revision.replace(out_dir / ".oms-publication")
        pending.unlink()

        return gate



    def render_skill(self, skill_id: str, tenant_id: str) -> str:
        """Render a single skill's SKILL.md as a string, without writing to disk.
        Reuses the same renderer used by publish() so output matches exactly."""
        skill = self._store.get_skill(skill_id)
        if skill is None or skill.tenant_id != tenant_id:
            raise KeyError(f"skill {skill_id!r} not found for tenant {tenant_id!r}")
        return self._render(skill).skill_md

    def render_package(self, skill: Skill) -> RenderedSkill:
        """Render a skill's SKILL.md *and* its Level 3 references file, without
        writing to disk. The Tier 2 catalogue (§9.2) serves both from here, so
        an on-demand query and the published tree cannot drift. Takes the Skill
        because the caller has already loaded it."""
        return self._render(skill)

    def resource_names(self, skill_id: str, has_references: bool) -> list[str]:
        """The Level 3 files published alongside a skill: the generated overflow
        file when the body overran its budget, then the imported artefacts. The
        publisher-owned files regenerated on every publish are not resources."""
        names = [REFERENCES_FILE] if has_references else []
        names += [path for _artefact, path in self._store.artefacts_for_skill(skill_id)
                  if path not in (REFERENCES_FILE, "SKILL.md", "CLAUDE.md")]
        return names

    def fetch_view(self, skill: Skill, package: RenderedSkill) -> tuple[str, list[str]]:
        """The Tier 2 view of a rendered skill: the same content, with every
        pointer to one of its own reference files naming the MCP call instead
        of a path, since a fetching host has no directory to read (§9.2).

        Publish computes the manifest's version map through here too, so the
        versions it quotes are the versions the read tools serve."""
        resources = self.resource_names(skill.id, package.references_md is not None)
        return rewrite_resource_links(package.skill_md, skill.id, resources), resources

    def publishable_skills(self, tenant_id: str) -> list[tuple[Skill, RenderedSkill]]:
        """This tenant's skills, minus the ones with nothing in them yet.

        `_skills` in the name, not just `publishable`, because this module
        already imports `render.publishable`, which answers the same question
        of a RULE. Two of one word in one file is a reader's problem every time
        afterwards.

        Public because it is the definition of "what the tree contains", and a
        second surface now has to agree with it exactly: a personal overlay
        renders the same root files over the same skills minus the caller's
        mutes, and a selection that differed from this one would put a skill in
        an agent's index that publish never wrote a directory for.

        Skipped rather than emitted-and-ignored: an empty skill that reaches the
        tree is worse than absent, because the skill index and the Tier 2
        dispatch table both tell agents to use it. Nothing is marked and no
        state is kept - a skill returns to the tree by the ordinary route, on
        the first publish after it has something to say.

        A skill whose only content is held for review is empty for publication
        purposes until that content becomes eligible.
        """
        out: list[tuple[Skill, RenderedSkill]] = []
        for skill in self._store.skills_for_tenant(tenant_id):
            if not skill.publish_enabled:
                continue
            if is_repo_skill(skill.id):
                # A repo bundle is reachable by naming it through `query_skill`
                # and by no other route (design §3.2). This one `continue`
                # covers four surfaces - publish, check, managed_bundle and the
                # personal overlay, which reuses this function - because this
                # method is, as its docstring says, the definition of what the
                # tree contains.
                #
                # Keyed on the id, never on `Skill.repo`. The id is the node's
                # primary key, so no adapter can round-trip a skill without it;
                # a nullable field can be dropped by a hand-written write and
                # read back as None, which here would mean "publish it".
                continue
            if skill.status is SkillStatus.REJECTED:
                # A human turned this auto-creation down. Rejection already
                # retires the rules it was minted for, so `has_nothing_to_say`
                # below would usually catch it - but only until a later
                # correction anchors a fresh rule here, and the review item
                # MERGEs on id so nobody would be asked a second time. Only an
                # explicit rejection is filtered; FLAGGED skills nobody has
                # decided on still publish, because filtering those would
                # silently withdraw skills already in every agent's file.
                logger.info("publish: skipping %s, which was rejected at review",
                            skill.id)
                continue
            body = self._render(skill)
            if has_nothing_to_say(body.skill_md):
                logger.info("publish: skipping %s, which has no content yet", skill.id)
                continue
            out.append((skill, body))
        return out

    def outline_skill(self, skill_id: str, tenant_id: str) -> tuple[list[DocPart], str]:
        """The skill as addressable parts, plus which renderer produced them.

        The same branch `_render` takes, and it has to stay the same branch: a
        console that outlined a skill one way while publish rendered it another
        would show an editor over a document that does not exist. So this calls
        the same store methods in the same order and tests the same condition,
        rather than re-deriving the choice from the skill.

        `path` travels out because the two documents differ in what can be
        edited. A flat skill has no section headings to rename, and an editor
        that simply showed none would look broken rather than correct.
        """
        skill = self._store.get_skill(skill_id)
        if skill is None or skill.tenant_id != tenant_id:
            raise KeyError(f"skill {skill_id!r} not found for tenant {tenant_id!r}")
        sections = self._store.sections_for_skill(skill.id)
        blocks_by = {s.id: self._store.blocks_for_section(s.id) for s in sections}
        if any(blocks_by.values()):
            rules_by = {s.id: self._store.rules_for_section(s.id) for s in sections
                        if s.kind is SectionKind.RULES}
            placements_by = {s.id: self._store.rule_placements_for_section(s.id)
                             for s in sections}
            ex_by_sec = {s.id: self._store.examples_for_section(s.id) for s in sections}
            section_rule_ids = {r.id for rs in rules_by.values() for r in rs}
            orphans = [r for r in self._store.rules_for_skill(skill.id)
                       if r.id not in section_rule_ids]
            all_rules = [r for rs in rules_by.values() for r in rs] + orphans
            active = sorted((r for r in all_rules if publishable(r)),
                            key=lambda r: r.corroboration_count, reverse=True)
            skill.description = describe(skill, active)
            examples_by_rule = {r.id: self._store.examples_for_rule(r.id) for r in all_rules}
            parts, _ = outline_skill_sectioned(
                skill, sections,
                rules_by_section=rules_by, blocks_by_section=blocks_by,
                examples_by_section=ex_by_sec, examples_by_rule=examples_by_rule,
                extra_body_rules=orphans, placements_by_section=placements_by,
                decisions=self._decisions(skill.id),
            )
            return parts, "sectioned"
        rules = self._store.rules_for_skill(skill.id)
        active = [r for r in rules if publishable(r)]
        active.sort(key=lambda r: r.corroboration_count, reverse=True)
        skill.description = describe(skill, active)
        examples_by_rule = {r.id: self._store.examples_for_rule(r.id) for r in rules}
        skill_examples = self._store.examples_for_skill(skill.id)
        parts, _ = outline_skill_flat(
            skill, rules, examples_by_rule=examples_by_rule,
            skill_examples=skill_examples, body_budget=self._body_budget,
        )
        return parts, "flat"

    def _decisions(self, skill_id: str) -> list[Rule] | None:
        """Extensions may supply a prioritised rules block; local publication
        retains the document's manually chosen rule positions."""
        return None

    def _render(self, skill: Skill) -> RenderedSkill:
        """Render a skill to SKILL.md. Uses the authorial section structure when
        the skill has sections (preserving headings, prose and checklists), and
        falls back to a flat rule list for skills without section custody."""
        sections = self._store.sections_for_skill(skill.id)
        blocks_by = {s.id: self._store.blocks_for_section(s.id) for s in sections}
        # Only the authorial-section structure is worth preserving; a skill whose
        # content is purely aggregated rules (no content blocks) renders the same
        # either way, so it stays on the flat path (which keeps the references file).
        if any(blocks_by.values()):
            rules_by = {s.id: self._store.rules_for_section(s.id) for s in sections
                        if s.kind is SectionKind.RULES}
            placements_by = {s.id: self._store.rule_placements_for_section(s.id) for s in sections}
            ex_by_sec = {s.id: self._store.examples_for_section(s.id) for s in sections}
            section_rule_ids = {r.id for rs in rules_by.values() for r in rs}
            # Only a rules section actually renders CONTAINS_RULE memberships.
            # Reimport can reclassify its heading as prose while a removal is
            # awaiting review. Keep those live rules visible as extras until
            # an operator explicitly retires them.
            orphans = [r for r in self._store.rules_for_skill(skill.id)
                       if r.id not in section_rule_ids]
            all_rules = [r for rs in rules_by.values() for r in rs] + orphans
            active = sorted((r for r in all_rules if publishable(r)),
                            key=lambda r: r.corroboration_count, reverse=True)
            skill.description = describe(skill, active)
            examples_by_rule = {r.id: self._store.examples_for_rule(r.id) for r in all_rules}
            return render_skill_sectioned(
                skill, sections,
                rules_by_section=rules_by, blocks_by_section=blocks_by,
                examples_by_section=ex_by_sec, examples_by_rule=examples_by_rule,
                extra_body_rules=orphans, placements_by_section=placements_by,
                decisions=self._decisions(skill.id),
            )
        rules = self._store.rules_for_skill(skill.id)
        active = [r for r in rules if publishable(r)]
        active.sort(key=lambda r: r.corroboration_count, reverse=True)
        skill.description = describe(skill, active)
        examples_by_rule = {r.id: self._store.examples_for_rule(r.id) for r in rules}
        skill_examples = self._store.examples_for_skill(skill.id)
        return render_skill_with_examples(
            skill, rules, examples_by_rule=examples_by_rule,
            skill_examples=skill_examples, body_budget=self._body_budget,
        )

    def _write_with_ledger(
        self, path: Path, body: str, *,
        tenant_id: str, skill_id: str, source_ref: str,
    ) -> str:
        """Write the file and record it in the publication ledger. Returns the
        source_ref it ledgered, so a caller tracking what this publish emitted
        can collect it in the same expression as the write rather than keeping
        a second list beside the writes."""
        content_hash = self._write_published_file(path, body.encode("utf-8"), source_ref)
        self._store.upsert_publication(Publication(
            id=f"publication-{tenant_id}-{_slug(source_ref)}",
            skill_id=skill_id, source_ref=source_ref,
            content_hash=content_hash,
            published_at=datetime.now(timezone.utc),
            tenant_id=tenant_id,
        ))
        return source_ref

    @staticmethod
    def _write_published_file(path: Path, body: bytes, source_ref: str) -> str:
        """Journal ownership before atomically replacing generated bytes.

        Recording only at the end leaves an interrupted publish with new
        files and old ownership hashes. A later mute then mistakes those new
        files for hand edits. The write-ahead record names an intended hash;
        recovery trusts it only if the current file actually has those bytes.
        """
        root = path.parents[len(Path(source_ref).parts) - 1]
        journal = root / ".oms-writes.jsonl"
        if journal.is_symlink() or not path.parent.resolve().is_relative_to(root.resolve()):
            raise ValueError("publication writes must stay inside the output directory")
        digest = hashlib.sha256(body).hexdigest()
        temporary = path.with_name(f".{path.name}.oms-{uuid4().hex}")
        record = {"path": source_ref, "sha256": digest,
                  "temporary": temporary.relative_to(root).as_posix()}
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        created = False
        try:
            # Match ordinary file creation permissions, including inherited
            # ACLs that let the host owner manage Docker-published files.
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            created = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
        return digest

    @staticmethod
    def _recover_publication_writes(out_dir: Path, owned_hashes: dict[str, str]) -> None:
        journal = out_dir / ".oms-writes.jsonl"
        if journal.is_symlink():
            raise ValueError("the publication write journal cannot be a symbolic link")
        if not journal.is_file():
            return
        root = out_dir.resolve()
        candidates: dict[str, set[str]] = {}
        with journal.open("r+", encoding="utf-8") as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    # The writer never started its file replacement unless
                    # the complete journal line was flushed successfully.
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
                    break
                try:
                    record = json.loads(line)
                    ref, digest, temporary_ref = record["path"], record["sha256"], record["temporary"]
                    if not all(isinstance(value, str) for value in (ref, digest, temporary_ref)):
                        raise ValueError("invalid record")
                    target, temporary = out_dir / ref, out_dir / temporary_ref
                    prefix = f".{target.name}.oms-"
                    token = temporary.name.removeprefix(prefix)
                    if (root not in target.resolve().parents or root not in temporary.resolve().parents
                            or temporary.parent != target.parent or not temporary.name.startswith(prefix)
                            or len(token) != 32
                            or any(char not in "0123456789abcdef" for char in token)):
                        raise ValueError("invalid paths")
                except (ValueError, KeyError, TypeError):
                    raise ValueError("the publication write journal is invalid") from None
                candidates.setdefault(ref, set()).add(digest)
                # A killed writer can leave only its uniquely named temporary
                # file. It was never installed and is safe to discard.
                temporary.unlink(missing_ok=True)
        for ref, digests in candidates.items():
            target = out_dir / ref
            if target.is_file():
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual in digests:
                    owned_hashes[ref] = actual

    def _prune_stale_skill_dirs(self, tenant_id: str, out_dir: Path,
                                published_ids: set[str],
                                owned_hashes: dict[str, str] | None = None) -> None:
        """Delete skills/<id> directories for skills no longer in the graph -
        but ONLY what the publication ledger proves OMS created. A directory
        with no ledger rows is someone else's and is invisible to the prune.
        A directory whose files no longer match their ledgered hashes (or that
        holds files OMS never wrote) was hand-edited: keep it and warn, never
        destroy the evidence."""
        skills_dir = out_dir / "skills"
        if not skills_dir.is_dir():
            return
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir() or child.name in published_ids:
                continue
            prefix = f"skills/{child.name}/"
            if owned_hashes is None:
                ledgered = self._store.publications_for_skill(tenant_id, child.name)
                hashes = {p.source_ref[len(prefix):]: p.content_hash
                          for p in ledgered if p.source_ref.startswith(prefix)}
            else:
                hashes = {ref[len(prefix):]: digest for ref, digest in owned_hashes.items()
                          if ref.startswith(prefix)}
            if not hashes:
                continue  # no proof of ownership: not ours to delete
            edited = None
            for f in sorted(child.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(child).as_posix()
                if hashes.get(rel) != hashlib.sha256(f.read_bytes()).hexdigest():
                    edited = rel
                    break
            if edited is not None:
                logger.warning(
                    "publish: not pruning retired skill directory skills/%s: "
                    "%s is not the file OMS published (hand-edited or added)",
                    child.name, edited)
                continue
            shutil.rmtree(child)
            if owned_hashes is not None:
                for ref in list(owned_hashes):
                    if ref.startswith(prefix):
                        owned_hashes.pop(ref)
            self._store.delete_publications_for_skill(tenant_id, child.name)
            logger.info("publish: pruned retired skill directory skills/%s", child.name)

    def _prune_stale_root_files(self, tenant_id: str, out_dir: Path,
                                written_refs: set[str],
                                owned_hashes: dict[str, str] | None = None) -> None:
        """Delete root-level artefacts this publish no longer writes.

        .mcp.json, .cursor/mcp.json, oms_contribute.py and tier2/AGENTS.md are
        all conditional on a setting. Withdraw the setting and publish simply
        stops writing the file, which used to leave the last copy in place -
        and since the published tree is a git repository the organisation
        clones (and re-hard-resets to), a stale copy carries a dead endpoint,
        and .mcp.json's superseded bearer token, to every machine indefinitely.
        Rotating a token or moving a URL never needed this: the file is
        rewritten. Only removal leaves an orphan.

        Ownership follows the skill prune. The candidates come from the
        ledger's own rows for the root bundle (all filed under the "(org)"
        pseudo-skill), so a root file OMS never wrote has no row and is
        invisible here; a file whose bytes no longer match its ledgered hash
        was hand-edited and is kept with a warning rather than destroyed. It
        differs in one respect, deliberately: the row is dropped a publish
        later than the deletion, once the file is observed gone (see below).

        Nothing outside `out_dir` is touched, whatever a ledgered ref says.
        Every path here is re-derived from the resolved output root and must
        resolve strictly inside it, so no ref shape can reach past the tree
        this publish owns. A ref carrying `..` is the case that matters: the
        publisher is handed root file names from configuration, and a row
        written before that configuration was validated still sits in the
        ledger of any deployment that ran with one."""
        root = out_dir.resolve()
        database_records = {p.source_ref: p.content_hash
                            for p in self._store.publications_for_skill(tenant_id, "(org)")}
        records = (database_records if owned_hashes is None else
                   {ref: digest for ref, digest in owned_hashes.items() if not ref.startswith("skills/")})
        # Keep reporting unsafe historical ledger entries even when this
        # destination has its own manifest. They remain ineligible for prune.
        records.update({ref: digest for ref, digest in database_records.items()
                        if root not in (out_dir / ref).resolve().parents})
        for source_ref, content_hash in sorted(records.items()):
            if source_ref in written_refs:
                continue
            target = out_dir / source_ref
            if root not in target.resolve().parents:
                logger.warning(
                    "publish: not pruning retired root file %s: it resolves "
                    "outside the published tree", source_ref)
                continue
            if target.is_file():
                if hashlib.sha256(target.read_bytes()).hexdigest() != content_hash:
                    logger.warning(
                        "publish: not pruning retired root file %s: it is not the "
                        "file OMS published (hand-edited)", source_ref)
                    continue
                target.unlink()
                self._prune_emptied_dirs(root, target.parent)
                logger.info("publish: pruned retired root file %s", source_ref)
                # The row outlives the deletion on purpose. publish_to_repo
                # prunes inside the checkout and pushes afterwards, so the
                # removal is not durable yet: if the push fails, the next run's
                # `reset --hard origin/main` brings the file back, and a row
                # dropped here would leave OMS unable to prove it wrote it -
                # permanently invisible to the prune, and re-pushed by
                # `git add -A`. Keeping it makes the next publish try again.
                continue
            # The file is not in the tree, which is the acknowledgement: the
            # removal survived whatever round trip the tree makes. Now the row
            # can go, and the prune becomes one-shot as it is for skills - a
            # copy put back by hand afterwards has no ledger row, so it is
            # somebody else's file and is left alone.
            self._store.delete_publication(tenant_id, source_ref)
            if owned_hashes is not None:
                owned_hashes.pop(source_ref, None)

    def _prune_emptied_dirs(self, root: Path, directory: Path) -> None:
        """Walk up from a pruned file's directory, dropping the ones it left
        empty (tier2/, and .cursor/ where no rules file is published) and
        stopping at the first that still holds something, because that content
        is not ours.

        `root` is the RESOLVED output root and bounds the walk structurally:
        the loop runs only while the candidate is strictly inside it, so it
        cannot climb out of the published tree however it got here. `root` is
        never in its own parents, so the root itself is never a candidate."""
        current = directory.resolve()
        while root in current.parents:
            if any(current.iterdir()):
                return
            current.rmdir()
            current = current.parent

    def _write_artefacts(self, skill_id: str, skill_dir: Path, tenant_id: str) -> None:
        artefacts = self._store.artefacts_for_skill(skill_id)
        for artefact, path in artefacts:
            # Don't overwrite SKILL.md / CLAUDE.md / the overflow references
            # file - those are publisher-owned and regenerated every publish.
            if path in ("SKILL.md", "CLAUDE.md", REFERENCES_FILE):
                continue
            out_path = skill_dir / path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if self._blob_store is not None:
                try:
                    body = self._blob_store.get(artefact.content_ref)
                except Exception:
                    continue       # missing blob is reported by fsck, not here
                content_hash = self._write_published_file(out_path, body, f"skills/{skill_id}/{path}")
                self._store.upsert_publication(Publication(
                    id=f"publication-{tenant_id}-{_slug(f'skills/{skill_id}/{path}')}",
                    skill_id=skill_id,
                    source_ref=f"skills/{skill_id}/{path}",
                    content_hash=content_hash,
                    published_at=datetime.now(timezone.utc),
                    tenant_id=tenant_id,
                ))
