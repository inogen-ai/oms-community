"""One rule, one skill: the copies a rule gets when it fits several skills.

A rule belongs to exactly one skill and is placed only in that skill's
sections. When one learning, decision or review fits several skills, each
skill gets its own copy, and the copies are joined by an EQUIVALENT_TO edge
whose `kind` is "copy", written in both directions. Nothing passes between
copies: a change to one changes no other.

Every copy id keeps its source id as its prefix. The renderer breaks ties
between rules, and between a rule's examples, on id, and some readers know a
rule's origin by its id prefix (`rule-import-`). A copy that sorted elsewhere
or lost the prefix would reorder or relabel a published file. Ids are
deterministic per skill, so a retried write finds the copy it made.

The prefix keeps a copy where its source sorted on one condition: no other
rule of the skill has an id that is the source id plus a suffix sorting below
the copy's own suffix (`-copy-` or `-split-`, then 12 hex digits). The rules
`rule-t2` and `rule-t2-abc` are such a pair. The copy `rule-t2-split-...`
sorts after `rule-t2-abc`, so the two swap places in a published file when
they tie, on count or on placement order. The ids OMS makes itself cannot form
the pair: `rule-import-` plus 16 hex digits, `source-rule-` plus a digest and
`rule-` plus a generated uuid each have one length, and the copies of one
learning sit in different skills. Only transaction ids that a caller chose can
form it, when one extends another with a hyphen, as `t2` and `t2-abc` do.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from oms.domain.identity import SkillRef

COPY_KIND = "copy"
COPY_CONFIDENCE = 1.0


class CopyCause(str, Enum):
    """Why a copy exists, stored as the copy edge's `cause`."""
    LEARNING = "learning"
    MANUAL = "manual"
    REVIEW = "review"
    SPLIT = "split"


@dataclass(frozen=True)
class RuleSlot:
    """A place for a copy: a RULES section of the copy's own skill."""
    section_id: str
    order: int | None = None
    group: str | None = None


def _digest(*parts: str) -> str:
    # The unit separator joins the parts. A transaction id cannot contain it,
    # and a skill storage key is JSON, which escapes control characters, so
    # ("a", "bc") and ("ab", "c") never hash alike.
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def first_rule_id(transaction_id: str) -> str:
    """The first rule a transaction creates, as it always was."""
    return f"rule-{transaction_id}"


def learning_copy_id(transaction_id: str, skill: SkillRef) -> str:
    """A further copy a transaction creates (the compiler, Community create)."""
    return f"{first_rule_id(transaction_id)}-{_digest(skill.storage_key)[:8]}"


def copy_rule_id(source_rule_id: str, skill: SkillRef) -> str:
    """A copy of an existing rule (review moves, Community reinforce)."""
    return f"{source_rule_id}-copy-{_digest(source_rule_id, skill.storage_key)[:12]}"


def split_rule_id(original_rule_id: str, skill: SkillRef) -> str:
    """The copy the schema 3 split makes for one of a shared rule's skills."""
    return f"{original_rule_id}-split-{_digest(original_rule_id, skill.storage_key)[:12]}"


def copy_example_id(source_example_id: str, copy_rule_id: str) -> str:
    """The id of an example copied under a copy of its rule."""
    return f"{source_example_id}-{_digest(source_example_id, copy_rule_id)[:12]}"


_LATER_COPIES = r"(?:-(?:copy|split)-[0-9a-f]{12})*"


def created_by_transaction(rule_id: str, transaction_id: str) -> bool:
    """Whether `transaction_id` created this rule: its first copy, a further
    copy, or a review or split copy of one of those. A rule it only
    corroborated has another id, so this is the "created, not confirmed"
    test that used to be `rule_id == f"rule-{transaction_id}"`.

    One case cannot be told apart from the ids alone. A transaction whose own
    id is another id plus a hyphen and eight hex digits ("t1-deadbeef" after
    "t1") makes a first rule that reads as a further copy of the shorter
    transaction."""
    pattern = re.escape(first_rule_id(transaction_id)) + r"(?:-[0-9a-f]{8})?" + _LATER_COPIES
    return re.fullmatch(pattern, rule_id) is not None
