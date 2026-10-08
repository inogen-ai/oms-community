"""What an administrator may change about a skill, and how it is recorded.

Two surfaces, one service, because they are the same decision from a
non-technical person's point of view: this skill says the wrong thing, let me
fix it. What they can reach is deliberately narrow.

**Labels** - name, description, area, tags - are what the folder is called.
Editing one marks it `curated`, so the next import leaves it alone instead of
reverting it (`SkillImporter._import_skill`). The description is not cosmetic:
publish uses a skill's own description over the derived one, and it is the
"when the task matches" column of the Tier 2 dispatch table, so editing it
changes which tasks agents fetch the skill for. That is why every change is an
`AdminEvent`.

**Section prose** is the skill's own words, revised through block supersession,
exactly as an approved `block_revision` does it: a new content-addressed block
becomes active, the old one stays attached as history, and `SUPERSEDES` links
them. Only `authorial_passthrough` sections are offered. A `system_aggregated`
section is rendered FROM the rules at publish time, so an edit box over one
would be reverted at the next publish with no error - the failure this refuses
by construction rather than by warning.

Rule amendments must retain their contribution provenance and record the edit.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import copy
from functools import wraps
from inspect import signature
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from oms.activity.models import AdminAction, AdminEvent
from oms.domain.ids import block_id, slug as _skill_slug
from oms.domain.models import ContentBlock, Edge, Rule, Skill
from oms.domain.repo import REPO_ID_PREFIX, reserved_id_refusal
from oms.domain.types import (
    EdgeType, Mutability, Plane, SectionKind, SkillOrigin, SkillVersionCause,
)
from oms.ports.activity import AdminEventStore
from oms.ports.graph_store import GraphStore
from oms.ports.workflow import WorkflowRepository
from oms.skills.history import SkillHistory


class SkillNotFound(Exception):
    """No such skill in this tenant."""


class SkillExists(Exception):
    """A skill with this id already exists; creating over it would silently
    adopt its rules, sections and history."""


class InvalidSkillEdit(Exception):
    """The edit cannot be applied as asked; the message says why, in words an
    administrator can act on."""


@dataclass(frozen=True)
class DeletionReport:
    """What deleting a skill left behind, said at the moment of deletion.

    `rules_detached` is the honest half: `delete_skill` removes the skill node
    and its edges, so the rules that belonged to it survive, anchored to
    nothing, and publish nowhere. Reporting the count is what lets the
    confirmation screen say that before the click instead of after."""
    skill_id: str
    name: str
    rules_detached: int


@dataclass(frozen=True)
class SectionView:
    """One section of a skill as the editor sees it.

    `editable` is the whole point of the type. A reader has to be able to tell
    their own words from generated text BEFORE they type into it, so the flag
    travels with the section rather than being re-derived in the frontend from
    a mutability enum it would have to learn.
    """
    id: str
    heading: str
    kind: str
    editable: bool
    body: str | None


# Where a block written from this editor says it came from. The importer's
# two-writer policy escalates when the active block's `source_ref` does not
# start with the source file's path (`SkillImporter._import_sections`), so this
# prefix is what makes a later re-import of an edited section ask a human
# instead of overwriting the edit silently.
_EDITOR_SOURCE = "skill-editor"


def _guarded_write(method):
    @wraps(method)
    def write(self, *args, **kwargs):
        if self._repository is None:
            return method(self, *args, **kwargs)
        values = signature(method).bind(self, *args, **kwargs).arguments
        tenant_id = values["tenant_id"]
        participants = ()
        if self._admin_events is not None and not hasattr(self._admin_events, "_driver"):
            if not all(callable(getattr(self._admin_events, name, None))
                       for name in ("snapshot_state", "restore_state")):
                raise ValueError("Guarded audit storage must support rollback")
            participants = (self._admin_events,)

        def apply(store, queue):
            bound = self.using(store, queue)
            result = method(bound, *args, **kwargs)
            if self._history_factory is not None and method.__name__ != "delete_skill":
                history = self._history_factory(store)
                skill_id = values.get("skill_id", getattr(result, "id", None))
                affected = {skill_id} if skill_id is not None else set()
                if "rule_id" in values:
                    affected.update(skill.id for skill in store.skills_for_rule(
                        values["rule_id"], tenant_id=tenant_id))
                cause = (SkillVersionCause.CREATED if method.__name__ == "create_skill"
                         else SkillVersionCause.RULE_EDIT if method.__name__ == "edit_rule"
                         else SkillVersionCause.CONSOLE_EDIT)
                for identifier in sorted(affected):
                    history.capture_required(identifier, tenant_id, cause=cause, actor=values.get("actor"))
            return result

        return self._repository.atomic(f"skill-{method.__name__}", tenant_id, apply,
                                       rollback_participants=participants)
    return write


class SkillAdminService:
    def __init__(self, *, store: GraphStore,
                 admin_events: AdminEventStore | None = None,
                 event_actions=AdminAction,
                 repository: WorkflowRepository | None = None,
                 history_factory: Callable[[GraphStore], SkillHistory] | None = None) -> None:
        self._store = store
        self._admin_events = admin_events
        self._event_actions = event_actions
        self._repository = repository
        self._history_factory = history_factory
        self._queue = None
        if repository is not None and hasattr(repository, "_mutex"):
            bind_lock = getattr(admin_events, "bind_workflow_lock", None)
            if callable(bind_lock):
                bind_lock(repository._mutex)

    def using(self, store, queue):
        """Bind this edition's editor to an existing transaction without nesting it."""
        bound = copy(self)
        bound._store, bound._queue, bound._repository = store, queue, None
        if self._admin_events is not None and hasattr(self._admin_events, "_driver"):
            driver = getattr(store, "_driver", None)
            if driver is None:
                raise ValueError("Audit storage requires the bound graph transaction")
            bound._admin_events = copy(self._admin_events)
            bound._admin_events._driver = driver
        return bound

    def skill(self, skill_id: str, tenant_id: str) -> Skill:
        """The skill, or `SkillNotFound`. The tenant check lives here so no
        caller has to remember it."""
        return self._require(skill_id, tenant_id)

    # -- lifecycle ------------------------------------------------------------

    @_guarded_write
    def create_skill(self, tenant_id: str, *, name: str, description: str,
                     domain: str, actor: str | None = None) -> Skill:
        """Create an empty skill from the console.

        Empty deliberately: rules arrive through corrections and prose through
        the section editor or a package upload, both of which record custody.
        What this creates is the folder - a name, a description agents dispatch
        on, and an area - so guidance has somewhere to accumulate without a zip
        or a shell.

        The id is a slug of the name, as the importer derives it, and a
        collision within the admitted tenant is refused rather than adopted,
        preserving the existing skill's rules and history.

        Every label is marked curated from birth. All three were typed by a
        human, and a later package upload for the same id must not revert
        them, which is exactly the promise `update_metadata` makes for edits.
        """
        cleaned_name = name.strip()
        cleaned_domain = domain.strip()
        if not cleaned_name:
            raise InvalidSkillEdit("a skill needs a name")
        if not cleaned_domain:
            raise InvalidSkillEdit("a skill needs an area")
        skill_id = _skill_slug(cleaned_name)
        if not skill_id:
            raise InvalidSkillEdit("a skill needs a usable name")
        if skill_id.startswith(REPO_ID_PREFIX):
            # `InvalidSkillEdit`, not a bare `ValueError`. `skills_api` catches
            # `SkillExists` and `InvalidSkillEdit`; a `ValueError` passed both
            # and reached the browser as a 500, so the sentence this guard
            # carefully writes was never shown to the person who has to act on
            # it.
            raise InvalidSkillEdit(reserved_id_refusal(skill_id))
        if self._store.get_skill(skill_id, tenant_id=tenant_id) is not None:
            raise SkillExists(f"a skill with id {skill_id!r} already exists")
        skill = Skill(id=skill_id, name=cleaned_name,
                      description=description.strip(), domain=cleaned_domain,
                      tenant_id=tenant_id,
                      curated=frozenset({"name", "description", "domain"}),
                      origin=SkillOrigin.AUTHORED)
        self._store.upsert_skill(skill)
        self._record_action(tenant_id, skill_id, actor,
                            action=self._event_actions.SKILL_CREATED,
                            before=None, after=cleaned_name)
        return skill

    @_guarded_write
    def delete_skill(self, skill_id: str, tenant_id: str, *,
                     actor: str | None = None) -> DeletionReport:
        """Remove a skill from the graph - the undo for a bad package apply.

        Deletion removes the skill node and its edges; its publication
        records go with it (a deleted skill must not short-circuit a future
        re-import as "unchanged"), and its rules survive unanchored. The
        report says how many, so the confirmation screen can put that in
        front of the administrator before the click.
        """
        skill = self._require(skill_id, tenant_id)
        rules = self._store.rules_for_skill(skill_id, tenant_id=tenant_id)
        from oms.domain.identity import SkillRef
        from oms.sources.mutation import source_repository
        sources = source_repository(self._store)
        ref = SkillRef(tenant_id, skill_id)
        if self._queue is None:
            if sources.get_binding(ref) or sources.get_local_stream(ref) or sources.open_update(ref):
                raise InvalidSkillEdit("Source-bound deletion requires the workflow review queue")
        else:
            for update in sources.retire_skill(ref):
                item = self._queue.get(update.review_item_id)
                if (item is not None and item.tenant_id == tenant_id and item.kind == "skill_update"
                        and item.subject_id == update.update_id and not item.resolved):
                    self._queue.resolve(item.id, "skill_deleted", decided_by=actor)
        self._store.delete_publications_for_skill(tenant_id, skill_id)
        self._store.delete_skill(skill_id, tenant_id=tenant_id)
        self._record_action(tenant_id, skill_id, actor,
                            action=self._event_actions.SKILL_DELETED,
                            before=skill.name, after=None)
        return DeletionReport(skill_id=skill_id, name=skill.name,
                              rules_detached=len(rules))

    # -- labels ---------------------------------------------------------------

    # Scalar fields an administrator may set. Tags are handled separately
    # because they are edges, not a property.
    _FIELDS = ("name", "description", "domain")

    @_guarded_write
    def update_metadata(self, skill_id: str, tenant_id: str, *,
                        name: str | None = None,
                        description: str | None = None,
                        domain: str | None = None,
                        tags: list[str] | None = None,
                        actor: str | None = None) -> Skill:
        """Set any of the labels. `None` means "not submitted", not "clear it".

        A submitted value equal to the current one changes nothing AND curates
        nothing: opening a form and pressing save must not quietly claim every
        field on the skill away from its source file for ever.
        """
        skill = self._require(skill_id, tenant_id)
        submitted = {"name": name, "description": description, "domain": domain}
        changes: list[tuple[str, str, str]] = []      # field, before, after

        for field, value in submitted.items():
            if value is None:
                continue
            cleaned = value.strip()
            if field in ("name", "domain") and not cleaned:
                raise InvalidSkillEdit(
                    f"a skill's {'name' if field == 'name' else 'area'} cannot be empty")
            if cleaned == getattr(skill, field):
                continue
            changes.append((field, getattr(skill, field), cleaned))
            setattr(skill, field, cleaned)

        if tags is not None:
            wanted = [t.strip() for t in tags if t.strip()]
            wanted = list(dict.fromkeys(wanted))
            current = self._store.tags_for_skill(skill_id, tenant_id=tenant_id)
            if wanted != current:
                self._replace_tags(skill_id, current, wanted, tenant_id=tenant_id)
                changes.append(("tags", ", ".join(current), ", ".join(wanted)))

        if not changes:
            return skill

        skill.curated = skill.curated | {field for field, _b, _a in changes}
        self._store.upsert_skill(skill)
        for field, before, after in changes:
            self._record(tenant_id, skill_id, actor,
                         before=f"{field}: {before}", after=f"{field}: {after}")
        return skill

    def _replace_tags(self, skill_id: str, current: list[str],
                      wanted: list[str], *, tenant_id: str) -> None:
        for tag in wanted:
            if tag in current:
                continue
            self._store.upsert_tag(tag, tag)
            self._store.attach_edge(Edge(type=EdgeType.TAGGED_WITH,
                                         from_id=skill_id, to_id=tag), tenant_id=tenant_id)
        for stale in current:
            if stale in wanted:
                continue
            # The Tag node itself stays: other skills and rules may point at
            # it, and nothing here can see whether they do.
            self._store.detach_edge(Edge(type=EdgeType.TAGGED_WITH,
                                         from_id=skill_id, to_id=stale), tenant_id=tenant_id)

    # -- prose ----------------------------------------------------------------

    def sections(self, skill_id: str, tenant_id: str) -> list[SectionView]:
        """Every section of the skill, in publish order, saying which are the
        skill's own words and which are generated."""
        self._require(skill_id, tenant_id)
        out: list[SectionView] = []
        for section in self._store.sections_for_skill(skill_id, tenant_id=tenant_id):
            editable = section.mutability is Mutability.AUTHORIAL_PASSTHROUGH
            body: str | None = None
            if editable:
                active = self._store.blocks_for_section(section.id, tenant_id=tenant_id)
                body = active[0].body if active else ""
            out.append(SectionView(
                id=section.id, heading=section.heading,
                kind=section.kind.value if isinstance(section.kind, SectionKind)
                else str(section.kind),
                editable=editable, body=body))
        return out

    @_guarded_write
    def revise_section(self, skill_id: str, section_id: str, tenant_id: str,
                       body: str, actor: str | None = None,
                       transaction_id: str | None = None) -> None:
        """Replace an authorial section's text.

        Same write as approving a `block_revision`: supersede the ACTIVE block
        with a content-addressed one. The old block stays attached, so the
        section's history survives and "why does this say this" keeps its
        answer.
        """
        self._require(skill_id, tenant_id)
        section = self._store.get_section(section_id, tenant_id=tenant_id)
        if section is None or section.tenant_id != tenant_id \
                or section.skill_id != skill_id:
            raise SkillNotFound(f"no section {section_id!r} on skill {skill_id!r}")
        if section.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise InvalidSkillEdit(
                "this section is generated from the skill's rules; editing it "
                "here would be undone at the next publish. Change the rules "
                "through review instead.")
        if not body or not body.strip():
            raise InvalidSkillEdit("the replacement text must not be empty")

        active = self._store.blocks_for_section(section_id, tenant_id=tenant_id)
        if not active:
            raise InvalidSkillEdit("this section has no text to replace yet")
        current = active[0]
        # Exact comparison, as `ReviewService.revise_prose` uses and for the
        # same reason: a person editing markdown may legitimately change only
        # whitespace, because list indentation and code fences are content.
        if current.body == body:
            raise InvalidSkillEdit("the text is unchanged")

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        new_block = ContentBlock(
            id=block_id(section_id, content_hash),
            content_ref=f"sha256-{content_hash}",
            kind=section.kind, tenant_id=tenant_id,
            source_ref=f"{_EDITOR_SOURCE}/{section_id}", body=body,
        )
        if new_block.id == current.id:
            # Unreachable given the body check unless the current block is
            # itself content-addressed on the same text; kept because a
            # self-referential SUPERSEDES edge is the one outcome here that
            # corrupts history rather than merely annoying.
            raise InvalidSkillEdit("the text is unchanged")
        self._store.supersede_block(current.id, new_block, section_id=section_id,
                                    transaction_id=transaction_id, tenant_id=tenant_id)
        self._record(tenant_id, skill_id, actor,
                     before=f"section {section.heading}: {current.body}",
                     after=f"section {section.heading}: {body}")

    @_guarded_write
    def rename_section(self, skill_id: str, section_id: str, tenant_id: str,
                       heading: str, actor: str | None = None) -> None:
        """Change a section's `## heading`.

        Allowed on every section, generated ones included, and that is the one
        thing worth stating. `revise_section` refuses a `SYSTEM_AGGREGATED`
        section because its BODY is rewritten from the rules at every publish,
        so an edit there would vanish. A heading is not rewritten:
        `render_skill_sectioned` emits `f"## {sec.heading}"` off the section
        row whatever the kind. Refusing here would have copied that rule from
        the wrong place and left an administrator unable to fix a heading they
        can plainly see is wrong.
        """
        self._require(skill_id, tenant_id)
        section = self._store.get_section(section_id, tenant_id=tenant_id)
        if section is None or section.tenant_id != tenant_id \
                or section.skill_id != skill_id:
            raise SkillNotFound(f"no section {section_id!r} on skill {skill_id!r}")
        cleaned = heading.strip()
        if not cleaned:
            raise InvalidSkillEdit("a section heading cannot be empty")
        if cleaned == section.heading:
            # Same rule `update_metadata` follows: opening a form and pressing
            # save writes nothing and records nothing.
            raise InvalidSkillEdit("the heading is unchanged")
        before = section.heading
        section.heading = cleaned
        self._store.upsert_section(section)
        self._record(tenant_id, skill_id, actor,
                     before=f"section heading: {before}",
                     after=f"section heading: {cleaned}")

    @_guarded_write
    def edit_rule(self, skill_id: str, rule_id: str, tenant_id: str,
                  body: str, actor: str | None = None) -> Rule:
        """Reword a rule in place.

        The one write in OMS that changes a rule without a review item, and it
        exists because an administrator reading the published document has to
        be able to fix a sentence in it. What makes that acceptable is the
        audit event below, not the rarity of the act.

        Three refusals and one deliberate omission:

          * a rule not anchored to this skill, because the skill in the path is
            what proves the administrator is editing something in front of
            them rather than any rule in the tenant by id;
          * a rule from another tenant, because `get_rule` is not tenant-scoped;
          * a control-plane rule, because `render.publishable` holds those out
            of every published file whatever their status, and editing one here
            would be a route around that decision.

        Corroboration is NOT reset. The rule is the same principle, reworded by
        somebody with authority, and the count is the evidence that the
        principle is real. Status is not touched either: retracting and
        restoring are their own verbs, and an edit must not become a back door
        to publishing a rule somebody retired.
        """
        self._require(skill_id, tenant_id)
        rule = self._store.get_rule(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            raise SkillNotFound(f"no rule {rule_id!r} for tenant {tenant_id!r}")
        if skill_id not in {s.id for s in self._store.skills_for_rule(rule_id, tenant_id=tenant_id)}:
            raise SkillNotFound(
                f"rule {rule_id!r} does not belong to skill {skill_id!r}")
        if rule.plane is not Plane.DATA:
            raise InvalidSkillEdit(
                "this is a control-plane rule. It describes how OMS itself "
                "operates and is never published, so editing it here would "
                "change nothing an agent reads.")
        cleaned = body.strip()
        if not cleaned:
            raise InvalidSkillEdit("a rule\'s text cannot be empty")
        if cleaned == rule.body:
            raise InvalidSkillEdit("the text is unchanged")

        before = rule.body
        rule.body = cleaned
        # Any stored embedding describes the old text and must be invalidated.
        # This service has no model dependency and does not recompute it.
        rule.embedding = None
        self._store.upsert_rule(rule)
        self._record_action(tenant_id, rule_id, actor,
                            action=self._event_actions.RULE_EDITED,
                            before=before, after=cleaned, subject_kind="rule")
        return rule

    @_guarded_write
    def revise_block(self, skill_id: str, block_id: str, tenant_id: str,
                     body: str, actor: str | None = None,
                     transaction_id: str | None = None) -> None:
        """Replace the text of one authorial block, addressed as the DOCUMENT
        addresses it.

        The editor knows a paragraph as `block:<block id>`, which is what the
        outline anchors it by and what `check_document` validates. The write
        underneath is `revise_section`, because a section holds exactly one
        active block and superseding it is what "edit this paragraph" means.

        Resolving the block to its section HERE rather than in the console is
        the point of the method. The console did it by sending the block id to
        the section route, which answered "unknown section" every time, so
        every paragraph edit passed the check and then failed to save. A client
        cannot be expected to know a mapping the server already has.
        """
        self._require(skill_id, tenant_id)
        block = self._store.get_content_block(block_id, tenant_id=tenant_id)
        section = self._store.section_for_block(block_id, tenant_id=tenant_id)
        if block is None or section is None or section.skill_id != skill_id \
                or section.tenant_id != tenant_id:
            raise SkillNotFound(
                f"no text {block_id!r} on skill {skill_id!r}")
        self.revise_section(skill_id, section.id, tenant_id, body, actor=actor,
                            transaction_id=transaction_id)

    def check_document(self, skill_id: str, tenant_id: str,
                       parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Every problem with a draft, as `(anchor, message)`.

        Writes nothing. Each check mirrors a refusal the corresponding write
        already makes, and duplicating them here is the whole value: the reader
        is told about all of them at once instead of discovering one per
        attempt, with the previous three already applied.

        EVERY part is checked. Returning on the first problem would give the
        reader exactly what they had before.

        Staleness is not checked here. The service is handed a draft to
        validate; whether the document moved under the editor is a fact about
        the HTTP exchange and belongs to the route.
        """
        self._require(skill_id, tenant_id)
        problems: list[tuple[str, str]] = []
        for anchor, text in parts:
            message = self._problem_with(skill_id, tenant_id, anchor, text)
            if message is not None:
                problems.append((anchor, message))
        return problems

    def _problem_with(self, skill_id: str, tenant_id: str, anchor: str,
                      text: str) -> str | None:
        """What is wrong with one proposed edit, or None.

        A dispatch on the anchor's prefix, one arm per editable kind, each arm
        repeating the refusals its own write makes. Anything the outline marks
        uneditable falls through to the last arm rather than being ignored: a
        draft naming a part with no write behind it is a bug in the client, and
        silently dropping it would hide the bug and lose the edit.
        """
        cleaned = text.strip()
        if anchor == "description":
            skill = self._require(skill_id, tenant_id)
            if not cleaned:
                return "a description cannot be empty"
            if cleaned == skill.description:
                return "the description is unchanged"
            return None
        if anchor == "title":
            skill = self._require(skill_id, tenant_id)
            if not cleaned:
                return "a skill needs a name"
            if cleaned == skill.name:
                return "the name is unchanged"
            return None
        if anchor.startswith("section:"):
            section = self._store.get_section(anchor[len("section:"):], tenant_id=tenant_id)
            if section is None or section.tenant_id != tenant_id \
                    or section.skill_id != skill_id:
                return "this section is no longer part of this skill"
            if not cleaned:
                return "a section heading cannot be empty"
            if cleaned == section.heading:
                return "the heading is unchanged"
            return None
        if anchor.startswith("block:"):
            block_ref = anchor[len("block:"):]
            block = self._store.get_content_block(block_ref, tenant_id=tenant_id)
            section = self._store.section_for_block(block_ref, tenant_id=tenant_id)
            if block is None or section is None or section.skill_id != skill_id \
                    or section.tenant_id != tenant_id:
                return "this text is no longer part of this skill"
            if not cleaned:
                return "the replacement text must not be empty"
            # Exact comparison, as `revise_section` uses and for the same
            # reason: a person editing markdown may legitimately change only
            # whitespace, because list indentation and code fences are content.
            if text == block.body:
                return "the text is unchanged"
            return None
        if anchor.startswith("rule:"):
            rule = self._store.get_rule(anchor[len("rule:"):])
            if rule is None or rule.tenant_id != tenant_id:
                return "this rule is no longer part of this skill"
            if skill_id not in {sk.id for sk
                                in self._store.skills_for_rule(rule.id, tenant_id=tenant_id)}:
                return "this rule is no longer part of this skill"
            if rule.plane is not Plane.DATA:
                return ("this is a control-plane rule. It describes how OMS "
                        "itself operates and is never published, so editing it "
                        "here would change nothing an agent reads.")
            if not cleaned:
                return "a rule's text cannot be empty"
            if cleaned == rule.body:
                return "the text is unchanged"
            return None
        return "this part cannot be edited"

    # -- shared ---------------------------------------------------------------

    def _require(self, skill_id: str, tenant_id: str) -> Skill:
        skill = self._store.get_skill(skill_id, tenant_id=tenant_id)
        if skill is None or skill.tenant_id != tenant_id:
            raise SkillNotFound(f"no skill {skill_id!r} for tenant {tenant_id!r}")
        return skill

    def _record(self, tenant_id: str, skill_id: str, actor: str | None, *,
                before: str, after: str) -> None:
        """One event per changed thing, as `people_api` records one per
        mutation: "who rewrote the description" and "who moved it under
        legal" are different questions and a combined row answers neither.

        Called after the store write, so an edit that raised records nothing.
        """
        self._record_action(tenant_id, skill_id, actor,
                            action=self._event_actions.SKILL_EDITED,
                            before=before, after=after)

    def _record_action(self, tenant_id: str, subject_id: str, actor: str | None,
                       *, action: AdminAction, before: str | None,
                       after: str | None, subject_kind: str = "skill") -> None:
        """`subject_kind` defaults to "skill" so no existing caller changes.
        `edit_rule` passes "rule": the subject of that event is the rule, and a
        reader resolving a display name needs to know which store to ask."""
        if self._admin_events is None:
            return
        self._admin_events.record_admin_event(AdminEvent(
            # uuid4 rather than a hash of the fields: two identical edits
            # minutes apart are two events, and a content-derived id would
            # collapse them under the store's MERGE.
            id=f"admin-{uuid.uuid4().hex}",
            tenant_id=tenant_id, at=datetime.now(timezone.utc),
            action=action,
            subject_id=subject_id, subject_kind=subject_kind,
            actor_person_id=actor, before=before, after=after))
