"""Aligning two snapshots of a skill: what a reader sees on the compare pane.

Pure functions only, on the `parts_json` shape `SkillHistory.capture` writes
(a plain dict per `DocPart`: `anchor, kind, lines, editable, source_id, note,
edit_text`), never on the store or a live skill. That is what lets Task 8's
routes call this the same way whether the newer side is a stored version or
the skill as it stands today.

`compare_parts` answers by anchor, not by position: a rule kept its anchor
even if its neighbours shuffled, so lining rows up on anchor identity is what
keeps "changed" from lighting up every row after one insertion. A rule that
existed in the old snapshot and has none in the new one still needs a row -
that is the whole point of "restore the rule that was dropped" - so those
`only_old` rows are woven back in next to the surviving anchor they used to
sit beside, rather than dumped at the end where they would read as newly
added instead of removed.

Restorability is deliberately narrower than "this row differs". Four things
can never be staged, whatever `staging` says: a generated part (its content
comes from the rules at publish, so editing it here would be overwritten the
next time anything published); a hard-deleted rule under `only_old` (nothing
exists to restore into - the rule node itself is gone, not merely retired);
an `only_old` or `only_new` row for anything that is not a rule (design
decision 3 scopes existence-restore to rules - there is no retract verb for
a heading or block added since, nor a restore verb for one dropped, so a
button on either side would have nothing to call); and, blanket, every row
at all when `staging` is False, because that mode compares two snapshots
that are both history, and "Use this" only ever writes into today's draft.

One case used to land on that "not a rule" branch dishonestly. A block's
anchor carries a hash of the block's own text (`block_id` in
`oms.domain.ids`), so rewriting a paragraph or a table gives it a new
anchor, and aligning on anchor identity alone read one edit as a block
dropped plus an unrelated block added - two rows, side by side, neither
restorable, so the old wording could not be brought back at all.
`_repair_rewritten_blocks` re-anchors the old block onto the block that
replaced it before alignment runs, so the pair comes back as one `changed`
row keyed by today's anchor. That narrows the paragraph above rather than
widening it: the row restores by writing text into a part that exists
today, which is the ordinary `changed` path the block write route already
serves, and existence-restore stays scoped to rules.

Rule identity is not the same thing as `kind == "rule"`: a reference-only
rule renders with `kind="overflow-rule"` (it still publishes, just to
`references/edge-cases.md` instead of the body - see
`_overflow_parts` in `oms.publish.render`), and it is exactly as much a rule
as one with `kind="rule"` - same `source_id`, same retire/hard-delete
distinction via `rule_status`. Narrowing rule detection to `kind == "rule"`
would silently drop every overflow rule's identity from the compare view.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from oms.publish.parts import DocPart, join_parts

GENERATED_NOTE = "this part is rebuilt from the rules at publish; change the rules instead"
HARD_DELETED_NOTE = "this rule no longer exists; copy the text into a new rule instead"
NOT_A_RULE_NOTE = ("only a rule's existence can be restored or retracted; this part "
                   "has neither, so copy the text across yourself instead")

# Kinds a reader edits as a rule's body, whatever file the rule publishes to.
_RULE_KINDS = frozenset({"rule", "overflow-rule"})


@dataclass(frozen=True)
class CompareRow:
    """One aligned row of a compare view. `rule_id` and `rule_status_today`
    are set only for rule-kind rows; every other kind carries `None` in both,
    because existence-restore (retract/restore) has no meaning for a
    heading or a generated block."""
    anchor: str
    kind: str
    state: str            # "changed" | "only_old" | "only_new" | "same"
    old_text: str | None  # edit_text (or joined lines for uneditable parts)
    new_text: str | None
    rule_id: str | None
    rule_status_today: str | None
    restorable: bool
    note: str | None


def _text(part: dict | None) -> str | None:
    """What the row shows for one side: the words a person typed, or - for a
    part with no textarea - the markdown it renders as, since `edit_text` is
    always `None` there."""
    if part is None:
        return None
    if part.get("editable", False):
        return part.get("edit_text")
    return "\n".join(part.get("lines", ()))


def _rule_id(part: dict) -> str | None:
    """`source_id` is the rule's real id; the anchor is only ever `rule:<id>`
    by convention, and this must not assume the two agree. Both rule kinds
    count - `overflow-rule` is a reference-only rule, still a rule, just
    published to a different file (see the module docstring)."""
    if part.get("kind") not in _RULE_KINDS:
        return None
    return part.get("source_id")


def _row(*, anchor: str, kind: str, state: str, old_part: dict | None,
        new_part: dict | None, rule_status: Callable[[str], str | None],
        staging: bool) -> CompareRow:
    part = new_part if new_part is not None else old_part
    if part is None:
        raise ValueError(f"compare row {anchor!r} has neither an old nor a new part")
    editable = part.get("editable", False)
    rule_id = _rule_id(part)
    rule_status_today = rule_status(rule_id) if rule_id is not None else None

    if not editable:
        restorable, note = False, GENERATED_NOTE
    elif state == "same":
        restorable, note = False, None
    elif rule_id is None and state in ("only_old", "only_new"):
        # Existence-restore (design decision 3) is scoped to rules: a
        # heading or prose block that was dropped, or one added since, has
        # no retract/restore verb to stage either way round.
        restorable, note = False, NOT_A_RULE_NOTE
    elif state == "only_old" and rule_status_today is None:
        restorable, note = False, HARD_DELETED_NOTE
    else:
        restorable, note = staging, None

    return CompareRow(
        anchor=anchor, kind=kind, state=state,
        old_text=_text(old_part), new_text=_text(new_part),
        rule_id=rule_id, rule_status_today=rule_status_today,
        restorable=restorable, note=note,
    )


# A block's anchor carries a hash of its own content
# (`block_id` in `oms.domain.ids`), so rewriting a block re-anchors it. Two
# anchors that differ only in that hash are the same block at two moments,
# and aligning them by anchor identity alone would read one paragraph's edit
# as a paragraph deleted plus a different one added - which is what the
# reader saw, with no way to restore either half, because neither is a rule.
_HASH_SUFFIX = re.compile(r"-[0-9a-f]{16}$")


def _block_stem(anchor: str) -> str | None:
    """The part of a block anchor that survives a rewrite, or `None` for
    anything that is not a content block. Deliberately narrow: a rule's
    anchor is its id and a heading's has no hash, so neither can pair."""
    if not anchor.startswith("block:"):
        return None
    stem, hit = _HASH_SUFFIX.subn("", anchor)
    return stem if hit else None


def _repair_rewritten_blocks(old_parts: list[dict],
                             new_parts: list[dict]) -> list[dict]:
    """`old_parts` with each rewritten block re-anchored onto the block that
    replaced it, so the alignment below sees one anchor and reads a text
    change. Only blocks pair, only within one stem, and only in document
    order; a block with no partner keeps its own anchor and stays an honest
    drop, because there is no verb to bring a block back."""
    new_anchors = {p["anchor"] for p in new_parts}
    old_anchors = {p["anchor"] for p in old_parts}
    spare: dict[str, list[str]] = {}
    for part in new_parts:
        if part["anchor"] in old_anchors:
            continue
        stem = _block_stem(part["anchor"])
        if stem is not None:
            spare.setdefault(stem, []).append(part["anchor"])

    repaired: list[dict] = []
    for part in old_parts:
        anchor = part["anchor"]
        stem = None if anchor in new_anchors else _block_stem(anchor)
        candidates = spare.get(stem) if stem is not None else None
        if candidates:
            part = {**part, "anchor": candidates.pop(0)}
        repaired.append(part)
    return repaired


def compare_parts(old_parts: list[dict], new_parts: list[dict], *,
                  rule_status: Callable[[str], str | None],
                  staging: bool) -> list[CompareRow]:
    """Align two snapshots' parts into rows a compare view can render.

    Row order follows `new_parts`; an anchor only the old snapshot has is
    spliced in right after the nearest earlier anchor that survives into
    `new_parts` (or at the front, if none does) - see the module docstring
    for why that is the useful order rather than appending drops at the end.
    """
    old_parts = _repair_rewritten_blocks(old_parts, new_parts)
    old_by_anchor = {p["anchor"]: p for p in old_parts}
    new_by_anchor = {p["anchor"]: p for p in new_parts}

    # Which surviving anchor does each dropped anchor trail? `None` means
    # "before every surviving anchor", i.e. the front of the result.
    dropped_after: dict[str | None, list[str]] = {}
    predecessor: str | None = None
    for part in old_parts:
        anchor = part["anchor"]
        if anchor in new_by_anchor:
            predecessor = anchor
        else:
            dropped_after.setdefault(predecessor, []).append(anchor)

    def only_old_rows(predecessor_anchor: str | None) -> list[CompareRow]:
        return [
            _row(anchor=dropped_anchor, kind=old_by_anchor[dropped_anchor]["kind"],
                state="only_old", old_part=old_by_anchor[dropped_anchor],
                new_part=None, rule_status=rule_status, staging=staging)
            for dropped_anchor in dropped_after.get(predecessor_anchor, [])
        ]

    rows: list[CompareRow] = list(only_old_rows(None))
    for new_part in new_parts:
        anchor = new_part["anchor"]
        old_part = old_by_anchor.get(anchor)
        if old_part is None:
            state = "only_new"
        elif _text(old_part) == _text(new_part):
            state = "same"
        else:
            state = "changed"
        rows.append(_row(anchor=anchor, kind=new_part["kind"], state=state,
                         old_part=old_part, new_part=new_part,
                         rule_status=rule_status, staging=staging))
        rows.extend(only_old_rows(anchor))

    if not staging:
        # Belt and braces: every branch above already threads `staging`
        # through, but a row must never come back restorable in this mode,
        # so enforce it once here rather than trust every branch to agree.
        rows = [row if not row.restorable else _unset_restorable(row) for row in rows]
    return rows


def _unset_restorable(row: CompareRow) -> CompareRow:
    return CompareRow(
        anchor=row.anchor, kind=row.kind, state=row.state,
        old_text=row.old_text, new_text=row.new_text, rule_id=row.rule_id,
        rule_status_today=row.rule_status_today, restorable=False, note=row.note,
    )


def version_markdown(parts: list[dict]) -> str:
    """The file as it was: `join_parts` over one snapshot's parts, the same
    assembly the renderer and the live editor use, so a version read back
    later shows exactly the document a reader would have seen at the time."""
    doc_parts = [
        DocPart(anchor=part["anchor"], kind=part["kind"],
               lines=tuple(part.get("lines", ())),
               editable=part.get("editable", False),
               source_id=part.get("source_id"), note=part.get("note"),
               edit_text=part.get("edit_text"))
        for part in parts
    ]
    return join_parts(doc_parts)
