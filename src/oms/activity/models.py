"""Local content and settings history records."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class AdminAction(str, Enum):
    SKILL_CREATED = 'skill_created'
    SKILL_DELETED = 'skill_deleted'
    SKILL_EDITED = 'skill_edited'
    RULE_EDITED = 'rule_edited'
    SKILL_RESTORED = 'skill_restored'
    PUBLISH_RUN = 'publish_run'
    SETTINGS_CHANGED = 'settings_changed'
    SANITISATION_QUARANTINED = 'sanitisation_quarantined'


@dataclass(frozen=True)
class AdminEvent:
    """One administrative mutation, persisted because nothing else records it.

    `actor_person_id` is the administrator who acted, or None when nobody was
    proven: the shared `OMS_ADMIN_TOKEN`, the development switch, or a
    deployment with no gate. That is exactly the distinction
    `ReviewItem.decided_by` draws, and it is drawn the same way here so the two
    cannot come to mean different things. `at` is recorded either way, so
    "when" survives where "who" does not.

    `before` and `after` are rendered strings, not structured values. They are
    written once and only ever read back to display, and a string keeps a
    frozenset of domains and a trust enum in one field without the reader
    needing to know which action produced the row.

    `subject_kind` says what `subject_id` points at, because administration
    stopped being only about people: a skill edit names a Skill. It travels
    beside the id rather than being inferred from the action, so a reader
    resolving a display name knows which store to ask without a table of
    actions to consult - the same reasoning `ActivityEvent.subject_kind`
    carries for review items.
    """
    id: str
    tenant_id: str
    at: datetime
    action: AdminAction
    subject_id: str
    subject_kind: str = "person"
    actor_person_id: str | None = None
    before: str | None = None
    after: str | None = None

