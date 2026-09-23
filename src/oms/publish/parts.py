"""One addressable piece of a rendered SKILL.md.

Its own module rather than another type in `render.py`, which is already 2959
lines. The console imports this and nothing else from the publish package, so
the editor's contract is one small file somebody can read in a sitting.

`lines` and `edit_text` are deliberately different fields, and conflating them
is the mistake this docstring exists to prevent. `lines` is what the part
CONTRIBUTES TO THE FILE: a rule's bullet, a heading's `##` prefix, the blank
line after a section. `edit_text` is what a PERSON TYPES: the rule's body, the
heading's words. A textarea seeded from `lines` would have the reader editing
the renderer's punctuation, and saving it would write `* * the rule`.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class DocPart:
    """`anchor` addresses this part in a save, and is unique within a document.

    The forms are `name`, `description`, `title`, `section:<id>`, `block:<id>`,
    `rule:<id>`, `examples:<section id>`, `group:<section id>:<n>` and
    `references`. Structured rather than an index, because an index changes the
    moment a rule is retracted and a draft prepared against the old document
    would then write its edits to the wrong rows.

    `note` is required when `editable` is False and says, in a reader's words,
    why there is no control. A padlock with no sentence beside it sends
    somebody looking for a button that does not exist.
    """
    anchor: str
    kind: str
    lines: tuple[str, ...]
    editable: bool = False
    source_id: str | None = None
    note: str | None = None
    edit_text: str | None = None

    @property
    def markdown(self) -> str:
        return "\n".join(self.lines)


def join_parts(parts: Sequence[DocPart]) -> str:
    """The parts as one file, assembled exactly as the renderers assemble their
    line list: joined on newlines, trailing blank lines trimmed, one newline at
    the end. `tests/publish/test_outline.py` asserts this equals what the
    renderer returns for every fixture."""
    lines: list[str] = []
    for part in parts:
        lines.extend(part.lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def document_revision(parts: Sequence[DocPart]) -> str:
    """A token identifying this exact document, for optimistic concurrency.

    Over the JOINED text rather than over part ids: a rule whose body changed
    keeps its id, and a token that did not move would let a second editor
    overwrite the first with no warning.
    """
    return "sha256:" + hashlib.sha256(
        join_parts(parts).encode("utf-8")).hexdigest()
