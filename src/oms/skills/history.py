"""Every change to a skill leaves a version behind.

`capture` renders the same outline the editor shows (`Publisher.outline_skill`)
so a version can never disagree with the document publish writes. Dedup is on
content, not on time: document revision plus metadata plus rules, because tags
and domain are not in the rendered document and a metadata-only change must
still version. Capture failure never fails the write it follows: callers get
`None` and the safety-net capture on the next history read records the change
as `unattributed`.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone

from oms.activity.models import AdminAction, AdminEvent
from oms.domain.models import SkillVersion
from oms.domain.types import SkillVersionCause
from oms.ports.activity import AdminEventStore
from oms.ports.repositories import HistoryCaptureRepository
from oms.publish.parts import document_revision
from oms.publish.publisher import Publisher

logger = logging.getLogger(__name__)

# Newest versions kept per skill, plus always the oldest (the `created` row).
# The tenant's `skill_history_keep` setting overrides this through the `keep`
# callable, which every process that captures should pass (build one with
# `settings.skill_history.keep_reader`). This is the floor a caller gets when
# it does not, and a trim is permanent, so a history built without a reader
# quietly undoes a raised ceiling.
DEFAULT_KEEP = 200


class SkillHistory:
    """Captures a snapshot of a skill on every write path that changes one.

    Takes the store and the publisher and nothing else - no LLM, no embedder -
    because a version is a rendering plus a read of the graph, never a
    judgement about the content.
    """

    def __init__(self, *, store: HistoryCaptureRepository, publisher: Publisher,
                admin_events: AdminEventStore | None = None,
                keep: Callable[[str], int] | None = None,
                event_actions=AdminAction) -> None:
        self._store = store
        self._publisher = publisher
        self._admin_events = admin_events
        self._keep = keep
        self._event_actions = event_actions
        # Restore groups already announced in the activity feed. Process-local
        # by design: a group lives for the seconds one save takes, and a
        # duplicate event after a restart is a smaller wrong than a store
        # round-trip on every part write.
        self._restored_groups: set[tuple[str, str, str | None]] = set()

    def snapshot_state(self) -> object:
        events = self._admin_events
        event_state = events.snapshot_state() if hasattr(events, "snapshot_state") else None
        return self._restored_groups.copy(), event_state

    def restore_state(self, state: object) -> None:
        if not isinstance(state, tuple) or len(state) != 2 or not isinstance(state[0], set):
            raise TypeError("Invalid history rollback state")
        self._restored_groups = state[0].copy()
        if state[1] is not None:
            self._admin_events.restore_state(state[1])

    def capture(self, skill_id: str, tenant_id: str, *,
               cause: SkillVersionCause,
               actor: str | None = None,
               detail: str | None = None,
               group_id: str | None = None,
               restore_source: str | None = None,
               restore_taken: str | None = None) -> SkillVersion | None:
        """Snapshot the skill now, or do nothing if the content is unchanged.

        Wrapped so a rendering failure - a bad section, a store hiccup - never
        surfaces to the caller, which is a write path with its own reason for
        being here. The version this call would have made is simply not
        written; the safety net on the next history read is what recovers it.
        """
        try:
            return self._capture(
                skill_id, tenant_id, cause=cause, actor=actor, detail=detail,
                group_id=group_id, restore_source=restore_source,
                restore_taken=restore_taken)
        except Exception:
            logger.exception("skill history capture failed for %s", skill_id)
            return None

    def capture_required(self, skill_id: str, tenant_id: str, **kwargs) -> SkillVersion | None:
        """Capture inside a caller's unit of work; a failure must roll it back."""
        values = {"actor": None, "detail": None, "group_id": None,
                  "restore_source": None, "restore_taken": None, **kwargs}
        return self._capture(skill_id, tenant_id, required=True, **values)

    def capture_for_rule(self, rule_id: str, tenant_id: str, *,
                         cause: SkillVersionCause,
                         actor: str | None = None,
                         detail: str | None = None,
                         group_id: str | None = None,
                         restore_source: str | None = None,
                         restore_taken: str | None = None) -> None:
        """Capture every skill a rule anchors to.

        A rule is written by review or by the editor's own `edit_rule`, and
        `BELONGS_TO` is not exclusive: a shared correction can anchor to more
        than one skill, and every one of those documents just changed.
        """
        try:
            skills = self._store.skills_for_rule(rule_id, tenant_id=tenant_id)
        except Exception:
            logger.exception("skill history lookup failed for rule %s", rule_id)
            return
        for skill in skills:
            if skill.tenant_id != tenant_id:
                continue
            self.capture(skill.id, tenant_id, cause=cause, actor=actor,
                        detail=detail, group_id=group_id,
                        restore_source=restore_source,
                        restore_taken=restore_taken)

    # -- internals --------------------------------------------------------

    def _capture(self, skill_id: str, tenant_id: str, *,
                cause: SkillVersionCause, actor: str | None,
                detail: str | None, group_id: str | None,
                restore_source: str | None,
                restore_taken: str | None, required: bool = False,
                source_operation_id: str | None = None, source_origin_id: str | None = None,
                source_revision: str | None = None) -> SkillVersion | None:
        skill = self._store.get_skill(skill_id, tenant_id=tenant_id)
        if skill is None or skill.tenant_id != tenant_id:
            return None
        parts, _path = self._publisher.outline_skill(skill_id, tenant_id)
        revision = document_revision(parts)
        parts_json = json.dumps([asdict(p) for p in parts])
        metadata_json = json.dumps({
            "name": skill.name,
            "description": skill.description,
            "domain": skill.domain,
            "status": skill.status.value,
            "publish_enabled": skill.publish_enabled,
            "origin": skill.origin.value,
            "declared_license": skill.declared_license,
            "document_mode": skill.document_mode,
            "curated": sorted(skill.curated),
            "tags": sorted(self._store.tags_for_skill(skill_id, tenant_id=tenant_id)),
        })
        rules_json = json.dumps(sorted((
            {"id": r.id, "body": r.body, "status": r.status.value,
             "corroboration_count": r.corroboration_count,
             "polarity": r.polarity.value}
            for r in self._store.rules_for_skill(skill_id, tenant_id=tenant_id)
        ), key=lambda r: r["id"]))
        files_json = json.dumps([
            {"path": path, "content_ref": artefact.content_ref, "size": artefact.size,
             "mode": artefact.mode, "kind": artefact.kind.value, "source_ref": artefact.source_ref}
            for artefact, path in sorted(self._store.artefacts_for_skill(skill_id, tenant_id=tenant_id),
                                        key=lambda entry: entry[1])
        ], sort_keys=True)

        latest = self._store.latest_skill_version(tenant_id, skill_id)
        if latest is not None and latest.revision == revision \
                and latest.metadata_json == metadata_json \
                and latest.rules_json == rules_json and latest.files_json == files_json:
            return None

        if restore_source is not None:
            # Only for the skill the restore was actually staged against.
            # `capture_for_rule` loops over EVERY skill a rule anchors to, and
            # a shared rule reaches skills the reader never opened; stamping
            # those `restore` too gave them a detail naming a version that is
            # not in their own history, and handed the audit event to
            # whichever of them sorted first. `_restore_detail` answers None
            # for any skill whose history does not hold the source version,
            # and such a skill simply keeps the cause its own write path gave
            # it.
            restore_detail = self._restore_detail(
                tenant_id, skill_id, restore_source, restore_taken)
            if restore_detail is not None:
                cause = SkillVersionCause.RESTORE
                detail = restore_detail

        version = SkillVersion(
            id=f"skillversion-{uuid.uuid4().hex[:12]}",
            skill_id=skill_id, tenant_id=tenant_id,
            at=datetime.now(timezone.utc), revision=revision, cause=cause,
            actor_person_id=actor, detail=detail, group_id=group_id,
            parts_json=parts_json, metadata_json=metadata_json,
            rules_json=rules_json, files_json=files_json,
            source_operation_id=source_operation_id, source_origin_id=source_origin_id,
            source_revision=source_revision)
        self._store.append_skill_version(version)
        self._trim(tenant_id, skill_id, required=required)
        if cause is SkillVersionCause.RESTORE:
            self._announce_restore(tenant_id, skill_id, actor, group_id, detail, required=required)
        return version

    def _restore_detail(self, tenant_id: str, skill_id: str,
                        restore_source: str,
                        restore_taken: str | None) -> str | None:
        """The one-line audit sentence: when the restored version was taken
        from, and how much of the diff was carried across, in words rather
        than the fraction a screen would show.

        `None` when `restore_source` does not name one of THIS skill's own
        versions under THIS tenant. `get_skill_version` is keyed on the id
        alone in both adapters, and the id arrives on a caller-supplied
        header (`X-Restore-Source`) over routes that a reviewer, not only an
        administrator, can reach. Echoing that header into the sentence put
        arbitrary caller text into a history line that can never afterwards
        be edited or deleted, and into the administrator activity feed with
        it - the same reason `clean_filename` exists on the upload road.
        Answering None instead means an unresolvable, foreign-tenant or
        foreign-skill id simply does not make the write a restore.
        """
        source = self._store.get_skill_version(restore_source, tenant_id=tenant_id)
        if source is None or source.tenant_id != tenant_id \
                or source.skill_id != skill_id:
            return None
        when = source.at.strftime("%d %b %H:%M")
        taken = ""
        if restore_taken and "/" in restore_taken:
            picked, _, total = restore_taken.partition("/")
            if picked.strip().isdigit() and total.strip().isdigit():
                taken = f", taking {int(picked)} of {int(total)} changes"
        return f"restored to the version of {when}{taken}"

    def _announce_restore(self, tenant_id: str, skill_id: str,
                          actor: str | None, group_id: str | None,
                          detail: str | None, *, required: bool = False) -> None:
        """One `AdminEvent` per restore save, not one per part write.

        `applyDraft` writes one part per HTTP request, so a restore of several
        parts is several `capture` calls sharing one `group_id`. Keyed on that
        id AND the skill, so only the first write of that save reaches the
        feed while a second skill in the same save still gets its own event.
        Lacking a group id, the key falls back to the skill and the detail.

        The key is added only once the record has LANDED. Adding it first
        meant a single failing write lost the event for good: `capture`
        swallows the exception, and every later part write of the same save
        then saw the key already there and returned early, so the append-only
        history held a `restore` version with no `SKILL_RESTORED` event
        anywhere and nothing left to re-read it. Failing here is caught rather
        than raised, so the version this call just wrote is still returned to
        its caller; the next part write of the same save makes the attempt
        again. Required captures propagate the failure to their transaction.
        """
        if self._admin_events is None:
            return
        key = (tenant_id, skill_id, group_id if group_id is not None else detail)
        if key in self._restored_groups:
            return
        try:
            self._admin_events.record_admin_event(AdminEvent(
                # Same id scheme SkillAdminService._record_action uses: uuid4
                # rather than a hash of the fields, because two restores of the
                # same skill minutes apart are two events.
                id=f"admin-{uuid.uuid4().hex}",
                tenant_id=tenant_id, at=datetime.now(timezone.utc),
                action=self._event_actions.SKILL_RESTORED, subject_id=skill_id,
                subject_kind="skill", actor_person_id=actor,
                before=None, after=detail))
        except Exception:
            if required:
                raise
            logger.exception("skill restore announcement failed for %s", skill_id)
            return
        self._restored_groups.add(key)

    def _trim(self, tenant_id: str, skill_id: str, *, required: bool = False) -> None:
        keep = DEFAULT_KEEP
        if self._keep is not None:
            try:
                # Asked per tenant, not once per process: the ceiling is a
                # tenant setting, and the compile worker captures for every
                # tenant it drains through one history.
                keep = max(1, int(self._keep(tenant_id)))
            except Exception:
                if required:
                    raise
                logger.exception("skill history keep() failed for %s", skill_id)
                keep = DEFAULT_KEEP
        self._store.trim_skill_versions(tenant_id, skill_id, keep)
