"""Tier 2 skill projections using explicit read and listing policy.

A listing may narrow the readable set. Direct resource lookup always uses
read permission, and every returned body comes from the shared publisher.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from oms.domain.models import Skill, UsageEvent
from oms.domain.repo import is_repo_skill
from oms.domain.types import SourceRuntime
from oms.ports.blob_store import BlobStore
from oms.ports.repositories import CatalogueRepository
from oms.publish.publisher import Publisher
from oms.publish.render import (
    REFERENCES_FILE, RenderedSkill, rewrite_resource_links, version_of,
)
from oms.ports.catalogue_policy import (
    CatalogueReadPolicy, CatalogueSelection, SingleWorkspaceReadPolicy,
)


# Inline images only, and only these. MCP's `ImageContent` is what a vision
# model actually consumes, so a diagram beside a skill is worth the bytes; a
# spreadsheet or an archive is not, because base64 costs a third more than the
# file, lands in the context window, and gives the model nothing it can act on.
# Sniffed by extension rather than by magic bytes: the name is what the skill's
# own text points at, and a file whose contents disagree with its extension is
# a problem for whoever packaged it, not a case to guess at here.
_IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

# Only used to LABEL text that is served as text; nothing branches on it.
_TEXT_TYPES = {".md": "text/markdown", ".txt": "text/plain",
               ".json": "application/json", ".yaml": "text/yaml",
               ".yml": "text/yaml", ".py": "text/x-python",
               ".sh": "text/x-shellscript", ".csv": "text/csv"}

# One megabyte. Chosen against what it protects rather than against any image:
# base64 inflates by a third, and a tool result is injected into the model's
# context verbatim, so a cap in this range is the difference between a large
# diagram and a conversation that cannot continue.
MAX_INLINE_RESOURCE_BYTES = 1_000_000


def _extension(resource: str) -> str:
    _head, dot, tail = resource.rpartition(".")
    return f".{tail.lower()}" if dot else ""


def _image_media_type(resource: str) -> str | None:
    return _IMAGE_TYPES.get(_extension(resource))


def _text_media_type(resource: str) -> str:
    return _TEXT_TYPES.get(_extension(resource), "text/plain")


def version_of_bytes(blob: bytes) -> str:
    """A binary resource's version: the same short sha256 digest `version_of`
    takes of a text body, over the bytes actually served."""
    import hashlib

    return hashlib.sha256(blob).hexdigest()[:12]


class SkillNotFound(Exception):
    """No skill of that name is published to the caller's tenant."""


class ResourceNotFound(Exception):
    """The skill exists but carries no resource under that name."""


class CatalogueBlocked(Exception):
    """The tenant's `block_publish` flag is raised, so nothing is served.

    Safety is decided upstream (compile-step gate / periodic check) and both
    distribution tiers obey the same flag: Tier 1 stops republishing, Tier 2
    stops answering. Without this the read tools would be a live back door
    around the gate. Callers degrade to their local cache (§9.2)."""


@dataclass(frozen=True)
class SkillListing:
    """One row of the catalogue: what a host needs to decide whether to fetch."""
    id: str
    name: str
    description: str
    domain: str
    version: str


@dataclass(frozen=True)
class SkillDocument:
    id: str
    name: str
    version: str
    body: str
    resources: list[str]      # Level 3 resources fetchable with query_skill_resource


@dataclass(frozen=True)
class SkillResource:
    """A Level 3 file served over MCP.

    Exactly one of `body` and `blob` is set. Text keeps `body`, which is what
    every caller before images existed already reads, so nothing that works
    today changes shape. An image sets `blob` and `media_type`, and the
    transport turns it into an `ImageContent` block rather than a string,
    because base64 pasted into a text block is something the model can neither
    see nor use.
    """
    skill_id: str
    resource: str
    version: str
    body: str | None = None
    blob: bytes | None = None
    media_type: str | None = None


class SkillCatalogue:
    """The read tools' service layer. Tenant comes from the authenticated
    principal at the transport edge, never from tool arguments."""

    def __init__(self, store: CatalogueRepository, publisher: Publisher,
                 blob_store: BlobStore | None = None,
                 max_inline_bytes: int = MAX_INLINE_RESOURCE_BYTES,
                 read_policy: CatalogueReadPolicy | None = None) -> None:
        self._store = store
        self._publisher = publisher
        self._blob_store = blob_store
        self._max_inline_bytes = max_inline_bytes
        self._read_policy = (read_policy if read_policy is not None
                             else SingleWorkspaceReadPolicy())

    def list_skills(self, tenant_id: str, domain_hint: str | None = None, *,
                    person_id: str | None = None) -> list[SkillListing]:
        """List the selected readable skills in name order, with rendered versions."""
        selection = self._read_policy.for_reader(tenant_id, person_id)
        if not selection.allowed:
            return []
        self._assert_serving(tenant_id)
        listings: list[SkillListing] = []
        for skill in self._store.skills_for_tenant(tenant_id):
            if is_repo_skill(skill.id):
                # NOT covered by the publisher's exclusion: this loop reads
                # `skills_for_tenant` directly. This is the MCP `list_skills`
                # tool, so without this line every repo bundle in the tenant is
                # announced to every agent on every machine - one row per repo,
                # growing without bound, which is the exact noise repo scoping
                # exists to remove.
                #
                # Before the scope and mute filters rather than after, because
                # this is not a preference or a permission: a bundle is not a
                # member of this list at all.
                continue
            if not selection.can_read(skill):
                continue
            if not selection.can_list(skill):
                continue
            body, _resources = self._fetch_view(skill)
            listing = SkillListing(
                id=skill.id, name=skill.name, description=skill.description,
                domain=skill.domain, version=version_of(body),
            )
            if _matches(listing, domain_hint):
                listings.append(listing)
        listings.sort(key=lambda s: s.name)
        return listings

    def query_skill(self, tenant_id: str, name: str, *, principal_id: str | None = None,
                    person_id: str | None = None,
                    source_runtime: SourceRuntime = SourceRuntime.MCP) -> SkillDocument:
        """Fetch one readable skill, regardless of whether it is in the listing."""
        selection = self._read_policy.for_reader(tenant_id, person_id)
        if not selection.allowed:
            raise SkillNotFound(f"no skill named {name!r} for tenant {tenant_id!r}")
        self._assert_serving(tenant_id)
        skill = self._resolve(tenant_id, name, selection)
        body, resources = self._fetch_view(skill)
        self._record(skill, tenant_id, principal_id, source_runtime, resource=None)
        return SkillDocument(
            id=skill.id, name=skill.name, version=version_of(body),
            body=body, resources=resources,
        )

    def query_skill_resource(self, tenant_id: str, name: str, resource: str, *,
                             principal_id: str | None = None,
                             person_id: str | None = None,
                             source_runtime: SourceRuntime = SourceRuntime.MCP) -> SkillResource:
        """Fetch a resource owned by a readable skill with the shared custody limits."""
        selection = self._read_policy.for_reader(tenant_id, person_id)
        if not selection.allowed:
            raise SkillNotFound(f"no skill named {name!r} for tenant {tenant_id!r}")
        self._assert_serving(tenant_id)
        skill = self._resolve(tenant_id, name, selection)
        package = self._publisher.render_package(skill)
        resources = self._publisher.resource_names(
            skill.id, package.references_md is not None)
        if resource not in resources:
            raise ResourceNotFound(
                f"skill {skill.id!r} has no resource {resource!r}")

        media_type = _image_media_type(resource)
        if media_type is not None:
            blob = self._resource_bytes(skill, resource)
            if blob is None:
                raise ResourceNotFound(
                    f"skill {skill.id!r} has no resource {resource!r}")
            if len(blob) > self._max_inline_bytes:
                raise ResourceNotFound(
                    f"resource {resource!r} is {len(blob) // 1024}KB, too large to "
                    f"serve inline (the limit is {self._max_inline_bytes // 1024}KB). "
                    "It is in the published file tree.")
            self._record(skill, tenant_id, principal_id, source_runtime, resource=resource)
            return SkillResource(
                skill_id=skill.id, resource=resource,
                # The bytes served, hashed the same way a text body is. The
                # artefact's own content_ref is the same digest, so a Tier 2
                # cache keyed on (name, version) invalidates exactly when the
                # file changes.
                version=version_of_bytes(blob), blob=blob, media_type=media_type)

        body = self._resource_body(skill, package, resource)
        if body is None:
            raise ResourceNotFound(
                f"skill {skill.id!r} has no resource {resource!r}")
        # A reference can point at a sibling reference, which is the same
        # unreadable relative path one level down; rewrite those too.
        body = rewrite_resource_links(body, skill.id, resources)
        self._record(skill, tenant_id, principal_id, source_runtime, resource=resource)
        return SkillResource(skill_id=skill.id, resource=resource,
                             version=version_of(body), body=body,
                             media_type=_text_media_type(resource))

    def _resource_bytes(self, skill: Skill, resource: str) -> bytes | None:
        """The raw bytes behind an artefact this skill owns, undecoded."""
        for artefact, path in self._store.artefacts_for_skill(skill.id):
            if path != resource or self._blob_store is None:
                continue
            try:
                return self._blob_store.get(artefact.content_ref)
            except Exception:
                return None      # missing blob: reported by fsck, not here
        return None

    def _assert_serving(self, tenant_id: str) -> None:
        block = self._store.get_publish_block(tenant_id)
        if block is None:
            return
        detail = "; ".join(block.reasons) if block.reasons else "no reason recorded"
        raise CatalogueBlocked(
            f"skill delivery is blocked by the {block.source} check: {detail}")

    def _resolve(self, tenant_id: str, name: str,
                 selection: CatalogueSelection) -> Skill:
        """Resolve a readable skill by id or display name without disclosing withheld content."""
        wanted = _key(name)
        for skill in self._store.skills_for_tenant(tenant_id):
            if _key(skill.id) != wanted and _key(skill.name) != wanted:
                continue
            if not selection.can_read(skill):
                # `continue`, not `break`. Matching is on id OR display name and
                # only id is unique, so two skills can normalise to the same
                # key. Stopping at the first out-of-scope match would hide an
                # in-scope skill the caller is entitled to, and
                # `skills_for_tenant` has no ORDER BY, so it would do so
                # non-deterministically. Serving the in-scope match leaks
                # nothing, and a miss still says nothing.
                continue
            return skill
        raise SkillNotFound(f"no skill named {name!r} for tenant {tenant_id!r}")

    def _fetch_view(self, skill: Skill) -> tuple[str, list[str]]:
        """Render the skill as Tier 2 serves it: body plus its resource names.
        One place, so a listing's version always covers the body a query
        returns."""
        return self._publisher.fetch_view(skill, self._publisher.render_package(skill))

    def _resource_body(self, skill: Skill, package: RenderedSkill,
                       resource: str) -> str | None:
        """The bytes behind a resource this skill is already known to own: the
        generated overflow file, or an artefact out of the blob store."""
        if resource == REFERENCES_FILE:
            return package.references_md
        for artefact, path in self._store.artefacts_for_skill(skill.id):
            if path != resource:
                continue
            if self._blob_store is None:
                return None
            try:
                return self._blob_store.get(artefact.content_ref).decode("utf-8")
            except UnicodeDecodeError as exc:
                # Images never reach here - `query_skill_resource` routes them
                # to the byte path first. What is left is a spreadsheet, an
                # archive or a binary fixture: inlining one costs a third more
                # than the file in tokens and gives the model nothing it can
                # act on. The refusal names where it does exist.
                raise ResourceNotFound(
                    f"resource {resource!r} is not text and cannot be served over "
                    "MCP. It is in the published file tree.") from exc
            except Exception:
                return None      # missing blob: reported by fsck, not here
        return None

    def _record(self, skill: Skill, tenant_id: str, principal_id: str | None,
                source_runtime: SourceRuntime, resource: str | None) -> None:
        self._store.record_usage_event(UsageEvent(
            id=f"usage-{uuid.uuid4().hex}",
            skill_id=skill.id, tenant_id=tenant_id,
            timestamp=datetime.now(timezone.utc),
            source_runtime=source_runtime,
            principal_id=principal_id, resource=resource,
        ))


def _key(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _matches(listing: SkillListing, hint: str | None) -> bool:
    if not hint:
        return True
    needle = hint.strip().lower()
    haystack = " ".join([listing.domain, listing.name, listing.id, listing.description]).lower()
    return needle in haystack
