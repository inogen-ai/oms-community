from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.domain.identity import SkillRef
from oms.sources.discovery import Discoveries
from oms.sources.models import (
    ActionContext, AcquiredPackage, ApplyRequest, CheckRequest, CheckResult,
    DiscoveryResult, DiscoveredPackage, Evidence, InstallRequest, InstallSelection,
    ManifestEntry, ResolvedRef,
)
from oms.sources.projection import ProjectionBuilder


class Policy:
    held = False
    policy_generation = 0

    def revision(self, context, graph, reviews, skills):
        return str(self.policy_generation)

    def admit(self, action, context, skills):
        if any(skill.tenant_id != context.tenant_id for skill in skills):
            raise PermissionError("foreign skill")

    def admit_operation(self, context, operation, graph):
        if operation.context.tenant_id != context.tenant_id or operation.context.actor_id != context.actor_id:
            raise PermissionError("foreign operation")
        refs = {proof.skill for proof in operation.scope_proofs} | {row.skill for row in operation.result.outcomes}
        refs.update(member.skill for member in operation.batch_members)
        self.admit("operation", context, tuple(sorted(refs)))

    def admit_install(self, context, request):
        pass

    def screen(self, snapshot, local):
        return CheckResult(state="held" if self.held else "passed", code="fixture")

    def screen_resolved(self, snapshot, local, writes, *, manifest=None):
        result = self.screen(snapshot, local)
        if result.state != "passed":
            return result
        from oms.sources.safety import screen_selected
        return screen_selected(snapshot, writes, self.blobs, manifest=manifest)

    def admit_local(self, context, request):
        pass

    def admit_profile(self, context, profile_id):
        pass

    def automatic_eligibility(self, plan, binding):
        return CheckResult(state="passed", code="fixture")


class SourceWorld:
    def __init__(self, case, tmp_path):
        from oms.sources.service import SourceService
        self.case = case
        self.store, self.queue, self.repository = case.store, case.queue, case.repository
        self.skill = SkillRef("acme", "expenses")
        self.store.delete_skill("expenses", tenant_id="acme")
        self.blobs = FileBlobStore(tmp_path / "blobs")
        self.now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.context = ActionContext(tenant_id="acme", actor_id="reviewer", domain_scope=("finance",), capabilities=frozenset({"sources:write"}))
        self.policy = Policy()
        self.policy.blobs = self.blobs
        self.history_failure = False
        first = self.package("a", "First description")
        self.incoming = self.package("b", "Second description")
        world = self

        class Reader:
            reads = 0
            discovery_reads = 0
            before_discover = None
            before_read = None

            def discover(self, request):
                self.discovery_reads += 1
                if self.before_discover:
                    self.before_discover()
                return world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer").model_copy(
                    update={"discovery_id": "fetched-discovery"})

            def resolve(self, request):
                return world.incoming.resolved_ref.model_copy(update={"kind": request.ref_kind or "branch", "name": request.ref_name or "main"})

            def read(self, request):
                self.reads += 1
                if self.before_read:
                    self.before_read()
                assert request.resolved_ref.commit == world.incoming.resolved_ref.commit
                return world.incoming.model_copy(update={"resolved_ref": request.resolved_ref})
        self.reader = Reader()
        self.discoveries = Discoveries(self.sources, authorise=lambda *args: None, clock=lambda: self.now)
        self.discoveries.save(DiscoveryResult(discovery_id="discovery", tenant_id="acme", actor_id="reviewer",
            resolved_ref=first.resolved_ref, expires_at=self.now + timedelta(hours=1),
            packages=(DiscoveredPackage(path="", valid=True, upstream_name="expenses"),), acquired_packages=(first,)))

        def factory_for(context, action, skills):
            def factory(graph, queue):
                from dataclasses import replace
                bound = case.factory(graph, queue)
                if world.history_failure:
                    class History:
                        def capture_required(self, *args, **kwargs):
                            raise OSError("required history unavailable")
                    bound = replace(bound, history=History())
                return bound
            return factory
        self.service = SourceService(self.repository, factory_for, self.reader,
            ProjectionBuilder(self.blobs, tmp_path / "work"), self.policy, self.discoveries,
            clock=lambda: self.now)

    @property
    def sources(self):
        return self.case.sources

    def package(self, revision, description, *, prose="Keep original documents.", tags=("receipts",), procedure_heading="Procedure", example=None, rule="Keep receipts."):
        from hashlib import sha256
        example_line = f"  Example: {example}\n" if example is not None else ""
        body = f"---\nname: expenses\ndescription: {description}\ntags: [{", ".join(tags)}]\n---\n## Rules\n- {rule}\n{example_line}\n## {procedure_heading}\n{prose}\n".encode()
        digest = sha256(body).hexdigest()
        blob = self.blobs.put(body)
        return AcquiredPackage(history_evidence="initial" if revision == "a" else "proven_ancestor",
            resolved_ref=ResolvedRef(canonical_url="https://github.com/example/skills", kind="branch", name="main", commit=revision * 40),
            package_path="", raw_frontmatter=Evidence(kind="unknown", policy_version="1"),
            manifest=(ManifestEntry(path="SKILL.md", blob_ref=blob, digest=digest, size=len(body), mode=0o100644),))

    def install(self):
        return self.service.install(self.context, InstallRequest(discovery_id="discovery",
            selections=(InstallSelection(package_path="", local_name="expenses", domain="finance"),)), idempotency_key="install")

    def check(self, key="check"):
        binding = self.sources.get_binding(self.skill)
        source = self.sources.get_source(binding.source_id, tenant_id="acme")
        return self.service.check(self.context, CheckRequest(source_id=source.source_id,
            expected_source_generation=source.generation, skills=(self.skill,)), idempotency_key=key)

    def open_update(self):
        self.install()
        result = self.check()
        update = self.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
        return SimpleNamespace(update=update, before=self.snapshot())

    def apply(self, candidate):
        update = candidate.update
        return self.service.apply(self.context, ApplyRequest(update_id=update.update_id,
            skill=self.skill, fingerprint=update.plan.fingerprint), idempotency_key="apply")

    def description(self):
        return self.store.get_skill("expenses", tenant_id="acme").description

    def edit_description(self, text):
        return self.case.edit_description(text)

    def baseline_revision(self, origin="github"):
        binding = self.sources.get_binding(self.skill)
        return self.sources.get_snapshot(binding.baseline).revision

    def fail_required_history(self):
        self.history_failure = True

    def snapshot(self):
        from oms.sources.mutation import content_digest
        return (content_digest(self.store, self.skill), self.sources.get_generations(self.skill),
            self.sources.get_binding(self.skill), self.sources.open_update(self.skill),
            self.store.skill_versions("acme", "expenses"), deepcopy(self.queue.pending("acme")))

    def submit_local(self, body, key="local"):
        from hashlib import sha256
        from oms.sources.models import LocalPackage, LocalImportRequest
        digest = sha256(body).hexdigest()
        package = LocalPackage(revision=digest, package_path="", raw_frontmatter=Evidence(kind="unknown", policy_version="1"),
            manifest=(ManifestEntry(path="SKILL.md", blob_ref=self.blobs.put(body), digest=digest, size=len(body), mode=0o100644),))
        guard = self.sources.get_generations(self.skill)
        return self.service.submit_local(self.context, LocalImportRequest(skill=self.skill, package=package,
            expected_content_generation=guard.content, expected_binding_generation=guard.binding, transport="cli"), idempotency_key=key)

    def open_local_review(self):
        self.install()
        return self.submit_local(b"---\nname: expenses\ndescription: Local proposal\n---\n## Rules\n- Keep receipts.\n")

    def resolve_current(self):
        from oms.sources.models import UpdateRequest
        update = self.sources.open_update(self.skill)
        return self.service.adopt(self.context, UpdateRequest(update_id=update.update_id, skill=self.skill,
            fingerprint=update.plan.fingerprint), idempotency_key="resolve-current")

    def retarget(self, kind, name):
        from oms.sources.models import RetargetRequest
        guard = self.sources.get_generations(self.skill)
        ref = self.incoming.resolved_ref.model_copy(update={"kind": kind, "name": name})
        return self.service.retarget(self.context, RetargetRequest(skill=self.skill, ref=ref,
            expected_content_generation=guard.content, expected_binding_generation=guard.binding), idempotency_key="retarget")

    def retry_needed(self, action):
        return any(row.action == action and row.retry_needed for row in self.sources.blocked_attempts(self.skill))

    def open_file_update(self, path, *, old_mode=0o100644, new_mode=0o100644, new_bytes=b"Updated supporting document\n"):
        from hashlib import sha256

        def entry(body, mode):
            if isinstance(mode, str):
                mode = int(mode, 8)
            return ManifestEntry(path=path, blob_ref=self.blobs.put(body), digest=sha256(body).hexdigest(), size=len(body), mode=mode)
        first = self.package("a", "First description")
        original = b"Original supporting document\n"
        first = first.model_copy(update={"manifest": (*first.manifest, entry(original, old_mode))})
        self.discoveries.save(DiscoveryResult(discovery_id="file-package", tenant_id="acme", actor_id="reviewer",
            resolved_ref=first.resolved_ref, expires_at=self.now + timedelta(hours=1),
            packages=(DiscoveredPackage(path="", valid=True),), acquired_packages=(first,)))
        self.service.install(self.context, InstallRequest(discovery_id="file-package",
            selections=(InstallSelection(package_path="", local_name="expenses", domain="finance"),)), idempotency_key="install")
        incoming = self.package("b", "First description")
        if new_bytes is not None:
            incoming = incoming.model_copy(update={"manifest": (*incoming.manifest, entry(new_bytes, new_mode))})
        self.incoming = incoming
        result = self.check()
        update = self.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
        return SimpleNamespace(update=update, before=self.snapshot())

    def open_two_updates(self):
        from oms.sources.models import CheckRequest
        first = self.package("a", "First description")
        other = first.model_copy(update={"package_path": "other"})
        self.discoveries.save(DiscoveryResult(discovery_id="two", tenant_id="acme", actor_id="reviewer",
            resolved_ref=first.resolved_ref, expires_at=self.now + timedelta(hours=1),
            packages=(DiscoveredPackage(path="", valid=True), DiscoveredPackage(path="other", valid=True)),
            acquired_packages=(first, other)))
        self.service.install(self.context, InstallRequest(discovery_id="two", selections=(
            InstallSelection(package_path="", local_name="expenses", domain="finance"),
            InstallSelection(package_path="other", local_name="other", domain="finance"))), idempotency_key="install")
        packages = {"": self.package("b", "Second description"), "other": self.package("b", "Second other").model_copy(update={"package_path": "other"})}

        def read(request):
            self.reader.reads += 1
            return packages[request.package_path].model_copy(update={"resolved_ref": request.resolved_ref})
        self.reader.read = read
        source_id = self.sources.get_binding(self.skill).source_id
        source = self.sources.get_source(source_id, tenant_id="acme")
        result = self.service.check(self.context, CheckRequest(source_id=source_id, expected_source_generation=source.generation,
            skills=(self.skill, SkillRef("acme", "other"))), idempotency_key="check")
        return tuple(self.sources.get_update(outcome.update_id, tenant_id="acme") for outcome in result.outcomes)
