"""Uploading a skill package: intake, dry run, then apply on approval.

The unit under custody is a directory, not a document. A skill is `SKILL.md`
plus `references/*.md` plus scripts, templates and fixtures, and those are
`Artefact` nodes with bytes in a blob store. A markdown editor cannot express
any of that, and would invite edits to the generated half of a rendered
`SKILL.md` that publish would silently revert.

So an upload is a PROPOSAL, which is what the spec already says a file edit
should be once a skill is under OMS custody (§6.5): stage it, show what would
change, apply only when somebody says so. Apply runs the real `SkillImporter`
over the extracted tree, so the whole pipeline runs - dedup, conflict
detection, the publication-ledger no-op, the two-writer escalation - rather
than a second implementation of it that could disagree.

Three things here exist only because the bytes now arrive from a browser
rather than from an operator's own checkout:

* **the archive is checked before anything is written** - total size, per-file
  size, member count, and any member whose path leaves the extraction root;
* **text artefacts pass the sanitiser**, which the import path never did.
  §4.4 says imported text is sanitised before persistence, and that was true of
  rules and not of the files beside them. What it redacts is reported, not
  silently applied;
* **nothing is applied without a second call.** A staged upload that is never
  approved leaves no trace in the graph.
"""
from __future__ import annotations

import shutil
import os
import re
import stat
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Literal

from oms.domain.ids import normalise as _normalise
from oms.domain.repo import ReservedSkillId
from oms.import_skills.importer import ImportReport, ImportState, PreparedImport, SkillImporter
from oms.import_skills.identity import ImportIdentityConflict
from oms.import_skills.parser import ParsedSkill
from oms.ingestion.schema import ExecutionContext
from oms.ports.graph_store import GraphStore
from oms.sources.errors import SourceConflict, StaleMutation
from oms.sources.models import LocalPackage

# The longest filename kept, and what an unusable one becomes. Both are about
# display rather than safety - the name is never joined onto a path, only
# shown back to a person, whether in a portal queue row or a history line -
# but a name that is one enormous word, or that is empty, is something
# nothing downstream can read.
_MAX_FILENAME = 200
_FALLBACK_FILENAME = "upload.zip"
# What `stage` writes under the staging root: the extracted tree, its retained
# manifest, and a manifest's temporary file. Anything else there is left alone.
_STAGED_ENTRY = re.compile(r"(upload-[0-9a-f]{32})(?:\.json)?|\.source-stage-.+")


def clean_filename(raw: str) -> str:
    """The uploader's own filename, reduced to something safe to show back.

    The name is never joined onto a path - the bytes go to a content-addressed
    blob and extraction uses its own generated directory - so this is not a
    traversal defence. It is a display one. A browser sends whatever the
    person chose, and `../../etc/passwd` rendered verbatim - in an
    administrator's queue row, or in an append-only history line that can
    never afterwards be edited or deleted - reads as though the server had
    put a path there, which is a lie about what was received. So only the
    last segment survives, of either separator, because a Windows uploader
    sends backslashes and a POSIX one sends slashes.

    Control characters go for the same reason: a newline breaks the row (or
    the line), and a name is a label rather than a document.

    Applied once, here in `stage()`, so every road a filename can travel -
    the console's own upload, the portal queue's submission, a re-stage for
    approval - carries the same cleaned value from the moment it enters the
    system, rather than each caller remembering to clean it again.
    """
    name = raw.replace("\\", "/")
    name = PurePosixPath(name).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if len(name) > _MAX_FILENAME:
        name = name[:_MAX_FILENAME]
    return name or _FALLBACK_FILENAME


class UploadRefused(Exception):
    """The archive cannot be accepted; the message says why in one sentence an
    administrator can act on."""


class UploadTooLarge(UploadRefused):
    """Over one of the size or count limits."""


class UnsafeArchive(UploadRefused):
    """A member path would write outside the extraction directory, or the file
    is not a readable zip at all."""


class UploadNotFound(Exception):
    """No staged upload under that id for this tenant, or it has been applied
    or discarded already."""


@dataclass(frozen=True)
class UploadLimits:
    """Bounds on what one upload may carry.

    Nothing in the import path limited any of these, because a script run over
    a trusted checkout does not need it. An HTTP endpoint does: without a cap
    the extraction step is a way to fill the server's disk with one request.
    """
    max_archive_bytes: int = 35_000_000       # 35MB compressed
    max_total_bytes: int = 100_000_000        # 100MB extracted, so a zip bomb stops here
    max_file_bytes: int = 10_000_000          # 10MB per file
    max_files: int = 2_000
    # A preview nobody applied or discarded within this long is swept, so an
    # abandoned upload costs disk for a day rather than for ever.
    max_staged_seconds: int = 86_400


@dataclass
class SkillDiff:
    """What applying this upload would do to ONE skill."""
    skill_id: str
    name: str
    is_new: bool
    rules_created: list[str] = field(default_factory=list)
    rules_corroborated: list[str] = field(default_factory=list)
    sections_changed: list[tuple[str, str]] = field(default_factory=list)   # heading, new body
    sections_added: list[str] = field(default_factory=list)
    artefacts: list[tuple[str, int]] = field(default_factory=list)          # path, bytes
    # Fields a human has claimed, which import will leave alone. Reported so a
    # reviewer is not told the description will change when it will not.
    kept_curated: list[str] = field(default_factory=list)
    source_ref: str = ""
    domain: str = "general"


@dataclass
class StagedUpload:
    id: str
    tenant_id: str
    root: Path
    skills: list[SkillDiff]
    warnings: list[str]
    # The archive's own name, as the uploader's browser sent it (or the
    # portal record's filename, on a re-stage for approval). Carried through
    # so a caller that needs to say what was applied - the console's own
    # stage-then-apply, in particular - can name the file rather than an
    # internal id that stops resolving to anything the moment `apply`
    # discards this object.
    filename: str = "upload.zip"
    identities: dict[str, str] = field(default_factory=dict)
    source_packages: tuple[LocalPackage, ...] = ()
    # Missing metadata belongs to a pre-purpose manifest and must be re-previewed.
    purpose: Literal["direct", "queue"] | None = None
    queue_upload_id: str | None = None


def _reject_unsafe_members(archive: zipfile.ZipFile, limits: UploadLimits) -> None:
    """Refuse the whole archive before a single byte is written.

    Checked here rather than during extraction because a partial write that is
    then rolled back is a rollback that can fail. The path rule is the classic
    one: a member named `../../etc/passwd` or `/etc/passwd` escapes the
    directory it is extracted into.
    """
    members = archive.infolist()
    if len(members) > limits.max_files:
        raise UploadTooLarge(
            f"the archive holds {len(members)} files; the limit is {limits.max_files}")
    total = 0
    paths = set()
    for member in members:
        if member.is_dir():
            continue
        name = member.filename
        key = unicodedata.normalize("NFC", name).casefold()
        if key in paths or "\\" in name or "\x00" in name:
            raise UnsafeArchive("the archive contains colliding or unsupported paths")
        paths.add(key)
        if name.startswith("/") or Path(name).is_absolute():
            raise UnsafeArchive(f"{name!r} is an absolute path")
        if ".." in Path(name).parts:
            raise UnsafeArchive(f"{name!r} points outside the archive")
        # High 16 bits of external_attr are the Unix mode. 0xA000 is a symlink,
        # which zipfile happily extracts as a file containing a path - and on a
        # host that later follows it, that is a read of whatever it names.
        if (member.external_attr >> 16) & 0xF000 == 0xA000:
            raise UnsafeArchive(f"{name!r} is a symbolic link")
        if stat.S_IFMT(member.external_attr >> 16) not in {0, stat.S_IFREG}:
            raise UnsafeArchive(f"{name!r} is not a regular file")
        if member.file_size > limits.max_file_bytes:
            raise UploadTooLarge(
                f"{name!r} is {member.file_size // 1024}KB; the per-file limit is "
                f"{limits.max_file_bytes // 1024}KB")
        total += member.file_size
        if total > limits.max_total_bytes:
            raise UploadTooLarge(
                "the archive expands to more than "
                f"{limits.max_total_bytes // 1_000_000}MB")


class UploadService:
    def __init__(self, *, store: GraphStore, importer: SkillImporter,
                 staging_root: Path, sanitiser=None,
                 limits: UploadLimits | None = None, source_service=None) -> None:
        self._store = store
        self._importer = importer
        self._staging_root = Path(staging_root)
        self._sanitiser = sanitiser
        self._limits = limits or UploadLimits()
        self._staged: dict[str, StagedUpload] = {}
        self._states: dict[str, ImportState] = {}
        self._source_service = source_service

    def configure_sources(self, service) -> None:
        self._source_service = service

    # -- intake ---------------------------------------------------------------

    def stage(self, archive: bytes, tenant_id: str,
             filename: str = "upload.zip", *, purpose: Literal["direct", "queue"] = "direct",
             queue_upload_id: str | None = None) -> StagedUpload:
        """Extract, parse and diff. Writes nothing to the graph.

        `filename` is the archive's own name - the multipart upload's
        filename, or the portal record's stored filename on a re-stage - kept
        only so a later caller can say what was applied. Optional and
        defaulted, so every existing caller that never had a name to give
        keeps working unchanged. Cleaned through `clean_filename` before it is
        stored, so every caller gets the same safe-to-display value back,
        whatever it sent.
        """
        if purpose not in {"direct", "queue"} or (purpose == "direct" and queue_upload_id is not None):
            raise UploadRefused("Invalid upload intake purpose")
        self._sweep_abandoned()
        filename = clean_filename(filename)
        if len(archive) > self._limits.max_archive_bytes:
            raise UploadTooLarge(
                f"the upload is {len(archive) // 1_000_000}MB; the limit is "
                f"{self._limits.max_archive_bytes // 1_000_000}MB")
        try:
            zf = zipfile.ZipFile(BytesIO(archive))
        except zipfile.BadZipFile as exc:
            raise UnsafeArchive("that file is not a readable zip archive") from exc
        with zf:
            _reject_unsafe_members(zf, self._limits)
            upload_id = f"upload-{uuid.uuid4().hex}"
            if purpose == "queue" and queue_upload_id is None:
                queue_upload_id = upload_id
            root = self._staging_root / upload_id
            root.mkdir(parents=True, exist_ok=True)
            try:
                zf.extractall(root)
                if os.name != "nt":
                    for member in zf.infolist():
                        if not member.is_dir():
                            (root / member.filename).chmod(0o755 if (member.external_attr >> 16) & 0o111 else 0o644)
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise

        if self._source_service is not None:
            from oms.sources.local_upload import stage_source
            try:
                return stage_source(self, root, upload_id, tenant_id, filename,
                                    purpose=purpose, queue_upload_id=queue_upload_id)
            except Exception as exc:
                shutil.rmtree(root, ignore_errors=True)
                (self._staging_root / (upload_id + ".json")).unlink(missing_ok=True)
                self._staged.pop(upload_id, None)
                self._states.pop(upload_id, None)
                if isinstance(exc, (SourceConflict, ValueError)):
                    raise UploadRefused(exc.code if isinstance(exc, SourceConflict) else "invalid source package") from exc
                raise

        state = self._importer.capture_state(tenant_id)
        try:
            parsed = self._importer.prepare_directory(root, tenant_id)
        except (ReservedSkillId, ImportIdentityConflict) as exc:
            # An id inside the reserved `repo-` namespace is something the
            # person who built the archive can fix, so it is a refusal in the
            # same shape as every other one here - not an unhandled exception.
            # `parse_directory` ran with no handler at all before this, so the
            # parser's error flew past both routers' `except UploadRefused` and
            # reached the uploader as a 500, with the cleanup below on a branch
            # that was never taken: the extracted tree stayed on disk, and any
            # person holding SKILL_UPLOAD could repeat it.
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRefused(str(exc)) from exc
        if not parsed.skills:
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRefused(
                "no skills found. The archive should hold skills/<name>/SKILL.md, "
                "each with a `name` in its frontmatter.")

        warnings: list[str] = []
        diffs = [self._diff(skill, tenant_id, warnings) for skill in parsed.skills]
        staged = StagedUpload(id=upload_id, tenant_id=tenant_id, root=root,
                              skills=diffs, warnings=warnings, filename=filename,
                              purpose=purpose, queue_upload_id=queue_upload_id,
                              identities={skill.source_path or skill.source_ref: skill.id for skill in parsed.skills})
        self._staged[upload_id] = staged
        self._states[upload_id] = state
        return staged

    def _sweep_abandoned(self) -> None:
        """Remove previews older than the age limit, whoever staged them.

        A preview is abandoned when nobody applies or discards it, and a
        browser that closes leaves exactly that. An upload is judged by its
        newest entry, and its manifest goes before its tree, so a half-swept
        upload reads as gone rather than as a manifest naming no files.
        """
        cutoff = time.time() - self._limits.max_staged_seconds
        groups: dict[str, list[Path]] = {}
        try:
            entries = list(self._staging_root.iterdir())
        except FileNotFoundError:
            return
        for entry in entries:
            match = _STAGED_ENTRY.fullmatch(entry.name)
            if match is not None:
                groups.setdefault(match.group(1) or entry.name, []).append(entry)
        for upload_id, paths in groups.items():
            try:
                if max(path.lstat().st_mtime for path in paths) > cutoff:
                    continue
            except FileNotFoundError:
                continue
            self._staged.pop(upload_id, None)
            self._states.pop(upload_id, None)
            for path in sorted(paths, key=lambda path: path.is_dir() and not path.is_symlink()):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)

    # -- the dry run ----------------------------------------------------------

    def _diff(self, skill: ParsedSkill, tenant_id: str,
              warnings: list[str]) -> SkillDiff:
        """What this parsed skill would do to the graph, computed by reading.

        The rule key is `oms.domain.ids.normalise` and the polarity, which is
        exactly what `MergeStep` keys on. Sharing the function rather than
        restating the rule is what keeps the preview from disagreeing with the
        apply.
        """
        existing_skill = self._store.get_skill(skill.id, tenant_id=tenant_id)
        in_tenant = existing_skill is not None and existing_skill.tenant_id == tenant_id
        diff = SkillDiff(skill_id=skill.id, name=skill.name, is_new=not in_tenant,
                         source_ref=skill.source_ref)
        if in_tenant:
            diff.kept_curated = sorted(existing_skill.curated)

        known = {(_normalise(r.body), r.polarity)
                 for r in (self._store.rules_for_skill(skill.id, tenant_id=tenant_id) if in_tenant else [])}
        for rule in skill.rules:
            if not rule.body.strip():
                continue
            key = (_normalise(rule.body), rule.polarity)
            if key in known:
                diff.rules_corroborated.append(rule.body)
            else:
                diff.rules_created.append(rule.body)
                known.add(key)

        sections = {s.heading: s for s in
                    (self._store.sections_for_skill(skill.id, tenant_id=tenant_id) if in_tenant else [])}
        for parsed_section in skill.sections:
            if not parsed_section.body.strip():
                continue
            section = sections.get(parsed_section.heading)
            if section is None:
                diff.sections_added.append(parsed_section.heading)
                continue
            active = self._store.blocks_for_section(section.id, tenant_id=tenant_id)
            current = active[0].body if active else ""
            if current != parsed_section.body:
                diff.sections_changed.append((parsed_section.heading, parsed_section.body))

        for artefact in skill.artefacts:
            diff.artefacts.append((artefact.path, len(artefact.body)))
            self._check_artefact(skill, artefact, warnings)
        return diff

    def _check_artefact(self, skill: ParsedSkill, artefact, warnings: list[str]) -> None:
        """Run a text artefact past the sanitiser and report what it would take.

        The import path never did this. §4.4 says imported text passes through
        the sanitiser before persistence, and that was true of the rules and
        not of the files beside them - fine when an operator ran a script over
        a repository they owned, and not fine for an upload form.

        Reported, not applied. Redacting a script's contents on the way past
        would corrupt the file; telling the reviewer their fixture carries what
        looks like an email address lets them decide.
        """
        if self._sanitiser is None:
            return
        try:
            text = artefact.body.decode("utf-8")
        except UnicodeDecodeError:
            return       # binary: nothing to read, nothing to say
        result = self._sanitiser.sanitise(ExecutionContext(
            user_input=f"upload: {skill.id}/{artefact.path}",
            agent_raw_output="", user_correction=text))
        # `.context.user_correction`, read directly. This was a `getattr` with
        # `text` as its fallback, which after the sanitiser's return type
        # changed would have compared the text to itself and reported every
        # upload as clean - a silent loss of the whole warning, with nothing
        # failing anywhere to say so.
        redacted = result.context.user_correction or text
        if redacted != text:
            warnings.append(
                f"{skill.id}/{artefact.path} looks like it contains personal data. "
                "It will be stored as it is; check before applying.")

    # -- apply ----------------------------------------------------------------

    def get(self, upload_id: str, tenant_id: str) -> StagedUpload:
        staged = self._staged.get(upload_id)
        if staged is None and self._source_service is not None:
            from oms.sources.local_upload import restore_stage
            staged = restore_stage(self, upload_id, tenant_id)
        if staged is None or staged.tenant_id != tenant_id:
            raise UploadNotFound(f"no staged upload {upload_id!r}")
        self._touch(staged)
        return staged

    def _touch(self, staged: StagedUpload) -> None:
        """Mark a preview in use, so the sweep judges it from its last read.

        A queue reviewer can hold a preview open for longer than the age
        limit; which previews a pending queue item still names is not known
        here, so every read counts as use.
        """
        now = time.time()
        for path in (self._staging_root / (staged.id + ".json"), staged.root):
            try:
                os.utime(path, (now, now), follow_symlinks=False)
            except FileNotFoundError:
                pass

    def apply(self, upload_id: str, tenant_id: str) -> ImportReport:
        """Run the real importer over the staged tree, then discard it.

        Discarded either way. A second apply of the same id is refused rather
        than being an idempotent no-op, because the graph may have moved
        underneath it and the diff the administrator approved would no longer
        be the diff they get.
        """
        if self._source_service is not None:
            raise UploadRefused("Choose the local stream explicitly before applying this package")
        self.get(upload_id, tenant_id)
        try:
            return self._importer.apply_prepared(self.prepare_apply(upload_id, tenant_id))
        except (ImportIdentityConflict, SourceConflict, StaleMutation) as exc:
            raise UploadRefused(str(exc)) from exc
        finally:
            self.discard(upload_id, tenant_id)

    def prepare_apply(self, upload_id: str, tenant_id: str) -> PreparedImport:
        """Run checks outside the caller's transaction and retain the preview fence."""
        if self._source_service is not None:
            raise UploadRefused("Source reconciliation is required for this package")
        staged = self.get(upload_id, tenant_id)
        return self._importer.prepare_import_directory(staged.root, tenant_id,
            filename=staged.filename, expected_identities=staged.identities,
            state=self._states[upload_id])

    def discard(self, upload_id: str, tenant_id: str) -> None:
        """Remove a staged upload and its files.

        An unknown id raises rather than passing quietly. A DELETE could
        defensibly be idempotent, but this id came from a response minutes ago:
        "that upload is gone" is information, and swallowing it would let a
        second browser tab believe it had discarded something it had not.
        """
        self.get(upload_id, tenant_id)
        staged = self._staged.pop(upload_id, None)
        if staged is None:
            raise UploadNotFound(f"no staged upload {upload_id!r}")
        if staged.tenant_id != tenant_id:
            # Put it back: this caller has no business removing it.
            self._staged[upload_id] = staged
            raise UploadNotFound(f"no staged upload {upload_id!r}")
        self._states.pop(upload_id, None)
        if self._source_service is not None:
            (self._staging_root / (upload_id + ".json")).unlink(missing_ok=True)
        shutil.rmtree(staged.root, ignore_errors=True)


def _skills_root(root: Path) -> Path:
    """Compatibility entry point: package directories now carry identity.

    Do not strip a lone wrapper: importing A alone must identify it exactly
    as importing A beside B. The parser already ignores operating-system debris.
    """
    return root
