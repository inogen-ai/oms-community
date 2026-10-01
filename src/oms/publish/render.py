import hashlib
import json
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from oms.client import credentials as _credentials_module
from oms.domain import repo as _repo_module
from oms.domain.models import (
    Constraint, ContentBlock, Example, Rule, RulePlacement, Section, Skill,
)
from oms.domain.types import ExampleKind, Plane, Polarity, RuleStatus, SectionKind
from oms.ingestion.schema import ExecutionContext
from oms.publish.harnesses import HARNESSES, Harness
from oms.publish.parts import DocPart, join_parts
from oms.publish.urls import public_repo_url
from oms.ports.publishing import (
    ContributionFragments, PowerShellInstallFragments, ShellInstallFragments,
)

# The long-tail overflow file. The parser skips lines mentioning it on
# re-import and the publisher writes it; renaming it here updates all three
# or round-trip silently breaks.
REFERENCES_FILE = "references/edge-cases.md"

# The filenames a publish emits the always-loaded root file under when the
# operator has not narrowed the list. Stated once, here, and read by both
# `Publisher.publish` (which writes them) and `render_install_script` (which
# writes an import for one of them). Two statements of the same default is
# exactly how the installer came to import a CLAUDE.md that a legal
# `root_instruction_files = ["AGENTS.md"]` had stopped publish emitting.
DEFAULT_ROOT_INSTRUCTION_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

# Who decides that a correction is shared. "automatic": the agent logs a
# qualifying correction itself, in the same turn. "confirm": the agent asks
# the person and submits only a preview they approve. An installation picks
# one, so publish writes both and neither is a tenant-wide switch.
ContributionMode = Literal["automatic", "confirm"]
CONTRIBUTION_MODES: tuple[ContributionMode, ...] = ("automatic", "confirm")


def confirm_variant_name(name: str) -> str:
    """The name a root instruction file's confirm-mode copy is published
    under, beside it: `.confirm` goes before the final suffix as `pathlib`
    splits the name, so `AGENTS.md` becomes `AGENTS.confirm.md`, `CLAUDE.md`
    becomes `CLAUDE.confirm.md` and `rules.v2.md` becomes
    `rules.v2.confirm.md`. A name with no suffix, a dotfile such as
    `.cursorrules` included, gains `.confirm` at the end: `X` becomes
    `X.confirm`.

    A sibling with its own name, never a same-named file in a subdirectory.
    Harnesses find instruction files by exact name, some in subdirectories
    too (Claude Code loads the CLAUDE.md of a directory whose files it
    reads), and a published tree is often committed into a repository an
    agent works in, so a `confirm/CLAUDE.md` could load both modes at once.
    No harness loads `CLAUDE.confirm.md` or `AGENTS.confirm.md` by itself;
    an installation in confirm mode names the file it wants.

    The one statement of the rule: publish writes these names and an
    installer choosing confirm mode reads them, so both ask this function."""
    path = PurePosixPath(name)
    return f"{path.stem}.confirm{path.suffix}"


# First line of every root instruction file and of the Tier 2 manifest, from
# `_constraint_block` below. Exported because it is the only thing in a
# published tree that identifies a root instruction file by content rather
# than by a name the operator is free to choose, and
# `scripts/regenerate_committed_installers.py` has to recognise one. The
# confirm-mode copies open with it too, so a reader that wants the root
# files an installation imports by default leaves out every name that is
# `confirm_variant_name` of another.
ROOT_INSTRUCTION_MARKER = "# Organisational Constraints"

_REFERENCES_POINTER = (
    f"\nFor edge cases and low-frequency rules, read `{REFERENCES_FILE}`.\n"
)

# A rule body that already self-marks as proscriptive (Don't / Do not / Never /
# Avoid X) does not need the "Avoid:" prefix; adding one creates a double-negative.
_SELF_PROSCRIBE = re.compile(r"(?i)^(don[''’]?t|do\s*not|never|avoid)\b")


@dataclass
class RenderedSkill:
    skill_md: str
    references_md: str | None


def publishable(rule: Rule) -> bool:
    """Whether a rule may reach an agent file.

    Two conditions, not one. ACTIVE is the usual lifecycle check. The plane
    check is separate on purpose: a control-plane rule describes how OMS
    itself operates, and it must not publish whatever its status says (work
    plan G5). Status alone was not enough, because a rule can be held for two
    unrelated reasons at once - an untrusted writer (PENDING) and the plane
    hold - and resolving the trust question sets the rule ACTIVE without
    anyone having answered the plane question. Checking the plane here means
    only the control_plane review item can release a control-plane rule, and
    it holds for every route that sets a rule ACTIVE, including
    `restore_rule` after a retraction.
    """
    return rule.status is RuleStatus.ACTIVE and rule.plane is Plane.DATA


def describe(skill: Skill, active_rules: list[Rule]) -> str:
    """Publish-time description for a skill: its own if set, else derived from
    its highest-corroboration prescribe rule (so a description never leads with
    an anti-pattern). Shared by SKILL.md and the CLAUDE.md skill index."""
    if skill.description:
        return skill.description
    pool = [r for r in active_rules if r.polarity is Polarity.PRESCRIBE] or active_rules
    if not pool:
        return f"Use for {skill.domain} tasks."
    head = pool[0].body.strip().rstrip(".")
    return f"Use when handling {skill.domain} tasks such as: {head.lower()}."


#: Present learned decisions in force before the skill's authorial content.
#: Include decisions already represented in prose and state their precedence.
DECISIONS_HEADING = "## Decisions in force"
DECISIONS_LEAD = "These decisions override anything below that disagrees with them."


def in_force(rule: Rule, superseders: Sequence[str]) -> bool:
    """A rule whose value the document states: ACTIVE, or SUPERSEDED with no
    rule holding a SUPERSEDES edge onto it (absorbed into prose; see
    `ReviewService._absorb_if_prose_carries`). Control-plane rules never
    publish, and a rule withdrawn by a later rule is not in force."""
    if rule.plane is not Plane.DATA:
        return False
    if rule.status is RuleStatus.ACTIVE:
        return True
    return rule.status is RuleStatus.SUPERSEDED and not superseders


def _decision_parts(decisions: Sequence[Rule]) -> list[DocPart]:
    if not decisions:
        return []
    # A generated heading: shown by the portal as a line the agents read,
    # with no anchor because nobody may type into it. The rules keep the
    # `rule:` anchor every other route addresses a rule by; a rule is one
    # part wherever it renders, which is why a rule PLACED in a section is
    # not repeated here.
    parts = [DocPart(anchor="decisions", kind="heading",
                     lines=(DECISIONS_HEADING, "", DECISIONS_LEAD, ""),
                     note="Generated: every rule learned from a correction that "
                          "is in force for this skill, stated once where an "
                          "agent reads first.")]
    for r in decisions:
        parts.append(DocPart(anchor=f"rule:{r.id}", kind="rule",
                             lines=(_emit_rule(r),), editable=True,
                             source_id=r.id, edit_text=r.body))
    parts.append(DocPart(anchor="decisions-end", kind="gap", lines=("",),
                         note="Generated: the blank line after a section."))
    return parts


def _emit_rule(r: Rule) -> str:
    body = r.body.strip()
    if r.polarity is Polarity.PROSCRIBE and not _SELF_PROSCRIBE.match(body):
        return f"* Avoid: {body}"
    return f"* {body}"


def _emit_examples(examples: list[Example]) -> list[str]:
    lines: list[str] = []
    for e in examples:
        prefix = "Counter-example" if e.kind is ExampleKind.NEGATIVE else "Example"
        # Round-trip carrier is single-line; flatten any newlines in the body.
        flat = " ".join(line.strip() for line in e.body.splitlines() if line.strip())
        lines.append(f"  {prefix}: {flat}")
    return lines


def _emit_examples_block(examples: list[Example]) -> list[str]:
    """Reconstruct an examples section from its authorial structure: the
    heading-derived name becomes a `###` subheading, an authorial label
    (`**Strong**:` / `**Weak**:` ...) is re-emitted and carries the polarity,
    and multi-line bodies (templates, fenced code) are preserved verbatim.
    Plain unlabelled one-liners stay as bullets."""
    lines: list[str] = []
    current_name: str | None = None
    for ex in examples:
        if ex.name and ex.name != current_name:
            if lines and lines[-1] != "":
                lines.append("")
            lines += [f"### {ex.name}", ""]
        current_name = ex.name
        body = ex.body.rstrip("\n")
        if ex.original_label:
            if "\n" in body:
                lines += [f"**{ex.original_label}**:", body]
            else:
                lines.append(f"**{ex.original_label}**: {body}")
        elif "\n" in body:
            lines.append(body)
        elif ex.kind is ExampleKind.NEGATIVE:
            lines.append(f"* Counter-example: {body}")
        else:
            lines.append(f"* {body}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def outline_skill_sectioned(
    skill: Skill,
    sections: list[Section],
    *,
    rules_by_section: dict[str, list[Rule]],
    blocks_by_section: dict[str, list[ContentBlock]],
    examples_by_section: dict[str, list[Example]],
    examples_by_rule: dict[str, list[Example]],
    extra_body_rules: list[Rule] | None = None,
    placements_by_section: dict[str, list[RulePlacement]] | None = None,
    decisions: Sequence[Rule] | None = None,
) -> tuple[list[DocPart], str | None]:
    """The same document `render_skill_sectioned` writes, as addressable parts.

    `decisions` are the learned rules in force for the skill (see `in_force`),
    rendered as a block after the title. When it is given, the "Additional
    rules" tail is not rendered: every rule it would hold is in the block.

    Every branch here is the branch that was in `render_skill_sectioned`; the
    only change is that each `lines.append(...)` became a part carrying those
    lines. `join_parts` over the result equals the rendered file, which
    `tests/publish/test_outline.py` asserts for every fixture in
    `parity_fixtures`.
    """
    parts: list[DocPart] = []

    def last_line() -> str | None:
        """The last line emitted so far, across parts.

        The group logic below inserts a separating blank line only when the
        previous line is not already blank. That check read `lines[-1]` off one
        flat list; parts keep the same question answerable by walking back over
        any parts that carry no lines at all.
        """
        for part in reversed(parts):
            if part.lines:
                return part.lines[-1]
        return None

    all_active = sorted(
        (r for sec_rules in rules_by_section.values() for r in sec_rules if publishable(r)),
        key=lambda r: r.corroboration_count, reverse=True,
    )
    description = describe(skill, all_active)

    parts.extend(_preamble_parts(skill, description))
    parts.extend(_decision_parts(sorted(
        (r for r in (decisions or []) if not r.reference_only),
        key=lambda r: r.corroboration_count, reverse=True)))

    overflow: list[Rule] = []  # reference-only long-tail rules go to references/edge-cases.md
    for sec in sorted(sections, key=lambda s: s.order):
        heading = (sec.heading or "").strip()
        if heading and heading.lower() != "(intro)":
            parts.append(DocPart(
                anchor=f"section:{sec.id}", kind="heading",
                lines=(f"## {heading}", ""), editable=True,
                source_id=sec.id, edit_text=heading))
        if sec.kind is SectionKind.RULES:
            active = [r for r in rules_by_section.get(sec.id, []) if publishable(r)]
            overflow.extend(r for r in active if r.reference_only)
            body_rules = [r for r in active if not r.reference_only]
            placements = {p.rule_id: p
                          for p in (placements_by_section or {}).get(sec.id, [])
                          if p.order is not None}
            placed = sorted((r for r in body_rules if r.id in placements),
                            key=lambda r: placements[r.id].order)
            unplaced = sorted((r for r in body_rules if r.id not in placements),
                              key=lambda r: r.corroboration_count, reverse=True)
            group: str | None = None
            group_n = 0
            for r in placed:
                g = placements[r.id].group
                if g != group:
                    if g:
                        group_lines: list[str] = []
                        if last_line() is not None and last_line() != "":
                            group_lines.append("")
                        group_lines += [f"### {g}", ""]
                        parts.append(DocPart(
                            anchor=f"group:{sec.id}:{group_n}", kind="group",
                            lines=tuple(group_lines),
                            note="This comes from the source file's structure."))
                        group_n += 1
                    group = g
                parts.append(_rule_part(r, examples_by_rule))
            if placed and unplaced and last_line() is not None and last_line() != "":
                parts.append(DocPart(
                    anchor=f"gap:{sec.id}", kind="gap", lines=("",),
                    note="Generated: it separates the rules the source file "
                         "placed from the ones OMS learned later."))
            for r in unplaced:
                parts.append(_rule_part(r, examples_by_rule))
        elif sec.kind is SectionKind.EXAMPLES:
            parts.append(DocPart(
                anchor=f"examples:{sec.id}", kind="examples",
                lines=tuple(_emit_examples_block(examples_by_section.get(sec.id, []))),
                source_id=sec.id,
                note="Examples are not editable here yet."))
        else:
            for b in blocks_by_section.get(sec.id, []):
                if b.body:
                    parts.append(DocPart(
                        anchor=f"block:{b.id}", kind="prose",
                        lines=(b.body.rstrip("\n"),), editable=True,
                        source_id=b.id, edit_text=b.body))
        parts.append(DocPart(
            anchor=f"section-end:{sec.id}", kind="gap", lines=("",),
            note="Generated: the blank line between sections."))

    extras = [r for r in (extra_body_rules or []) if publishable(r)]
    overflow.extend(r for r in extras if r.reference_only)
    body_extras = sorted((r for r in extras if not r.reference_only),
                         key=lambda r: r.corroboration_count, reverse=True)
    if decisions is not None:
        body_extras = []  # every one of them is in the decisions block
    if body_extras:
        parts.append(DocPart(
            anchor="extras", kind="heading", lines=("## Additional rules", ""),
            note="Generated: it holds the rules that belong to no section of "
                 "the source file."))
        for r in body_extras:
            parts.append(_rule_part(r, examples_by_rule))
        parts.append(DocPart(anchor="extras-end", kind="gap", lines=("",),
                             note="Generated: the blank line after a section."))

    # The overflow rules, last and contributing nothing to SKILL.md. They
    # publish to another file, and Task 14 left them out of the list entirely -
    # so the editor showed a document missing rules the skill really does
    # publish.
    parts.extend(_overflow_parts(overflow))

    return parts, _references_md(overflow, examples_by_rule)


def _references_md(overflow: list[Rule],
                   examples_by_rule: dict[str, list[Example]]) -> str | None:
    """The edge-cases file, or None when nothing overflowed. One copy, because
    both renderers build it identically and a second copy would drift."""
    if not overflow:
        return None
    ref_lines = ["# Edge cases and low-frequency rules", ""]
    for r in overflow:
        ref_lines.append(_emit_rule(r))
        ref_lines.extend(_emit_examples(examples_by_rule.get(r.id, [])))
    return "\n".join(ref_lines) + "\n"


def _overflow_parts(overflow: list[Rule]) -> list[DocPart]:
    """Rules that publish to `references/edge-cases.md` rather than to
    SKILL.md.

    `lines=()` on purpose: they contribute nothing to the joined document,
    which is exactly what they contribute to the published SKILL.md. They are
    still parts because they are still the skill's rules, and an editor that
    hid them would be showing less than the skill says. The note names the file
    they land in, so nobody is left wondering why an edit here does not appear
    in the preview.
    """
    return [DocPart(anchor=f"rule:{r.id}", kind="overflow-rule", lines=(),
                    editable=True, source_id=r.id, edit_text=r.body,
                    note="This rule publishes to references/edge-cases.md "
                         "rather than to SKILL.md, so it does not appear "
                         "above.")
            for r in overflow]


def _preamble_parts(skill: Skill, description: str) -> list[DocPart]:
    """Frontmatter and title, which both renderers emit identically. Shared so
    the two paths cannot come to describe the same four lines differently."""
    return [
        DocPart(
            anchor="name", kind="name", lines=("---", f"name: {skill.id}"),
            note="This is the skill's id. Every agent fetches the skill by it, "
                 "and renaming it here would break all of them."),
        DocPart(
            anchor="description", kind="description",
            lines=(f"description: {description}",), editable=True,
            edit_text=description,
            # `describe` falls back to the highest-corroboration prescribe rule
            # when the skill has none of its own, so what is shown may be
            # derived. Typing here sets `Skill.description`, which then wins and
            # joins `curated` so the next import does not revert it.
            note=None if skill.description else
                 "Derived from the rules below, because this skill has no "
                 "description of its own. Type here to set one."),
        DocPart(anchor="frontmatter-end", kind="frontmatter",
                lines=("---", ""), note="Generated."),
        DocPart(anchor="title", kind="title",
                lines=(f"# {skill.name}", ""), editable=True,
                edit_text=skill.name),
    ]


def _rule_part(rule: Rule, examples_by_rule: dict[str, list[Example]]) -> DocPart:
    """One rule as it renders and as it is edited.

    `lines` is the bullet plus that rule's example lines; `edit_text` is the
    body alone. A textarea seeded from `lines` would have somebody editing the
    renderer's `* ` prefix, and saving it would write the prefix into the rule.
    """
    lines = [_emit_rule(rule)]
    lines.extend(_emit_examples(examples_by_rule.get(rule.id, [])))
    return DocPart(anchor=f"rule:{rule.id}", kind="rule", lines=tuple(lines),
                   editable=True, source_id=rule.id, edit_text=rule.body)


def render_skill_sectioned(
    skill: Skill,
    sections: list[Section],
    *,
    rules_by_section: dict[str, list[Rule]],
    blocks_by_section: dict[str, list[ContentBlock]],
    examples_by_section: dict[str, list[Example]],
    examples_by_rule: dict[str, list[Example]],
    extra_body_rules: list[Rule] | None = None,
    placements_by_section: dict[str, list[RulePlacement]] | None = None,
    decisions: Sequence[Rule] | None = None,
) -> RenderedSkill:
    """Render SKILL.md preserving the authorial section structure: each section
    keeps its `## heading`, authorial content blocks are emitted verbatim, and
    aggregated rules/examples render as lists under their own section.

    When a section's rules carry placements (their authorial sequence and `###`
    subsection, recorded at import), the section is reconstructed in that
    order with its subsection headings; rules without a placement follow,
    ordered by corroboration.

    Unchanged signature, unchanged output. The assembly moved into
    `outline_skill_sectioned` so the console can address what it writes;
    `tests/publish/test_render_parity.py` pins that the bytes did not move
    with it."""
    parts, references_md = outline_skill_sectioned(
        skill, sections, rules_by_section=rules_by_section,
        blocks_by_section=blocks_by_section,
        examples_by_section=examples_by_section,
        examples_by_rule=examples_by_rule,
        extra_body_rules=extra_body_rules,
        placements_by_section=placements_by_section,
        decisions=decisions)
    body = join_parts(parts)
    if references_md is not None:
        body += _REFERENCES_POINTER
    return RenderedSkill(skill_md=body, references_md=references_md)


def has_nothing_to_say(skill_md: str) -> bool:
    """True when a rendered SKILL.md is a title and nothing else.

    Omit empty skills from publication and catalogue listings so agents are
    never directed to a document containing only metadata and a heading.

    Tested on the rendered artefact rather than on the skill's parts, so it can
    never disagree with what would actually be written. A skill of pure
    authorial prose has plenty to say and no rules at all, which is why "has no
    rules" would have been the wrong question.
    """
    body = skill_md
    if body.startswith("---"):
        # Drop the YAML frontmatter: name and description are metadata, and a
        # description is exactly what an empty skill does have.
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]
    lines = [line for line in body.splitlines() if line.strip()]
    # The `# Title` line is the renderer's own scaffolding, not content.
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return not lines


def render_skill(skill: Skill, rules: list[Rule], body_budget: int = 200) -> RenderedSkill:
    """Thin wrapper for callers that don't have example data."""
    return render_skill_with_examples(skill, rules, examples_by_rule={}, skill_examples=[],
                                      body_budget=body_budget)


def outline_skill_flat(skill: Skill, rules: list[Rule],
                       examples_by_rule: dict[str, list[Example]],
                       skill_examples: list[Example],
                       body_budget: int = 200) -> tuple[list[DocPart], str | None]:
    """The flat document as addressable parts.

    A skill with no content blocks takes this path: no headings at all, every
    rule a bullet under the H1. That is not a lesser skill and its
    administrator gets the same editor, so every branch of
    `render_skill_with_examples` is transcribed here the same way Task 14
    transcribed the sectioned ones.
    """
    active = [r for r in rules if publishable(r)]
    body_pool = sorted((r for r in active if not r.reference_only),
                       key=lambda r: r.corroboration_count, reverse=True)
    forced_refs = [r for r in active if r.reference_only]
    body_rules = body_pool[:body_budget]
    overflow = body_pool[body_budget:] + forced_refs

    description = describe(skill, body_pool or active)
    # Frontmatter `name` is the stable slug id (re-import uses it as the id);
    # the H1 carries the human-readable display name.
    parts: list[DocPart] = _preamble_parts(skill, description)
    for r in body_rules:
        parts.append(_rule_part(r, examples_by_rule))

    if skill_examples:
        ex_lines = ["", "## Examples", ""]
        for ex in skill_examples:
            if ex.kind is ExampleKind.NEGATIVE:
                ex_lines.append(f"* Counter-example: {ex.body.strip()}")
            else:
                ex_lines.append(f"* {ex.body.strip()}")
        parts.append(DocPart(anchor="skill-examples", kind="examples",
                             lines=tuple(ex_lines),
                             note="Examples are not editable here yet."))

    parts.extend(_overflow_parts(overflow))
    return parts, _references_md(overflow, examples_by_rule)


def render_skill_with_examples(skill: Skill, rules: list[Rule],
                               examples_by_rule: dict[str, list[Example]],
                               skill_examples: list[Example],
                               body_budget: int = 200) -> RenderedSkill:
    """Unchanged signature, unchanged output. The assembly moved into
    `outline_skill_flat`; `tests/publish/test_render_parity.py` pins that the
    bytes did not move with it."""
    parts, references_md = outline_skill_flat(
        skill, rules, examples_by_rule, skill_examples, body_budget)
    body = join_parts(parts)
    if references_md is not None:
        body += _REFERENCES_POINTER
    return RenderedSkill(skill_md=body, references_md=references_md)


def _check_contribution_mode(mode: str) -> None:
    """Refuse a mode that does not exist. A misspelt "confirmed" must not
    publish automatic instructions to an installation that asked for its
    person to be consulted first."""
    if mode not in CONTRIBUTION_MODES:
        raise ValueError(f"unknown contribution mode {mode!r}: "
                         f"expected {' or '.join(CONTRIBUTION_MODES)}")


def _contribution_block(endpoint: str, *, session_learnings: bool = True,
                        mode: ContributionMode = "automatic") -> list[str]:
    """The standing, always-loaded instruction telling an agent how to post a
    learning back to OMS (spec §6.1). Rendered only when an ingest endpoint is
    configured. It rides the daily pull, so contribution needs no per-agent
    setup.

    Four variants. `mode` decides who shares a correction: "automatic" has
    the agent log it in the same turn, "confirm" has it ask the person and
    submit only a preview they approve. `session_learnings=False` drops the
    `## Session learnings` section, the only text that asks an agent to
    reflect or to send `self_reflection`, and changes nothing above it, so
    the correction guidance depends on the mode alone. A stale copy can still
    send a learning; refusing it is the admission policy's job, not this
    text's.

    That guidance is scoped to what "a person gives you". Unscoped, "useful
    evidenced lessons" invited the agent's own lessons in as ordinary
    corrections, which a policy refusing `self_reflection` never sees, so
    turning learnings off would only have relabelled them.

    Written as a forcing function, not a suggestion: a live agent applied a
    correction, replied "Noted for future code too" and never contributed -
    acknowledgement substituted for action. So the block leads with the
    trigger, demands the log BEFORE the acknowledgement, and de-scopes from
    the published skill list (a coding-style rule matched none of the
    marketing skills and read as out of scope).

    Kept short on purpose. Every line here is fixed overhead in every
    root instruction file, and Windsurf caps global_rules.md at 6000
    bytes: over the cap `write_instructions` writes nothing at all, so
    an overrun costs Windsurf its organisational constraints as well as
    this text. Whatever this block does not spend is what the tenant's
    skills list gets, and `test_it_still_fits_the_windsurf_cap` measures
    the committed root files against that budget.

    The `## Session learnings` half arrived 289 bytes over the room the
    cap left, so the correction prose above it paid for all 289: repeated
    motive was cut (the pipeline paragraph restated the reach the trigger
    paragraph already gives), not the wording that does the teaching.
    Trim here again before touching the learnings section.

    The learnings half then spent 338 more bytes on a forcing function
    of its own. It first read "when you finish a piece of work,
    ask yourself", and a live agent shipped a branch, reported it, and
    filed nothing until the user asked whether it had anything to
    contribute - finishing is a private thought, so the trigger never
    fired. It is now bound to the completion report, the one act the
    agent cannot skip, and answering it out loud is mandatory even when
    the answer is none.

    The one-paragraph retrieval line after the JSON block (repo-scoped rule
    bundles) is the same budget's newest tenant. It stays ONE paragraph
    however many repositories a tenant has - the cost is `query_skill`
    itself, called once per session, never a line per repo - so trim
    elsewhere first if this block ever needs the room back.

    THE TWO ABOVE WERE WRITTEN ON SEPARATE BRANCHES AND BOTH SPENT THE SAME
    87 BYTES OF HEADROOM. Neither author could see the other, git merged them
    without a conflict because they touch different paragraphs, and the
    rendered file came out at 6180 against the 6000 cap - Windsurf silently
    losing its whole rules file, guardrails included, over an arithmetic
    nobody performed. `test_it_still_fits_the_windsurf_cap` is the only thing
    that said so. If you are adding to this block on a branch, run that test
    against the merge, not against your branch.

    180 bytes came back, all of it motive that was already stated once:
    "compiled, deduplicated, and published to every agent in the
    organisation" (the first paragraph already gives the reach and the second
    already gives deduplication), "It is the most reliable" (the list is
    numbered "in order of preference"), "and routes the learning" (the second
    paragraph already says OMS routes it), and "a user who has to ask whether
    you have anything is a user you already failed" (the sentence before it
    already forbids silence). Every instruction survives; only the second
    telling of each reason is gone. Trim the same way next time, and read the
    test's number rather than the byte figure any comment here records - the
    skills list moves under this block and only the test measures the file
    that ships.

    The four variants arrived with a budget of their own: at most 3660 bytes
    each for the loopback endpoint `http://localhost:8000/api/ingest`, because
    the largest committed root file left the block 3676. Confirm mode carries
    about 700 bytes of flow whose question and choices are exact copy, and
    the five future-use tests are longer than the question they replaced, so
    the room came the same way as before, from motive told twice and from
    form. "It validates the structure for you", "The typed signature means
    you fill arguments", "Team rules must go through the OMS pipeline
    instead", the session ref's reason and "OMS fills in the id, timestamp
    and tenant" went (the last contradicts confirm mode, where the agent
    supplies the id); the HTTP body became one JSON line. The routing
    sentence stays: it is the de-scoping the forcing-function paragraph
    describes. The five tests absorb the different-machine test, the
    config-path rule and "if you cannot name a useful future action, file
    nothing". The trigger is still the completion report, but answering it
    out loud is not asked for: "no need to say so". First measured at 2903
    bytes with learnings on and 1759 off, confirm 3627 and 2385.

    Review then found that the fields sentence sent two doors wrong: it named
    `learning` alone, which the helper does not take (its first argument,
    `correction`, is mapped), and it no longer said that the thin body sends
    `learning` INSTEAD of `correction`, which the schema refuses to receive
    together. Saying where the rule goes for each door cost 63 bytes; the
    preview's audience and "qualifying" cost 18 more. Plain words paid for
    them, with no instruction lost: "in Python" for "if your harness runs
    Python", "like" for "for example", "<OMS token>", "the ingest endpoint",
    "<your current skill, if known>" (the same field, as its schema words
    it), and one sentence each for the bundle lookup and the learning
    approval. Measured on 29 September 2026: automatic 2899 bytes with
    learnings on and 1705 off, confirm 3632 on and 2349 off. Confirm with
    learnings on is the largest, so measure all four
    (`test_every_variant_fits_the_budget`) before adding to any.

    Confirm then spent 16 bytes on "a new UUID as its": told only to "give"
    an id, an agent invents a natural one, and another person's agent can
    invent the same. Confirm is now 3648 on and 2365 off. The HTTP example
    still carries no `transaction_id`: the 32 bytes it needs would put
    confirm with learnings on over the budget, and the sentence already
    reaches all three doors.

    Confirm then became one step: one offer, and one action by the person,
    in place of a question, a preview and an approval. The client's own
    prompt is that action only where the agent's instructions say the client
    asks before each call; an installer adds that sentence for Claude Code,
    outside this block, only once it has made Claude Code ask. Everywhere
    else the action is a reply to share. The new flow first measured 3743
    bytes; "editable", "the intended", "from them" and the second telling of
    the approval rule paid for it. Measured on 1 October 2026: confirm 3651
    on and 2368 off."""
    _check_contribution_mode(mode)
    confirm = mode == "confirm"
    lines = [
        "", "# Contributing learnings", "",
        "Record durable organisational or repository guidance a person gives "
        "you: standing preferences, corrections to existing rules, and useful "
        "evidenced lessons. Ask what useful future decision retaining it would "
        "change." + ("" if confirm else
                     " Log qualifying corrections in the same turn, BEFORE you "
                     "acknowledge them as remembered."), "",
    ]
    if confirm:
        # In place of logging in the same turn: one offer, and one action by
        # the person. The question and the two bold choices are exact copy.
        # Where the agent's own instructions say its client asks before each
        # call (an installer writes that only when it made the client ask),
        # the client's prompt is that action; everywhere else it is a reply.
        lines += [
            "Apply a qualifying correction a person gives you to the current "
            "task, then offer it in one message: \"Use this for the team's "
            "<skill> guidance too?\" with the wording, its skill and any context it "
            "shares, and **Share with the team** or **Just this time**. If "
            "your instructions say your client asks the person before each "
            "`log_correction` call, call it right after the offer: their answer "
            "there is the choice. Otherwise call it, or any other door, only once "
            "they say to share. Declining, blocking or silence sends nothing; do "
            "not send it another way or offer it again unless they reopen it. New "
            "wording is a new offer. If they ask to share, skip the "
            "question. Give each shared wording a new UUID `transaction_id`, "
            "reused on a retry. One tool approval does not cover later "
            "suggestions.", "",
        ]
    lines += [
        "Do not submit task or research requests, answer outlines, completion "
        "reports, generic good practice, or coaching for one answer. A user "
        "redirecting the current task has not necessarily stated a standing policy. "
        "Adding 'When asked' does not make it one. Preserve terse policy changes "
        "and genuine corroboration; a rule need not be novel or broadly applicable. "
        "Zero contributions is normal. OMS handles routing and deduplication.", "",
        "IMPORTANT: Do NOT save organisational or team-wide rules to your personal "
        "memory (MEMORY.md), which is for session-specific preferences only. Do "
        "not edit these files; OMS overwrites them on each pull.", "",
        "How to log, in order of preference:", "",
        # Confirm mode says what one approval of the tool does not cover, so
        # only automatic mode tells the agent to give it.
        "1. Call the OMS MCP tool `log_correction`"
        + ("." if confirm else " (approve it once when prompted)."),
        "2. Otherwise, in Python, call "
        "`contribute_learning(correction, skill_hint, ...)` from the bundled "
        "`oms_contribute.py`.",
        "3. Otherwise POST JSON to the ingest endpoint:", "",
        "```http",
        # Its own line: a reader recovers the endpoint of a committed root
        # file with the pattern `^POST (\S+)$`.
        f"POST {endpoint}",
        "Authorization: Bearer <OMS token> (omit behind a trusted gateway)",
        "Content-Type: application/json",
        "",
        '{"correction": "<the rule, in one or two sentences>", '
        '"skill_hint": "<your current skill, if known>"}',
        "```",
        "",
        "When you start work in a git repository, call `query_skill` once with "
        "its normalised remote (like `github.com/acme/payments`). Any bundle "
        "it returns holds rules for that repository only; if there is none, "
        "carry on.",
    ]
    if session_learnings:
        lines += [
            "", "## Session learnings", "",
            # Mind the trailing commas. Two adjacent strings with none between
            # them fold into one element, so a list item merges with the next
            # or the blank line between paragraphs is lost, in every published
            # file.
            "Before reporting work finished, check whether it taught a lesson "
            "worth keeping. Keep one only if the work shows all five:", "",
            "1. a task or condition likely to recur in this project, repository or team;",
            "2. a changed action for a future agent, and the error, waste or risk it avoids;",
            "3. an observation supporting it and its scope;",
            "4. it stays true after this task (not a temporary failure, a fixed "
            "defect or one machine's state);",
            "5. it adds to the skills you read rather than restating generic practice.", "",
            "Write it as a RULE: a condition and an action in one imperative "
            "sentence, like \"In this repo, manage Python deps with uv, "
            "not pip\", not \"This repo uses uv\". Normally there is nothing to "
            "submit, and no need to say so; never file a weak lesson to fill a "
            "quota. At most three per piece of work, ranked; never split one "
            "lesson into several."
            + (" Show the person each proposed learning with its context; "
               "submit only those they approve." if confirm else ""), "",
            # Where the rule goes differs by door. The thin body (MCP
            # `log_correction` and HTTP) refuses a self_reflection with a
            # correction beside it; the helper has no `learning` keyword and
            # maps its first argument; `log_signal` nests the rule.
            "File each separately with the rule in `learning`, not `correction` "
            "(the helper takes it as `correction`; `log_signal` as "
            "`execution_context.learning`), plus `signal_type: \"self_reflection\"`, "
            "`source_ref: \"session:<your session id>\"`, `session_summary`, "
            "`reuse_case` and `learning_evidence` (and `project_name` outside a "
            "repository).",
        ]
    return lines


def _render_cursor_rule(root_md: str) -> str:
    """Wrap the root content as a Cursor always-applied rule (`.mdc` with
    frontmatter). Cursor uses a directory of rules rather than a single root
    file, so the content needs this envelope to be loaded."""
    return ("---\n"
            "description: Organisational skills, constraints, and how to contribute\n"
            "alwaysApply: true\n"
            "---\n\n") + root_md


def _render_windsurf_rule(root_md: str) -> str:
    """Wrap the root content as a Windsurf always-on rule (markdown with an
    activation-mode frontmatter)."""
    return ("---\n"
            "trigger: always_on\n"
            "---\n\n") + root_md


# Frameworks whose root rules are a directory-and-format scheme rather than a
# single markdown file at the tree root. Each entry maps a format name to its
# published path and the wrapper that adapts the shared root content.
ROOT_RULE_FORMATS = {
    "cursor": (".cursor/rules/oms.mdc", _render_cursor_rule),
    "windsurf": (".windsurf/rules/oms.md", _render_windsurf_rule),
}


def render_mcp_json(mcp_url: str, bundle_token: str | None = None) -> str:
    """Project-scoped MCP config for Claude Code (.mcp.json at the published
    root). Pointing at the HTTP endpoint means zero local install: any agent
    that can reach the ingest URL gets log_correction in its tool list, which
    is what makes spontaneous contribution reliable - prose instructions get
    acknowledged, tools get called."""
    server: dict = {"type": "http", "url": mcp_url}
    if bundle_token:
        # §8.2: the bundle carries its own admission. Written into the
        # published tree deliberately - it is a shared low-privilege secret
        # that proves bundle possession, never identity, and whose rotation
        # is a republish, not a personal credential.
        server["headers"] = {"Authorization": f"Bearer {bundle_token}"}
    return json.dumps({"mcpServers": {"oms": server}}, indent=2) + "\n"


def render_cursor_mcp_json(mcp_url: str, bundle_token: str | None = None) -> str:
    """Cursor's variant (.cursor/mcp.json). Cursor infers the transport from
    the url key, so no type field."""
    server: dict = {"url": mcp_url}
    if bundle_token:
        server["headers"] = {"Authorization": f"Bearer {bundle_token}"}
    return json.dumps({"mcpServers": {"oms": server}}, indent=2) + "\n"


def root_import_file(root_files: Sequence[str] | None = None) -> str:
    """Which published root file the installer imports into the user CLAUDE.md.

    One of them, never all: publish writes byte-identical content under every
    configured name, so a second import would only load the constraints and
    the skill index into every agent's context twice.

    CLAUDE.md when publish emits it, because that is the filename Claude Code
    itself uses and the installer's whole target is the Claude user scope.
    Otherwise the first name configured, which is the one an operator who
    narrowed the list to a single filename meant. Falls back to the publish
    default for an empty list, matching `Publisher.__init__`, so the two
    cannot disagree about what "unset" means."""
    names = list(root_files) if root_files else list(DEFAULT_ROOT_INSTRUCTION_FILES)
    return "CLAUDE.md" if "CLAUDE.md" in names else names[0]


def _have(h: Harness) -> str:
    """The shell variable holding whether this harness is installed and usable."""
    return f"HAVE_{h.key.upper()}"


def _detection_blocks() -> str:
    """One detection block per harness, generated from the table rather than
    hand-written five times over: the blocks differ only in their paths, and
    five no-graph copies is how one of them ends up testing a directory
    it does not write to."""
    blocks = []
    for h in HARNESSES:
        tests = []
        if h.cli:
            tests.append(f"command -v {h.cli} >/dev/null 2>&1")
        if h.detect_env:
            tests.append(f'[ -n "${{{h.detect_env}:-}}" ]')
        tests.append(f'[ -d "{h.detect}" ]')
        blocks.append(
            f'{_have(h)}=""\n'
            f'if {" || ".join(tests)}; then\n'
            f'  DETECTED="$DETECTED, {h.label}"\n'
            f'  if harness_ready "{h.label}" "{h.detect}" '
            f'"{h.skills_dir or ""}" "{h.instructions or ""}" "{h.hint or ""}"; then\n'
            f'    {_have(h)}=1\n'
            f'    READY="$READY, {h.label}"\n'
            f'  else\n'
            f'    not_updated "{h.label}"\n'
            f'  fi\n'
            f'fi'
        )
    return "\n".join(blocks)


def _covered_by(h: Harness) -> list[Harness]:
    """Harnesses whose own skills directory this one also reads.

    Where one of them is being installed, linking this harness's directory as
    well would stack a second copy of every skill in front of the same agent,
    and the harnesses that read compatibility paths do not merge them.
    """
    return [o for o in HARNESSES
            if o.key != h.key and o.skills_dir and o.skills_dir in h.also_reads]


def _install_blocks() -> str:
    """The per-harness install, generated from the table for the same reason
    as the detection blocks: five no-graph copies of the same six lines
    is how one of them ends up writing to a path it never checked."""
    blocks = []
    for h in HARNESSES:
        lines = [f'if [ -n "${_have(h)}" ]; then']
        if h.skills_dir:
            covered = _covered_by(h)
            if covered:
                for i, o in enumerate(covered):
                    lines.append(
                        f'  {"if" if i == 0 else "elif"} [ -n "${_have(o)}" ]; then\n'
                        f'    echo "  {h.label}: reads {o.skills_dir} already;'
                        f' no separate copy linked"')
                lines.append(f'  else\n    link_skills "{h.skills_dir}" "{h.label}"\n  fi')
            else:
                lines.append(f'  link_skills "{h.skills_dir}" "{h.label}"')
        if h.overridden_by:
            lines.append(
                f'  if [ -f "{h.overridden_by}" ]; then\n'
                f'    echo "  {h.label}: {h.overridden_by} exists, and is read in"\n'
                f'    echo "    preference to {h.instructions}. What is written there will"\n'
                f'    echo "    NOT load until you merge it into the override file."\n'
                f'  fi')
        if h.instructions:
            lines.append(f'  write_instructions "{h.instructions}" "{h.style}"'
                         f' "{h.label}" "{h.max_chars or ""}"')
        elif h.style == "manual":
            lines.append(f'  stage_manual_rules "{h.label}"'
                         f' "$OMS_DIR/{h.key}-user-rules.md"')
        lines.append("fi")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


# The JSON merge, as a Python program the installer stages and runs.
#
# Merged rather than written, because these files hold servers the user
# configured and replacing one silently disconnects every other tool they
# have. A real parser rather than sh: the alternative is pattern-matching
# JSON in shell, which fails on exactly the hand-edited files this is trying
# not to damage. Values arrive as argv, never interpolated into the source,
# so a URL or token containing a quote cannot rewrite the program.
_MERGE_MCP_JSON = '''\
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
key, url, token = sys.argv[2], sys.argv[3], sys.argv[4]
raw = path.read_text(encoding="utf-8") if path.exists() else ""
try:
    data = json.loads(raw) if raw.strip() else {}
except ValueError:
    raise SystemExit(3)
if not isinstance(data, dict):
    raise SystemExit(3)
servers = data.setdefault("mcpServers", {})
if not isinstance(servers, dict):
    raise SystemExit(3)
entry = {key: url}
if token:
    entry["headers"] = {"Authorization": "Bearer " + token}
servers["oms"] = entry
path.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")
'''

# Shell literal characters that cannot be baked into generated scripts.
_UNBAKEABLE = re.compile(r"""[\s"'`$\\]""")


def validate_script_literals(**values: str | None) -> None:
    """Refuse to render an installer around a value a shell would re-read.

    The staged JSON helper takes its URL and token as argv.
    The same values are also baked as literals into the scripts that
    stage them - `OMS_MCP_TOKEN='...'` in sh, `$OMS_MCP_TOKEN = "..."` in
    PowerShell and URLs in command invocations and echo lines. At that layer
    there is no argv to put them in. This check is
    what makes the two treatments one policy rather than a rule the same file
    breaks a page later: `$(...)` and a backtick are command substitution
    inside double quotes in BOTH dialects, so a stray `$(` in an operator's
    OMS_PUBLIC_URL is not a broken URL, it is a command that runs on every
    machine that installs the bundle and on none that a publisher watches.

    Refused rather than escaped. Escaping would have to be right in four places
    at once - single-quoted sh, double-quoted sh, double-quoted PowerShell, and
    the echo lines that quote a value a second time - and a wrong one is
    invisible until a consumer runs the script, weeks downstream and on
    somebody else's laptop. There is nothing legitimate to escape for either: a
    URL with a space in it is a typo, and a token with a quote in it is a paste
    that brought the surrounding quotes along. Operator-set configuration is
    allowed to fail where it is set, and here that costs one operator one run.
    """
    for name, value in values.items():
        found = _UNBAKEABLE.search(value or "")
        if found:
            # The character, never the value: this message reaches a terminal
            # and a CI log, and one of the three values is a secret.
            raise ValueError(
                f"{name} contains {found.group()!r}, which install.sh and "
                f"install.ps1 would re-read as script rather than carry as "
                f"text. Correct it where it is set (OMS_PUBLIC_URL, "
                f"OMS_BUNDLE_TOKEN) rather than escaping it here."
            )




# Claude Code's own approval for the two contribution tools, in confirm mode.
# The confirm instructions let an agent use its client's prompt as the
# person's one share action only where its instructions say the client asks
# before each call. This is that sentence. Installers write it into Claude
# Code's import block, and nowhere else, only once Claude Code has an `ask`
# rule for both tools, which makes it ask in every permission mode, auto
# mode and "always allow" included. Without the rule the sentence is left
# out and the agent waits for a reply to share: one step slower, never
# unasked.
CLAUDE_ASK_RULES: tuple[str, ...] = ("mcp__oms__log_correction", "mcp__oms__log_signal")
CLAUDE_APPROVAL_NOTE = ("On this machine, Claude Code asks the person before each "
                        "`log_correction` and `log_signal` call.")

# Adds the `ask` rules to the person's own Claude Code settings (argv: the
# settings file, the record of rules this installer added, and "confirm" or
# "automatic"), or, in automatic mode, takes out only the recorded ones: a
# rule the person wrote is theirs. Exit 0 when done, 3 when the file is not
# settings it can change, 4 when it cannot be read or written. Either failure
# leaves the file as it was. Written through a temporary file beside the
# target, which is the link's target where settings.json is a link, so a
# dotfile manager's link survives.
_CLAUDE_ASK_PY = '''\
import json, os, pathlib, sys

RULES = %(rules)r
settings_path = pathlib.Path(sys.argv[1])
record_path = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
try:
    recorded = [r for r in record_path.read_text(encoding="utf-8").split() if r in RULES]
except FileNotFoundError:
    recorded = []
except OSError:
    raise SystemExit(4)
try:
    raw = settings_path.read_text(encoding="utf-8")
except FileNotFoundError:
    raw = ""
except OSError:
    raise SystemExit(4)
try:
    data = json.loads(raw) if raw.strip() else {}
except ValueError:
    raise SystemExit(3)
if not isinstance(data, dict):
    raise SystemExit(3)
permissions = data.get("permissions", {})
if not isinstance(permissions, dict):
    raise SystemExit(3)
ask = permissions.get("ask", [])
if not isinstance(ask, list):
    raise SystemExit(3)
if mode == "confirm":
    added = [r for r in RULES if r not in ask]
    keep = [r for r in RULES if r in recorded or r in added]
    new_ask = ask + added
else:
    keep = []
    new_ask = [r for r in ask if r not in recorded]
if new_ask != ask:
    if new_ask:
        permissions["ask"] = new_ask
    else:
        permissions.pop("ask", None)
    data["permissions"] = permissions
    target = settings_path.resolve()
    tmp = target.with_name(target.name + ".oms.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        raise SystemExit(4)
try:
    if keep:
        record_path.write_text("\\n".join(keep) + "\\n", encoding="utf-8")
    elif record_path.exists():
        record_path.unlink()
except OSError:
    raise SystemExit(4)
if mode != "confirm" and len(new_ask) < len(ask):
    print("  Claude Code: removed the approval rule confirm mode added to %%s" %% settings_path)
''' % {"rules": list(CLAUDE_ASK_RULES)}


def _claude_ask_step(confirm_file: str) -> str:
    """Section 1a of install.sh: Claude Code's `ask` rules in confirm mode, and
    their removal when automatic is chosen again. Built as a plain string, as
    `_mcp_helpers` is, because it carries a Python program.

    Runs after detection, so it knows whether Claude Code is here, and before
    any instructions are written, because `write_instructions` reads
    OMS_CLAUDE_ASKS to decide whether Claude Code is told that it asks. Never
    fatal: a machine where the rule cannot be written still installs, and its
    Claude Code agents ask in the conversation instead."""
    return '''
# 1a. Claude Code's own approval, in confirm mode (see CLAUDE_APPROVAL_NOTE
#     in the renderer). OMS_CLAUDE_ASKS is set only once Claude Code has the
#     `ask` rules, and section 2 tells Claude Code it asks only then.
OMS_CLAUDE_ASKS=""
OMS_ASK_RECORD="$OMS_DIR/claude-ask-rules"
oms_ask_mode=""
if [ "$OMS_CONTRIBUTION_MODE_RESOLVED" = confirm ]; then
  # Only where the confirm instructions are installed: a bundle that takes
  # no contributions installs its one file in both modes, and asks nothing.
  if [ -n "$HAVE_CLAUDE" ] && [ "$OMS_ROOT_FILE" = "''' + confirm_file + '''" ]; then
    oms_ask_mode=confirm
  fi
elif [ -f "$OMS_ASK_RECORD" ]; then
  oms_ask_mode=automatic
fi
if [ -n "$oms_ask_mode" ]; then
  oms_ask_done=""
  if command -v python3 >/dev/null 2>&1; then
    cat > "$OMS_DIR/.claude-ask.py" <<'OMS_ASK_PY'
''' + _CLAUDE_ASK_PY + '''OMS_ASK_PY
    if python3 "$OMS_DIR/.claude-ask.py" "$CLAUDE_DIR/settings.json" "$OMS_ASK_RECORD" "$oms_ask_mode"; then
      oms_ask_done=1
    fi
    rm -f "$OMS_DIR/.claude-ask.py"
  fi
  if [ "$oms_ask_mode" = confirm ]; then
    if [ -n "$oms_ask_done" ]; then
      OMS_CLAUDE_ASKS=1
      echo "  Claude Code: asks the person before each OMS contribution (a permission rule in $CLAUDE_DIR/settings.json)."
    else
      echo "  Claude Code: could not add the approval rule to $CLAUDE_DIR/settings.json"
      echo "    (it needs python3 and a settings file that is a JSON object), so"
      echo "    Claude Code agents will ask in the conversation before sharing."
    fi
  elif [ -z "$oms_ask_done" ]; then
    echo "  Claude Code: could not take the approval rule confirm mode added out of"
    echo "    $CLAUDE_DIR/settings.json. Remove mcp__oms__log_correction and"
    echo "    mcp__oms__log_signal from permissions.ask there if you no longer want it."
  fi
fi
'''


def _mcp_helpers(mcp_url: str, bundle_token: str | None,
                 credential_setup: str | None = None, notice: str = "") -> str:
    """The two global-MCP writers, plus the per-harness calls.

    Built as a plain string rather than inside the installer's f-string: it
    carries a whole Python program and a TOML table, both dense with braces,
    and doubling every one of them by hand is how a generated script acquires
    a syntax error nobody sees until a consumer runs it.

    Credential setup and the optional terminal notice are explicit fragments.
    """
    resolve_token = (_local_credential_block(bundle_token)
                     if credential_setup is None else credential_setup)
    # Names the credential in use without echoing it: this reaches a terminal,
    # and terminals get screenshotted into chat threads.
    # A TOML table appended after everything else. Not parsed and rewritten:
    # writing TOML back out needs a library the installer cannot assume, and
    # this file carries the user's model settings, plugins and stdio servers.
    # The markers make the append idempotent, which matters more here than
    # anywhere else - a SECOND [mcp_servers.oms] table is not a duplicate
    # entry, it stops Codex parsing its configuration at all.
    helpers = f'''
merge_mcp_json() {{
  mm_file="$1"; mm_key="$2"; mm_label="$3"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "  $mm_label: no python3 on this machine, so $mm_file was left alone."
    echo "    Merging JSON without a parser is not worth the risk to a file that"
    echo "    holds your other servers. Add this entry to it by hand:"
    echo '      "oms": {{ "'"$mm_key"'": "{mcp_url}" }}'
    return 0
  fi
  mkdir -p "$(dirname "$mm_file")"
  cat > "$OMS_DIR/.merge-mcp.py" <<'OMS_MERGE_PY'
{_MERGE_MCP_JSON}OMS_MERGE_PY
  if python3 "$OMS_DIR/.merge-mcp.py" "$mm_file" "$mm_key" "{mcp_url}" "$OMS_MCP_TOKEN"; then
    echo "  $mm_label: MCP server 'oms' registered in $mm_file"
  else
    echo "  $mm_label: $mm_file is not JSON this can merge into, so it was left"
    echo "    alone. Add the 'oms' server to it by hand."
  fi
  rm -f "$OMS_DIR/.merge-mcp.py"
}}

write_codex_mcp() {{
  wc_file="$1"; wc_label="$2"
  touch "$wc_file"
  wc_tmp="$wc_file.oms-tmp"
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0 == b {{ skipping = 1 }}
    skipping && $0 == e {{ skipping = 0; next }}
    !skipping {{ print }}
  ' "$wc_file" > "$wc_tmp"
  # Ours is already stripped, so anything left is the user's own, and both
  # ways of handling it are wrong: appending makes the duplicate table that
  # breaks Codex outright, overwriting takes a decision that is theirs.
  if grep -q '^\\[mcp_servers\\.oms\\]' "$wc_tmp"; then
    rm -f "$wc_tmp"
    echo "  $wc_label: $wc_file already has its own [mcp_servers.oms]; left alone."
    # Said, because confirm mode's approval below is not written either, and
    # what Codex does without it is Codex's default, which is not documented.
    if [ "$OMS_CONTRIBUTION_MODE_RESOLVED" = confirm ]; then
      echo "    Confirm mode set no per-tool approval there, so Codex may not ask before a contribution unless you configure it."
    fi
    return 0
  fi
  # Confirm mode also sets Codex's own approval for the two tools that send a
  # contribution: approval_mode "prompt" has Codex ask the person before each
  # call, and their answer is the final confirmation of the preview they were
  # shown. Codex documents the key and its values without defining what each
  # does, so this is configured, not proven. Automatic mode writes none, and
  # the block is rewritten whole on every run, so choosing automatic again
  # takes them out. A table the user wrote for one of these tools stays
  # theirs, for the reason above: a second copy would stop Codex reading the
  # file at all.
  wc_ask=""
  if [ "$OMS_CONTRIBUTION_MODE_RESOLVED" = confirm ]; then
    for wc_tool in log_correction log_signal; do
      if grep -q "^\\[mcp_servers\\.oms\\.tools\\.$wc_tool\\]" "$wc_tmp"; then
        echo "  $wc_label: $wc_file already has its own [mcp_servers.oms.tools.$wc_tool];"
        echo "    that table, not confirm mode, decides whether Codex asks before $wc_tool."
      else
        wc_ask="$wc_ask $wc_tool"
      fi
    done
  fi
  {{
    cat "$wc_tmp"
    printf '%s\\n' "$BEGIN_MARK"
    printf '%s\\n' "[mcp_servers.oms]"
    printf '%s\\n' 'url = "{mcp_url}"'
    if [ -n "$OMS_MCP_TOKEN" ]; then
      printf '%s\\n' "http_headers = {{ Authorization = \\"Bearer $OMS_MCP_TOKEN\\" }}"
    fi
    for wc_tool in $wc_ask; do
      printf '%s\\n' "[mcp_servers.oms.tools.$wc_tool]" 'approval_mode = "prompt"'
    done
    printf '%s\\n' "$END_MARK"
  }} > "$wc_file"
  rm -f "$wc_tmp"
  echo "  $wc_label: MCP server 'oms' registered in $wc_file"
}}
'''
    calls = []
    for h in HARNESSES:
        if not h.mcp_config:
            continue
        call = (f'  merge_mcp_json "{h.mcp_config}" "{h.mcp_url_key}" "{h.label}"'
                if h.mcp_format == "json"
                else f'  write_codex_mcp "{h.mcp_config}" "{h.label}"')
        calls.append(f'if [ -n "${_have(h)}" ]; then\n{call}\nfi')
    return (resolve_token + helpers + "\n" + "\n".join(calls) + "\n"
            + notice)










def _toml_header_printf(toml_headers: str) -> str:
    """The http_headers line, emitted only when a bundle token exists."""
    if not toml_headers:
        return ""
    line = toml_headers.strip()
    return f"\n    printf '%s\\n' '{line}'"


def render_install_script(mcp_url: str | None,
                          root_files: Sequence[str] | None = None,
                          bundle_token: str | None = None,
                          *, fragments: ShellInstallFragments | None = None) -> str:
    """Render the shared POSIX installer with explicit trusted extension slots."""
    validate_script_literals(mcp_url=mcp_url, bundle_token=bundle_token)
    fragments = fragments or ShellInstallFragments()
    import_file = root_import_file(root_files)
    # Computed here and baked in: the script cannot ask Python which name
    # publish gave the confirm copy, and asking the same function publish
    # asked is what keeps the two names one.
    confirm_file = confirm_variant_name(import_file)
    detection = _detection_blocks()
    install_blocks = _install_blocks()
    before_links = fragments.before_links
    claude_ask_step = _claude_ask_step(confirm_file)
    claude_note = CLAUDE_APPROVAL_NOTE
    # Resolved at run time, by section 0, to the root file of the mode this
    # installation chose, so every harness below reads the same variant.
    root_md = fragments.root_source or "$SRC/$OMS_ROOT_FILE"
    link_initialiser = fragments.link_initialiser
    skip_skill = fragments.skip_skill
    link_report = fragments.link_report
    refresh_ok = fragments.refresh_success
    have_any = "".join(f"${_have(h)}" for h in HARNESSES)
    looked_for = " ".join(h.detect for h in HARNESSES)
    mcp_step = ""
    if mcp_url:
        # Claude Code is the only harness registered through a CLI rather than
        # a config file this installer writes, so it is the only one whose
        # credential has to be passed as an argument. Without it Claude Code
        # is the single harness that presents nothing, which is invisible
        # while the read tools admit anyone and total the moment they stop.
        # "$OMS_MCP_TOKEN", not the baked literal: the same resolved value
        # every other harness gets, so an enrolled machine's Claude Code
        # registration is attributable too. Guarded at run time because the
        # value can be empty on a deployment that gates nothing.
        claude_header = ' --header "Authorization: Bearer $OMS_MCP_TOKEN"'
        # The advice a failure prints names where the token lives instead of
        # echoing it: this reaches a terminal, and terminals get screenshotted
        # into chat threads. `.mcp.json` is in the bundle beside this script,
        # so the pointer is true on every machine that ran the installer.
        # Both the quotes and the dollar are escaped for the generated script,
        # not for Python: this text ends up inside `echo "..."`, where a bare
        # inner quote would close the string and a bare $TOKEN would expand -
        # to nothing under `set -u`, which aborts the installer outright.
        by_hand = (f'    claude mcp add --scope user --transport http oms {mcp_url}'
                   + (r' --header \"Authorization: Bearer \$TOKEN\"'
                      if bundle_token else ""))
        token_hint = (f'\n  echo "  (the token is the Authorization value in $SRC/.mcp.json)"'
                      if bundle_token else "")
        mcp_step = f'''
# 3. The log_correction / log_signal tools, user-scoped: present in every
#    project's tool list, which is what makes spontaneous contribution work.
#    Claude Code has a CLI for this; the rest have one global config file
#    each, in three different shapes, and every one of those files holds
#    servers the user configured.
{_mcp_helpers(mcp_url, bundle_token, fragments.credential_setup, fragments.credential_notice)}
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user oms >/dev/null 2>&1 || true
  # Tested with `if`, not run bare, for the same reason as the crontab write
  # in section 4: a claude CLI whose flags have drifted since this bundle was
  # published exits non-zero, and under `set -e` that aborts the installer
  # HERE - before the refresh script is written, before anything is scheduled,
  # before the status file exists, and before "Done." prints. The machine
  # would then never refresh and would look, to the person who pasted one
  # line into their shell, as though nothing had worked at all. This is the
  # primary path, not a cron edge case: `claude` is certainly on PATH there.
  if claude mcp add --scope user --transport http oms "{mcp_url}"{claude_header}; then
    echo "  registered MCP server 'oms' -> {mcp_url}"
  else
    echo "  could not register the MCP server 'oms' with this claude CLI."
    echo "  Everything else is installed. Register it by hand with:"
    echo "{by_hand}"{token_hint}
  fi
else
  echo "  claude CLI not found; register the tool later with:"
  echo "{by_hand}"{token_hint}
fi
'''
    return f'''#!/bin/sh
# OMS machine bootstrap (generated by OMS at publish time). Run once per
# machine; safe to re-run. Wires the published skills, instructions and
# contribution tool into the user scope so EVERY project picks them up,
# with no per-project setup.
set -eu

SRC="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_REVISION="$(cat "$SRC/.oms-publication" 2>/dev/null || true)"
CLAUDE_DIR="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
# OMS's own directory, alongside the credentials file enrolment writes. The
# refresh script and the staleness notice live here rather than inside any one
# harness's configuration: they belong to no harness, they must survive a
# machine that has no Claude Code at all, and every harness has to be able to
# read the status file. This is the one directory the installer creates
# unconditionally, because it is the only one that is OMS's to create.
OMS_DIR="$HOME/.oms"
BEGIN_MARK="# >>> oms (generated; do not edit between markers) >>>"
END_MARK="# <<< oms <<<"

echo "Installing OMS from $SRC"

# 0. Preconditions. Whether a write can happen is settled before it is
#    attempted, rather than being guarded command by command, because a write
#    that fails part way through fails in two different and equally bad ways.
#    `set -e` aborts on a simple command whose redirect fails but NOT on a
#    compound one, so section 2's `{{ ...; }} > "$CLAUDE_DIR/CLAUDE.md"` against
#    a read-only file left bash printing "instructions imported into ..." and
#    then "Done. ... are live." while the marker block - and the status import
#    inside it, the one channel that tells the human anything is wrong - was
#    never written. dash aborted at the same redirect instead, after every skill
#    had been linked and before anything was scheduled. Neither state is one the
#    installer can report on honestly, so the only good outcome is to not enter
#    it. Section 0b applies this per harness.
abort() {{
  echo "install.sh: $1" >&2
  echo "  Nothing has been installed. Fix that (ls -l shows the owner and mode)," >&2
  echo "  then re-run: sh '$SRC/install.sh'" >&2
  exit 1
}}
# Section 2 creates each of these when absent and rewrites it when present, so
# both get one test: whatever is already at the path must be a regular file
# this user can write. `-e` alone is not that test, and both ways it is wrong
# were reachable. It follows symlinks, so it answers FALSE for a link whose
# target is missing or sits behind a directory that cannot be traversed (how a
# dotfile manager leaves a machine), and the deferred write then failed with
# the skills already linked. And it answers TRUE for a DIRECTORY of that name,
# which sailed through into the compound redirect and back out to
# "Done. ... are live." with the import block unwritten - failure mode one,
# intact, past the guard meant to end it. `-L` closes the first, `-f` the
# second. Absent stays absent: that is every first run.
writable_target() {{
  if [ -e "$1" ] || [ -L "$1" ]; then
    [ -f "$1" ] && [ -w "$1" ]
  fi
}}
# The directory counterpart, for each harness's skills directory, and absent
# stays absent for the same reason: that is every first run, and section 1
# creates it. `-w` alone is not the test either - a directory has to be entered as well
# as written, and `ln` resolves the link path through it before it creates
# anything, so one missing the execute bit fails the write while answering TRUE
# to `-w`. `-d` is what rejects a plain file, and a symlink to one.
writable_dir() {{
  if [ -e "$1" ] || [ -L "$1" ]; then
    [ -d "$1" ] && [ -w "$1" ] && [ -x "$1" ]
  fi
}}
# The import written in section 2 points into this bundle, and an import of a
# path that does not exist resolves to nothing WITHOUT complaining: the agent
# loads no constraints and no skill index while the installer says "instructions
# imported" and exits 0. The name comes from the same configured list publish
# writes, so this cannot fire on a legal configuration - only on a bundle that
# arrived incomplete, which is the half a derived name cannot cover.
[ -f "$SRC/{import_file}" ] ||
  abort "$SRC/{import_file} is missing, so this bundle has no instructions to import."
[ ! -f "$SRC/.oms-publishing" ] ||
  abort "a publication is still in progress; retry when publishing has completed."
# The contribution mode: whether this installation's agents share a correction
# a person gives them by themselves ("automatic") or ask that person first and
# share only a preview they approve ("confirm"). The bundle carries root files
# for both, so the choice is the installation's: OMS_CONTRIBUTION_MODE when it
# is set and not empty, else the choice this machine recorded on its last run,
# which is how the refresh (run without the variable) keeps it, else automatic.
# Settled here, before anything is written, so a refused value changes nothing.
OMS_MODE_FILE="$OMS_DIR/contribution-mode"
# The choice the last run recorded. With no record the machine was automatic,
# because an installation nobody configured is automatic, and so was
# everything installed before the mode was recorded.
oms_mode_recorded=""
oms_mode_chosen=automatic
if [ -e "$OMS_MODE_FILE" ] || [ -L "$OMS_MODE_FILE" ]; then
  oms_mode_recorded=1
  oms_mode_chosen="$(cat "$OMS_MODE_FILE" 2>/dev/null | tr -d '[:space:]')"
fi
# What the tools were last given, read whatever this run chooses: a change of
# mode is news to anyone who copied the previous mode's rules by hand, and to
# a tool whose rules this run cannot rewrite. A second record, written at the
# end of a run, where the choice above is written before any tool: a run that
# stops part way leaves the tools' last mode here, so the next run still sees
# the change. A machine installed before this record existed was given the
# mode it chose.
OMS_INSTALLED_MODE_FILE="$OMS_DIR/contribution-mode-installed"
oms_mode_before="$oms_mode_chosen"
if [ -e "$OMS_INSTALLED_MODE_FILE" ] || [ -L "$OMS_INSTALLED_MODE_FILE" ]; then
  oms_mode_before="$(cat "$OMS_INSTALLED_MODE_FILE" 2>/dev/null | tr -d '[:space:]')"
fi
oms_mode_from=""
if [ -n "${{OMS_CONTRIBUTION_MODE:-}}" ]; then
  OMS_CONTRIBUTION_MODE_RESOLVED="$OMS_CONTRIBUTION_MODE"
elif [ -n "$oms_mode_recorded" ]; then
  # A record that is empty or unreadable is not a choice of automatic. It is
  # refused below like any other value, rather than quietly switching a
  # machine that chose confirm to sharing without asking.
  OMS_CONTRIBUTION_MODE_RESOLVED="$oms_mode_chosen"
  oms_mode_from="$OMS_MODE_FILE"
else
  OMS_CONTRIBUTION_MODE_RESOLVED=automatic
fi
# OMS_ROOT_FILE names the root file, inside the bundle, that every harness is
# given: the one the import above checked, or its confirm copy.
case "$OMS_CONTRIBUTION_MODE_RESOLVED" in
  automatic) OMS_ROOT_FILE="{import_file}" ;;
  confirm) OMS_ROOT_FILE="{confirm_file}" ;;
  *)
    echo "OMS_CONTRIBUTION_MODE must be automatic or confirm" >&2
    [ -z "$oms_mode_from" ] ||
      echo "  The value was read from $oms_mode_from, where this machine records its choice." >&2
    # Both ways out, confirm first. A record that cannot be read may well
    # have said confirm, and going back to automatic is a choice to make on
    # purpose, never the repair for a damaged record.
    echo "  Nothing has been installed. Re-run with the mode you mean:" >&2
    echo "    OMS_CONTRIBUTION_MODE=confirm sh '$SRC/install.sh'" >&2
    echo "    OMS_CONTRIBUTION_MODE=automatic sh '$SRC/install.sh'" >&2
    exit 1
    ;;
esac
# Never the automatic file instead: that would share corrections without
# asking the person who chose to be asked. Only choosing automatic again,
# explicitly, installs it. A bundle that takes no contributions is not that
# case: with no contribution block its root file says the same in both modes,
# so publish writes no confirm copy, and publishing again could not make one.
# Its one file is installed and the machine keeps confirm, so the refresh
# after a publication that does take contributions installs the confirm copy.
if [ "$OMS_CONTRIBUTION_MODE_RESOLVED" = confirm ] && [ ! -f "$SRC/$OMS_ROOT_FILE" ]; then
  if grep -qxF '# Contributing learnings' "$SRC/{import_file}"; then
    echo "This bundle has no confirm-mode instructions. Ask your OMS administrator to publish again." >&2
    echo "  Nothing has been installed. To install without the agent asking before it" >&2
    echo "  shares a correction, re-run with OMS_CONTRIBUTION_MODE=automatic." >&2
    exit 1
  fi
  OMS_ROOT_FILE="{import_file}"
  echo "This bundle takes no contributions, so both modes install the same instructions."
  echo "  Confirm mode is kept for when it does."
fi
# OMS's own directory is not a harness's, so its failure is fatal rather than a
# skip: the refresh script and the staleness notice go here, and without them
# no harness on this machine would ever update or be able to say that it had not.
mkdir -p "$OMS_DIR" ||
  abort "cannot create $OMS_DIR, and the refresh script and status file go there."
writable_dir "$OMS_DIR" ||
  abort "$OMS_DIR is not a writable directory, and the refresh script goes there."
# Every run touches this, not only the first: a root-run install that leaves it
# owned by somebody else breaks every later refresh.
writable_target "$OMS_DIR/status.md" ||
  abort "$OMS_DIR/status.md is not a writable file, and the staleness notices go there."
writable_target "$OMS_MODE_FILE" ||
  abort "$OMS_MODE_FILE is not a writable file, and this machine's contribution mode is recorded there."
writable_target "$OMS_INSTALLED_MODE_FILE" ||
  abort "$OMS_INSTALLED_MODE_FILE is not a writable file, and the mode this machine's tools were given is recorded there."

# 0b. Which agent tools are on this machine, and can each one be written to.
#     Detection is the tool's own config directory, or its CLI on PATH where
#     the command name is known - the directory is created by the tool's first
#     run rather than by its installation, so directory alone would skip a
#     freshly installed Claude Code. Nothing is ever created for a tool that
#     is absent: a ~/.gemini conjured on a machine with no Antigravity is
#     litter, and every later run would read it back as proof the tool is there.
#
#     The all-or-nothing contract above is kept, but per harness. Every path a
#     harness will be written to is tested before any of them is written, so no
#     harness is ever left half-installed; and a harness that fails is skipped
#     alone, because one tool with a read-only config directory must not cost
#     the machine every other tool's install.
# To stderr: a skipped harness is a degraded install, and on a machine with
# only that one tool it is the whole reason the run is about to fail. The
# fatal message below points back at these rather than restating them.
skip() {{
  echo "  skipped $1: $2" >&2
  [ -z "${{3:-}}" ] || echo "    $3" >&2
}}
DETECTED=""
READY=""
# The tools this run could not bring up to date: skipped here, or left without
# instructions in section 2. Each keeps whatever OMS instructions an earlier run
# gave it, or none, which after a change of mode is the other mode's, so the
# summary names them and never counts them as configured. Taking one out of
# READY matches whole ", "-separated entries; the labels come from the harness
# table and hold no pattern characters.
NOT_UPDATED=""
not_updated() {{
  NOT_UPDATED="$NOT_UPDATED, $1"
  case "$READY, " in
    *", $1, "*)
      nu_list="$READY, "
      READY="${{nu_list%%, $1, *}}, ${{nu_list#*, $1, }}"
      READY="${{READY%, }}"
      ;;
  esac
}}
harness_ready() {{
  hr_label="$1"; hr_root="$2"; hr_skills="$3"; hr_instr="$4"; hr_hint="${{5:-}}"
  # mkdir's own stderr is kept, not suppressed: it separates a permission
  # problem from a path with a plain file somewhere along it, and those need
  # different remedies.
  if ! mkdir -p "$hr_root"; then
    skip "$hr_label" "cannot create $hr_root" "$hr_hint"
    return 1
  fi
  if ! writable_dir "$hr_root"; then
    skip "$hr_label" "$hr_root is not writable, and everything for this tool goes there" "$hr_hint"
    return 1
  fi
  # The skills directory is the FIRST thing this installer writes into, and it
  # once sat outside the contract entirely. At mode 0555 `ln -sfn` failed with a
  # bare "ln: Permission denied"; as a plain file of that name, `mkdir -p`
  # failed with a bare "mkdir: File exists". Each rc=1 on sh, dash and bash
  # alike, and each with none of the guidance below. Tested and not created
  # here, so a skip still leaves the directory as it was found.
  if [ -n "$hr_skills" ] && ! writable_dir "$hr_skills"; then
    skip "$hr_label" "$hr_skills is not a writable directory, and every skill is linked there" "$hr_hint"
    return 1
  fi
  if [ -n "$hr_instr" ] && ! writable_target "$hr_instr"; then
    skip "$hr_label" "$hr_instr is not a writable file, and the instructions go there" "$hr_hint"
    return 1
  fi
  return 0
}}
{detection}
# Two different failures, and they need different people doing different
# things: a machine with no agent tool on it, and a machine whose agent tools
# are all unwritable. Reporting the second as the first sent somebody looking
# for software they had already installed.
if [ -z "{have_any}" ]; then
  if [ -z "$DETECTED" ]; then
    echo "install.sh: no supported agent tool found on this machine." >&2
    echo "  Looked for: {looked_for}" >&2
    echo "  Nothing has been installed. Install one of those, then re-run:" >&2
  else
    echo "install.sh: every agent tool on this machine was skipped." >&2
    echo "  Found: ${{DETECTED#, }}" >&2
    echo "  Each reason is above; ls -l shows the owner and mode." >&2
    echo "  Nothing has been installed. Fix that, then re-run:" >&2
  fi
  echo "    sh '$SRC/install.sh'" >&2
  exit 1
fi

# Staleness channel: the refresh script writes here when a pull fails, and
# every harness is pointed at it below, so a stale bundle reaches the human
# through the agent rather than dying in cron's output. Empty means healthy.
# Created (not truncated) so no pointer is ever dangling and so a notice
# already raised survives a hand-run of this installer - only a successful
# pull is evidence that the bundle is current.
touch "$OMS_DIR/status.md"
# The mode, recorded for the refresh, which re-runs this installer without
# OMS_CONTRIBUTION_MODE: the machine keeps the variant it was installed with
# until somebody chooses again. One word on one line, so `cat` answers which.
printf '%s\\n' "$OMS_CONTRIBUTION_MODE_RESOLVED" > "$OMS_MODE_FILE"
{claude_ask_step}{before_links}
# 1. Skills into the user scope. Symlinks, so the daily pull updates them in
#    place. A real directory with the same name belongs to the user: skip it.
#    First sweep OMS-owned garbage: a symlink pointing into this tree that no
#    longer resolves is a skill OMS retired (pruned upstream, landed via the
#    pull). Anything not pointing into $SRC is the user's and stays.
#    (This mkdir cannot fail: section 0b settled both that the harness root is
#    writable and that nothing unusable is sitting at this path already.)
link_skills() {{
  ls_dir="$1"; ls_label="$2"; ls_n=0{link_initialiser}
  mkdir -p "$ls_dir"
  for link in "$ls_dir"/*; do
    [ -L "$link" ] || continue
    case "$(readlink "$link")" in
      "$SRC"/*)
        if [ ! -e "$link" ]; then
          rm -f "$link"
          echo "  removed retired skill $(basename "$link")"
        fi
        ;;
    esac
  done
  for dir in "$SRC"/skills/*/; do
    [ -d "$dir" ] || continue
    name="$(basename "$dir")"
    target="$ls_dir/$name"
{skip_skill}    if [ -e "$target" ] && [ ! -L "$target" ]; then
      echo "  skip skills/$name: you already have a directory with this name"
      continue
    fi
    ln -sfn "${{dir%/}}" "$target"
    ls_n=$((ls_n + 1))
  done
{link_report}
}}

# 2. Instructions, in whichever form the harness reads.
#
#    import - a reference to the bundle file. The daily pull then refreshes the
#             content without this file ever changing again. Claude Code's
#             `@path` syntax, and nothing else supports it: emitted anywhere
#             else it is a literal line starting with @, and no constraints
#             load at all.
#    copy   - the content itself. Kept current by the daily re-run of this
#             installer rather than by the harness, and pointed at the status
#             file rather than importing it.
#
#    Everything outside the markers is the user's and is preserved verbatim.
write_instructions() {{
  wi_file="$1"; wi_style="$2"; wi_label="$3"; wi_cap="$4"
  # Windsurf caps global_rules.md, and truncating organisational constraints
  # to fit is not an available option: what would be dropped is unknowable
  # from here, and the agent would report a full set of rules either way.
  if [ -n "$wi_cap" ]; then
    wi_size=$(wc -c < "{root_md}" | tr -d ' ')
    if [ "$wi_size" -gt "$wi_cap" ]; then
      echo "  $wi_label: instructions NOT written. {root_md} is"
      echo "    $wi_size characters and $wi_file caps at $wi_cap. The skills above"
      echo "    are installed; the organisational constraints are not. Ask whoever"
      echo "    administers OMS to shorten them."
      # After a change of mode, what the last run left between the markers
      # is the other mode's, and kept it would go on loading while this run
      # reports the new one: a confirm machine sharing without asking, or the
      # reverse. So it goes, and the tool is not counted as configured. The
      # record read before this run says which mode that last run installed.
      # In an unchanged mode nothing here changes: an installation that
      # stays automatic, or was never configured, behaves exactly as it did
      # before modes existed, and its block is only an older publication's.
      if [ "$oms_mode_before" != "$OMS_CONTRIBUTION_MODE_RESOLVED" ]; then
        if [ -f "$wi_file" ] && grep -qxF "$BEGIN_MARK" "$wi_file"; then
          wi_tmp="$wi_file.oms-tmp"
          awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
            $0 == b {{ skipping = 1 }}
            skipping && $0 == e {{ skipping = 0; next }}
            !skipping {{ print }}
          ' "$wi_file" > "$wi_tmp"
          cat "$wi_tmp" > "$wi_file"
          rm -f "$wi_tmp"
          echo "  $wi_label: removed the OMS instructions an earlier run left in $wi_file." >&2
          echo "    $wi_label has no OMS instructions until they fit, rather than another mode's." >&2
        fi
        not_updated "$wi_label"
      fi
      return 0
    fi
  fi
  mkdir -p "$(dirname "$wi_file")"
  touch "$wi_file"
  # Staged next to the target (not mktemp): same filesystem, and it works in
  # sandboxes where the system temp directory is off-limits.
  wi_tmp="$wi_file.oms-tmp"
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0 == b {{ skipping = 1 }}
    skipping && $0 == e {{ skipping = 0; next }}
    !skipping {{ print }}
  ' "$wi_file" > "$wi_tmp"
  {{
    cat "$wi_tmp"
    printf '%s\\n' "$BEGIN_MARK"
    if [ "$wi_style" = import ]; then
      printf '%s\\n' "@{root_md}"
      printf '%s\\n' "@$OMS_DIR/status.md"
      if [ -n "$OMS_CLAUDE_ASKS" ]; then
        printf '%s\\n' '{claude_note}'
      fi
    else
      cat "{root_md}"
      printf '\\n%s\\n' "If the user asks whether their organisational skills are current, read $OMS_DIR/status.md. Empty means healthy."
    fi
    printf '%s\\n' "$END_MARK"
  }} > "$wi_file"
  rm -f "$wi_tmp"
  echo "  $wi_label: instructions imported into $wi_file"
}}

# The manual style, for a harness whose global instructions are not a file at
# all. Cursor's User Rules are plain text in its settings, and its AGENTS.md
# support is project-scoped, so there is nothing here to write and nothing to
# schedule: the most the installer can honestly do is stage the exact text and
# name the destination. Staged in OMS's own directory, overwritten every run,
# so the copy a user pastes from is never a version behind the bundle.
stage_manual_rules() {{
  sm_label="$1"; sm_file="$2"
  # Staged here before means it may have been pasted. After a change of mode
  # that paste is the other mode's rules, and the tool goes on loading them
  # until the person replaces them, so this run says so instead of "once".
  sm_staged=""
  if [ -f "$sm_file" ]; then sm_staged=1; fi
  cp "{root_md}" "$sm_file"
  if [ -n "$sm_staged" ] && [ "$oms_mode_before" != "$OMS_CONTRIBUTION_MODE_RESOLVED" ]; then
    echo "  $sm_label: skills are linked, but $sm_label has no global instructions"
    echo "    file, and these rules changed with the contribution mode. Paste this"
    echo "    into $sm_label Settings > Rules again, replacing the rules you pasted"
    echo "    before:"
    echo "      $sm_file"
  else
    echo "  $sm_label: skills are linked, but $sm_label has no global instructions"
    echo "    file. Paste this into $sm_label Settings > Rules, once, and it applies"
    echo "    to every project:"
    echo "      $sm_file"
  fi
}}
{install_blocks}{mcp_step}
# 3b. Remove the old session hook, if this machine has one.
#     It ran on every prompt to record that a session was alive, so
#     reflection could tell a finished session from a live one. The
#     transcript's own modification time answers that better: it moves on
#     every event, so it sees an agent working through a long task, where
#     the hook recorded only PROMPTS and so read that same session as quiet
#     and let reflection take it mid-work.
#
#     So nothing is registered here any more. This strips ours and leaves
#     the user's own hooks alone, which is how a machine that installed the
#     hook stops running it: on its next daily refresh, with nothing to do
#     by hand. The block goes entirely once no installed machine carries one.
#
#     Failure is reported and stepped over, never fatal. A malformed
#     settings.json is the user's file and not ours to repair, and section 4
#     still has to run.
if command -v python3 >/dev/null 2>&1; then
  if python3 - "$CLAUDE_DIR/settings.json" <<'OMS_HOOK_PY'
import json, os, sys

settings_path = sys.argv[1]

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
    if not isinstance(settings, dict):
        raise ValueError("not a JSON object")
except FileNotFoundError:
    settings = {{}}
except (ValueError, OSError) as exc:
    # Reported, not repaired, and certainly not replaced: this file is the
    # user's and may configure far more than us.
    print("  could not read %s (%s)." % (settings_path, exc))
    print("  The session hook is installed but not registered; reflection")
    print("  will need --all-transcripts until it is.")
    sys.exit(1)

hooks = settings.setdefault("hooks", {{}})
if not isinstance(hooks, dict):
    print("  'hooks' in %s is not an object; leaving it alone." % settings_path)
    sys.exit(1)
matchers = hooks.setdefault("UserPromptSubmit", [])
if not isinstance(matchers, list):
    print("  'hooks.UserPromptSubmit' is not a list; leaving it alone.")
    sys.exit(1)


def ours(entry):
    """One of ours, by name rather than by exact command.

    A bundle installed under a different path, or an older one that named the
    file differently, is then REPLACED rather than left beside the new entry
    firing twice on every prompt. Hooks the user owns match neither test and
    survive untouched.
    """
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        if isinstance(hook, dict) and "oms_session_hook" in str(hook.get("command", "")):
            return True
    return False


before = len(matchers)
matchers[:] = [m for m in matchers if not ours(m)]
if before == len(matchers):
    # Nothing of ours in here, so nothing to rewrite. The common case on a
    # machine that never had the hook, and on every run after the first.
    sys.exit(0)

os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
# Written beside the target and renamed, so an install interrupted here cannot
# leave the user with a half-written settings.json and a broken harness.
tmp = settings_path + ".oms.tmp"
with open(tmp, "w") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\\n")
os.replace(tmp, settings_path)
print("  removed the old session hook from %s" % settings_path)
OMS_HOOK_PY
  then :; else
    echo "  could not read $CLAUDE_DIR/settings.json; left it alone."
  fi
fi

# 4. Daily refresh. Pulls, then re-runs this (idempotent) installer so new
#    skills get linked and retired ones unlinked without any user action, and
#    records a staleness notice when the pull fails instead of swallowing it.
#    The work lives in a generated script rather than inline in the crontab
#    entry: cron rewrites a bare '%' as a newline, so the date format below
#    would corrupt the entry. A separate script is also directly runnable,
#    which is how it is tested. It is written unconditionally: a machine with
#    no crontab, or a tree that is not a clone, still needs the status file
#    the import above points at, and still needs a way to refresh by hand.
#    Staged and renamed rather than written in place: the refresh script
#    re-runs this installer, so this rewrite normally happens while the file
#    being rewritten is the one the calling shell is still reading. sh keeps a
#    byte offset into that file and re-seeks after each command, so truncating
#    it makes the shell resume at that offset inside different content. That
#    has been observed executing a fragment of the staleness printf below,
#    writing a false alarm into the one channel this exists to keep credible.
#    A rename leaves the running shell holding the old inode until it exits.
local_refresh=0
if [ ! -e "$SRC/.git" ] && [ -f "$SRC/.oms-publication" ]; then local_refresh=1; fi
cat > "$OMS_DIR/.oms-refresh.sh.tmp" <<REFRESH
#!/bin/sh
# Generated by OMS install.sh. Pulls the published bundle and re-runs the
# installer, or records why it could not.
set -u
SRC="$SRC"
STATUS="$OMS_DIR/status.md"
if [ "$local_refresh" = 1 ]; then
  if [ ! -f "\\$SRC/.oms-publication" ]; then
    printf '%s\\n' "The local OMS publication folder is unavailable. Restore \\$SRC or run install.sh from its new location." > "\\$STATUS"
    exit 1
  fi
  # Publication writes the revision last. A failed/incomplete write must not
  # remove agent links or replace copied instructions with a partial tree.
  [ ! -f "\\$SRC/.oms-publishing" ] || exit 0
  revision=\\$(cat "\\$SRC/.oms-publication")
  [ "\\$revision" != "\\$(cat "$OMS_DIR/local-revision" 2>/dev/null)" ] || exit 0
  if OMS_REFRESHING=1 sh "\\$SRC/install.sh" >/dev/null 2>&1; then
{refresh_ok}
  else
    printf '%s\\n' "OMS could not install the latest local publication. Run sh \\$SRC/install.sh to inspect the error." > "\\$STATUS"
    exit 1
  fi
  exit 0
fi
if cd "\\$SRC" && git pull --ff-only >/dev/null 2>&1; then
  # A pull that lands bytes nobody wires in is not a successful refresh: the
  # new skills sit in \\$SRC unlinked and the machine runs a version behind.
  # So the status file is cleared only once the re-install has also succeeded,
  # and the failure gets its own notice - a broken installer and an expired
  # token need different people doing different things.
  if sh "\\$SRC/install.sh" >/dev/null 2>&1; then
{refresh_ok}
  else
    printf '%s\\n' "**The organisational skills on this machine were downloaded but not installed.**" > "\\$STATUS"
    printf '%s\\n' "The update on \\$(date +%Y-%m-%d) pulled new content, then the installer failed, so what is loaded here is the previous version." >> "\\$STATUS"
    printf '%s\\n' "Tell the user their OMS skills did not install, and that running 'sh \\$SRC/install.sh' in a terminal will show the error to send to whoever administers OMS." >> "\\$STATUS"
  fi
else
  printf '%s\\n' "**The organisational skills on this machine are out of date.**" > "\\$STATUS"
  printf '%s\\n' "The daily update failed on \\$(date +%Y-%m-%d). The most likely cause is an expired access token." >> "\\$STATUS"
  printf '%s\\n' "Tell the user their OMS skills are stale, that the most likely cause is an expired access token, and that the install line in the bundle's README.md is what re-clones it once they have access again." >> "\\$STATUS"
fi
REFRESH
chmod +x "$OMS_DIR/.oms-refresh.sh.tmp"
# Same directory as the target, so this is a rename and not a copy.
mv "$OMS_DIR/.oms-refresh.sh.tmp" "$OMS_DIR/oms-refresh.sh"

# Scheduling is the conditional half: only a clone has anything to pull, and
# only a machine with crontab can be given a nightly slot. Every branch that
# does not schedule says so and names the manual alternative, because a bundle
# that silently never refreshes is the failure this whole file exists to avoid.
if [ -n "$INSTALL_REVISION" ] && [ ! -e "$SRC/.git" ]; then
  printf '%s\\n' "$INSTALL_REVISION" > "$OMS_DIR/local-revision"
fi
local_plist="$HOME/Library/LaunchAgents/ai.inogen.oms.local-refresh.plist"
if [ -e "$SRC/.git" ] && [ -f "$local_plist" ]; then
  # Changing from local publication to Git must retire the minute-based job
  # before the shared refresh script becomes a daily Git updater.
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$local_plist" >/dev/null 2>&1 || true
  fi
  rm -f "$local_plist"
fi
if [ "${{OMS_REFRESHING:-}}" = 1 ]; then
  : # A scheduled run only reconciles files; it never re-registers itself.
elif [ ! -e "$SRC/.git" ] && [ -f "$SRC/.oms-publication" ]; then
  # macOS launch agents do not require the terminal's Full Disk Access for
  # crontab. Other Unix systems use the same one-minute check through cron.
  local_scheduled=0
  if command -v launchctl >/dev/null 2>&1 && [ "$(uname -s)" = Darwin ]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    plist="$HOME/Library/LaunchAgents/ai.inogen.oms.local-refresh.plist"
    escaped_refresh=$(printf '%s' "$OMS_DIR/oms-refresh.sh" | sed -e 's/&/\\&amp;/g' -e 's/</\\&lt;/g' -e 's/>/\\&gt;/g')
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>ai.inogen.oms.local-refresh</string>
<key>ProgramArguments</key><array><string>/bin/sh</string><string>$escaped_refresh</string></array>
<key>StartInterval</key><integer>60</integer>
<key>RunAtLoad</key><true/>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$(id -u)" "$plist"; then local_scheduled=1; fi
  fi
  if [ "$local_scheduled" = 0 ] && command -v crontab >/dev/null 2>&1; then
    LINE="* * * * * sh '$OMS_DIR/oms-refresh.sh' >/dev/null 2>&1 # oms-local-refresh"
    if (crontab -l 2>/dev/null | grep -v -e '# oms-local-refresh' -e '# oms-daily-pull' || true; echo "$LINE") | crontab -; then
      local_scheduled=1
    fi
  fi
  if [ "$local_scheduled" = 1 ]; then
    : > "$OMS_DIR/status.md"
    echo "  local refresh scheduled: completed publications reach installed agents within one minute."
  else
    echo "  automatic local refresh could not be scheduled."
    echo "  Update by hand with: sh '$OMS_DIR/oms-refresh.sh'"
    printf '%s\\n' "Automatic local OMS refresh is not scheduled. Run the bundle installer in a terminal to enable it." > "$OMS_DIR/status.md"
  fi
elif [ ! -d "$SRC/.git" ]; then
  echo "  no daily refresh: this tree is not a git clone, so there is nothing to pull."
  echo "  To update, copy in a newer bundle and re-run install.sh. From a clone,"
  echo "  'sh $OMS_DIR/oms-refresh.sh' would do it for you."
elif ! command -v crontab >/dev/null 2>&1; then
  echo "  no daily refresh: this machine has no crontab command."
  echo "  Update by hand any time with: sh '$OMS_DIR/oms-refresh.sh'"
else
  LINE="@daily sh '$OMS_DIR/oms-refresh.sh' >/dev/null 2>&1 # oms-daily-pull"
  # The `|| true` is load-bearing under `set -e`: grep exits 1 when it selects
  # nothing, which is the normal case on a machine whose only crontab entry is
  # ours (or which has no crontab at all). Without it the subshell dies before
  # `echo "$LINE"`, and `crontab -` installs an EMPTY crontab - so the refresh
  # would unschedule itself on its own second run.
  #
  # Tested with `if`, not run bare, and that is the load-bearing part: stock
  # macOS refuses `crontab -` until the calling terminal has Full Disk Access,
  # and under `set -e` a bare pipeline would abort the installer with rc=1 and
  # no "Done." line AFTER every piece of real work had already succeeded. An
  # `if` condition is exempt from errexit, so the refusal is reported instead.
  if (crontab -l 2>/dev/null | grep -v -e "# oms-daily-pull" -e '# oms-local-refresh' || true; echo "$LINE") | crontab -; then
    echo "  daily refresh scheduled"
  else
    echo "  no daily refresh: crontab refused to install the entry."
    echo "  On macOS this usually means the terminal needs Full Disk Access"
    echo "  (System Settings > Privacy & Security > Full Disk Access)."
    echo "  Update by hand any time with: sh '$OMS_DIR/oms-refresh.sh'"
  fi
fi

# Built from what was actually configured, not asserted about the machine. The
# fixed line this replaces - "the skills, instructions and contribution tool
# are live" - was one claim on a Claude-only machine and five on this one, of
# which any number could have been skipped a few lines above.
#
# The mode goes to the terminal, not into status.md: that file is the
# staleness channel every harness is pointed at, where empty means healthy,
# and the refresh empties it on every success. A tool that was not brought up
# to date is named on stderr, like the reasons it points back to, and is never
# in the "Configured" list, which can then be empty: Windsurf alone, with a
# root file over its cap, has its skills but no instructions.
#
# Every tool step is behind us, so the tools now hold this run's mode, or are
# named below as not updated: recorded as what they were given (section 0
# says why this is a second record).
printf '%s\\n' "$OMS_CONTRIBUTION_MODE_RESOLVED" > "$OMS_INSTALLED_MODE_FILE"
echo "Contribution mode: $OMS_CONTRIBUTION_MODE_RESOLVED"
if [ -n "$NOT_UPDATED" ]; then
  echo "Not updated: ${{NOT_UPDATED#, }}. The reasons are above." >&2
  echo "  Until each is fixed, that tool keeps an earlier run's OMS instructions, or none." >&2
fi
if [ -n "$READY" ]; then
  echo "Done. Configured: ${{READY#, }}. Open any project and the skills are live."
else
  echo "Done, with no agent tool fully configured. The reasons are above."
fi
'''


# --- The Windows installer -------------------------------------------------
#
# install.sh covers macOS and Linux, both verified. Native Windows has none of
# what it needs: no sh, no awk, no wc, no crontab, and `ln -s` requires
# Developer Mode or an elevated prompt. So the same contract is rendered a
# second time, in the one language a stock Windows machine already has.
#
# Three substitutions carry the contract across, and each is why a step below
# does not simply mirror its shell counterpart:
#
#   symlink  -> directory junction. A junction needs no elevation and no
#               Developer Mode, and skills are always directories, so this is
#               the one link type every Windows user can create. On PowerShell
#               Core elsewhere it falls back to a symlink, which is also what
#               makes this script testable off Windows.
#   crontab  -> a registered scheduled task, where the ScheduledTasks module
#               exists. Absent (PowerShell Core on Linux or macOS), the branch
#               reports itself exactly as the no-crontab branch does in sh.
#   python3  -> ConvertFrom-Json. The shell installer stages a Python program
#               because merging JSON in sh is not safe; PowerShell parses JSON
#               itself, so the whole "no python3 on this machine" degradation
#               has no counterpart here.
#
# Written as a plain string with @@TOKEN@@ placeholders rather than an f-string
# like its shell sibling. PowerShell is made of braces, and an f-string would
# require doubling every one of them - several hundred edits whose only failure
# mode is a script that renders but cannot parse.
_PS1_TEMPLATE = r'''#Requires -Version 5.1
# OMS machine bootstrap (generated by OMS at publish time). Run once per
# machine; safe to re-run. Wires the published skills, instructions and
# contribution tool into the user scope so EVERY project picks them up, with
# no per-project setup. The PowerShell twin of install.sh; run that one on
# macOS and Linux.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$SRC = Split-Path -Parent $MyInvocation.MyCommand.Path
# $HOME is PowerShell's own: USERPROFILE on Windows, HOME elsewhere. Forward
# slashes are accepted by every path API on Windows, so the harness table's
# paths carry over from install.sh unchanged rather than being rewritten - one
# table, two installers, no chance of them disagreeing about where Codex lives.
$CLAUDE_DIR = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$HOME/.claude" }
$OMS_DIR = "$HOME/.oms"
$BEGIN_MARK = "# >>> oms (generated; do not edit between markers) >>>"
$END_MARK = "# <<< oms <<<"
# Not $IsWindows: that automatic variable does not exist on PowerShell 5.1,
# which is the version every stock Windows 10 and 11 machine has, and reading
# it under Set-StrictMode is an error rather than a false.
$OnWindows = ($env:OS -eq 'Windows_NT')

# Every file this script writes goes through here, and never through
# Set-Content. Windows PowerShell 5.1 writes a BOM for -Encoding UTF8, and a
# BOM at the head of CLAUDE.md puts three bytes in front of the begin marker:
# the next run no longer matches it, appends a second block, and the file
# grows one copy of the organisational constraints per install.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-TextFile($Path, $Text) {
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}
function Read-TextFile($Path) {
    if (Test-Path -LiteralPath $Path) {
        $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
        if ($null -ne $text) { return $text }
    }
    return ""
}
function Write-Err($Text) { [Console]::Error.WriteLine($Text) }

Write-Host "Installing OMS from $SRC"

# 0. Preconditions. Whether a write can happen is settled before it is
#    attempted, rather than guarded command by command, because a write that
#    fails part way through leaves a machine this script cannot describe
#    honestly: some skills linked, no instructions, and a "Done." line.
function Abort($Reason) {
    Write-Err "install.ps1: $Reason"
    Write-Err "  Nothing has been installed. Fix that, then re-run:"
    Write-Err "    powershell -ExecutionPolicy Bypass -File '$SRC\install.ps1'"
    exit 1
}
# Absent stays absent: that is every first run, and the caller creates it.
# Writability is settled by attempting a write, not by reading an ACL. On
# Windows the effective permission is the resolved sum of inherited and
# explicit entries for every group the user is in, and a script that tries to
# compute that gets it wrong on exactly the locked-down machines where the
# answer matters.
function Test-WritableDir($Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { return $false }
    $probe = "$Path/.oms-write-probe-$PID"
    try {
        [System.IO.File]::WriteAllText($probe, "", $Utf8NoBom)
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        return $true
    } catch { return $false }
}
function Test-WritableFile($Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $item = Get-Item -LiteralPath $Path -Force
    # A DIRECTORY of this name is the case that has to be rejected rather than
    # opened: it is not writable as a file, and every later step would report
    # a success that never happened.
    if ($item.PSIsContainer) { return $false }
    try {
        $stream = [System.IO.File]::Open($item.FullName, 'Open', 'Write')
        $stream.Close()
        return $true
    } catch { return $false }
}

# The import written below points into this bundle, and an import of a missing
# path resolves to nothing WITHOUT complaining: the agent loads no constraints
# and no skill index while this script says "instructions imported" and exits 0.
if (-not (Test-Path -LiteralPath "$SRC/@@IMPORT_FILE@@")) {
    Abort "$SRC/@@IMPORT_FILE@@ is missing, so this bundle has no instructions to import."
}
# The contribution mode, settled before anything is written (install.sh gives
# the reasons): OMS_CONTRIBUTION_MODE when it is set and not empty, else the
# choice this machine recorded on its last run, else automatic. Compared
# case-sensitively, as sh does, so one value means the same on every machine.
$OmsModeFile = "$OMS_DIR/contribution-mode"
# The choice the last run recorded. With no record the machine was automatic.
$OmsModeRecorded = Test-Path -LiteralPath $OmsModeFile
$OmsModeChosen = if ($OmsModeRecorded) { (Read-TextFile $OmsModeFile) -replace '\s', '' } else { 'automatic' }
# What the tools were last given, read whatever this run chooses: recorded at
# the end of a run, where the choice is recorded before any tool, so a run
# that stops part way leaves a change for the next run to see (install.sh
# gives the reasons). A machine installed before this record existed was
# given the mode it chose.
$OmsInstalledModeFile = "$OMS_DIR/contribution-mode-installed"
$OmsModeBefore = if (Test-Path -LiteralPath $OmsInstalledModeFile) { (Read-TextFile $OmsInstalledModeFile) -replace '\s', '' } else { $OmsModeChosen }
$OmsModeFrom = ''
if ($env:OMS_CONTRIBUTION_MODE) {
    $OmsContributionMode = $env:OMS_CONTRIBUTION_MODE
} elseif ($OmsModeRecorded) {
    # An empty or unreadable record is refused below, never read as automatic.
    $OmsContributionMode = $OmsModeChosen
    $OmsModeFrom = $OmsModeFile
} else {
    $OmsContributionMode = 'automatic'
}
if ($OmsContributionMode -cne 'automatic' -and $OmsContributionMode -cne 'confirm') {
    Write-Err "OMS_CONTRIBUTION_MODE must be automatic or confirm"
    if ($OmsModeFrom) {
        Write-Err "  The value was read from $OmsModeFrom, where this machine records its choice."
    }
    # Both ways out, confirm first: automatic is a choice, never the repair
    # for a record that cannot be read.
    Write-Err "  Nothing has been installed. Re-run with the mode you mean:"
    Write-Err "    `$env:OMS_CONTRIBUTION_MODE = 'confirm'; powershell -ExecutionPolicy Bypass -File '$SRC\install.ps1'"
    Write-Err "    `$env:OMS_CONTRIBUTION_MODE = 'automatic'; powershell -ExecutionPolicy Bypass -File '$SRC\install.ps1'"
    exit 1
}
# The root file, inside the bundle, that every harness is given. Never the
# automatic one in confirm mode: only choosing automatic again installs it.
# Except on a bundle that takes no contributions, whose one file has no
# contribution block and says the same in both modes (install.sh says why).
$OmsRootFile = if ($OmsContributionMode -ceq 'confirm') { '@@CONFIRM_FILE@@' } else { '@@IMPORT_FILE@@' }
if ($OmsContributionMode -ceq 'confirm' -and -not (Test-Path -LiteralPath "$SRC/$OmsRootFile")) {
    if (((Read-TextFile "$SRC/@@IMPORT_FILE@@") -split "`r?`n") -ccontains '# Contributing learnings') {
        Write-Err "This bundle has no confirm-mode instructions. Ask your OMS administrator to publish again."
        Write-Err "  Nothing has been installed. To install without the agent asking before it"
        Write-Err "  shares a correction, re-run with OMS_CONTRIBUTION_MODE=automatic."
        exit 1
    }
    $OmsRootFile = '@@IMPORT_FILE@@'
    Write-Host "This bundle takes no contributions, so both modes install the same instructions."
    Write-Host "  Confirm mode is kept for when it does."
}
# OMS's own directory is not a harness's, so its failure is fatal rather than a
# skip: the refresh script and the staleness notice go here, and without them
# no harness on this machine would ever update or be able to say that it had not.
try { New-Item -ItemType Directory -Path $OMS_DIR -Force | Out-Null }
catch { Abort "cannot create $OMS_DIR, and the refresh script and status file go there." }
if (-not (Test-WritableDir $OMS_DIR)) {
    Abort "$OMS_DIR is not a writable directory, and the refresh script goes there."
}
if (-not (Test-WritableFile "$OMS_DIR/status.md")) {
    Abort "$OMS_DIR/status.md is not a writable file, and the staleness notices go there."
}
if (-not (Test-WritableFile $OmsModeFile)) {
    Abort "$OmsModeFile is not a writable file, and this machine's contribution mode is recorded there."
}
if (-not (Test-WritableFile $OmsInstalledModeFile)) {
    Abort "$OmsInstalledModeFile is not a writable file, and the mode this machine's tools were given is recorded there."
}

# 0b. Which agent tools are on this machine, and can each one be written to.
#     Nothing is ever created for a tool that is absent: a ~/.gemini conjured
#     on a machine with no Antigravity is litter, and every later run would
#     read it back as proof the tool is there.
$DETECTED = @()
$READY = @()
# The tools this run could not bring up to date (install.sh gives the
# reasons): named in the summary and never counted as configured.
$NOT_UPDATED = @()
function Add-NotUpdated($Label) {
    $script:NOT_UPDATED += $Label
    $script:READY = @($script:READY | Where-Object { $_ -cne $Label })
}
function Skip-Harness($Label, $Reason, $Hint) {
    Write-Err "  skipped ${Label}: $Reason"
    if ($Hint) { Write-Err "    $Hint" }
}
function Test-HarnessReady($Label, $Root, $SkillsDir, $Instructions, $Hint) {
    try { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
    catch { Skip-Harness $Label "cannot create $Root" $Hint; return $false }
    if (-not (Test-WritableDir $Root)) {
        Skip-Harness $Label "$Root is not writable, and everything for this tool goes there" $Hint
        return $false
    }
    # Tested and not created here, so a skip leaves the directory as it was found.
    if ($SkillsDir -and -not (Test-WritableDir $SkillsDir)) {
        Skip-Harness $Label "$SkillsDir is not a writable directory, and every skill is linked there" $Hint
        return $false
    }
    if ($Instructions -and -not (Test-WritableFile $Instructions)) {
        Skip-Harness $Label "$Instructions is not a writable file, and the instructions go there" $Hint
        return $false
    }
    return $true
}

@@DETECTION@@

# Two different failures needing two different people: a machine with no agent
# tool on it, and a machine whose agent tools are all unwritable. Reporting the
# second as the first sends somebody looking for software they already have.
if ($READY.Count -eq 0) {
    if ($DETECTED.Count -eq 0) {
        Write-Err "install.ps1: no supported agent tool found on this machine."
        Write-Err "  Looked for: @@LOOKED_FOR@@"
        Write-Err "  Nothing has been installed. Install one of those, then re-run:"
    } else {
        Write-Err "install.ps1: every agent tool on this machine was skipped."
        Write-Err ("  Found: " + ($DETECTED -join ', '))
        Write-Err "  Each reason is above."
        Write-Err "  Nothing has been installed. Fix that, then re-run:"
    }
    Write-Err "    powershell -ExecutionPolicy Bypass -File '$SRC\install.ps1'"
    exit 1
}

# Staleness channel: the refresh task writes here when a pull fails, and every
# harness is pointed at it below, so a stale bundle reaches the human through
# the agent rather than dying in the task scheduler's history. Empty means
# healthy. Created, never truncated: a notice already raised survives a
# hand-run of this installer, because only a successful pull is evidence that
# the bundle is current.
if (-not (Test-Path -LiteralPath "$OMS_DIR/status.md")) {
    Write-TextFile "$OMS_DIR/status.md" ""
}
# The mode, recorded for the refresh, which re-runs this installer without
# OMS_CONTRIBUTION_MODE, so the machine keeps it until somebody chooses again.
Write-TextFile $OmsModeFile "$OmsContributionMode`n"
# 1a. Claude Code's own approval, in confirm mode (install.sh gives the
#     reasons). $OmsClaudeAsks is set only once Claude Code has the `ask`
#     rules, and Write-Instructions tells Claude Code it asks only then.
#     Never fatal: without the rules its agents ask in the conversation.
$OmsAskRules = @('mcp__oms__log_correction', 'mcp__oms__log_signal')
$OmsAskRecord = "$OMS_DIR/claude-ask-rules"
# $true once the person's Claude Code settings say what $Mode wants: the
# rules added in confirm mode (and the ones added recorded), or the recorded
# ones taken out in automatic mode, never a rule the person wrote. $false,
# with the file as it was, for settings that are not a JSON object or whose
# permissions or ask list are not what Claude Code writes.
function Update-ClaudeAskRules($Mode) {
    $path = Join-Path $CLAUDE_DIR 'settings.json'
    try {
        $recorded = @()
        if (Test-Path -LiteralPath $OmsAskRecord) {
            $recorded = @(((Read-TextFile $OmsAskRecord) -split '\s+') | Where-Object { $OmsAskRules -ccontains $_ })
        }
        $raw = Read-TextFile $path
        if ($raw.Trim()) { $settings = ConvertFrom-Json -InputObject $raw } else { $settings = [pscustomobject]@{} }
        if ($settings -isnot [System.Management.Automation.PSCustomObject]) { return $false }
        $permsProp = $settings.PSObject.Properties['permissions']
        if ($permsProp) {
            $perms = $permsProp.Value
            if ($perms -isnot [System.Management.Automation.PSCustomObject]) { return $false }
        } else {
            $perms = [pscustomobject]@{}
        }
        $askProp = $perms.PSObject.Properties['ask']
        $ask = @()
        if ($askProp) {
            if ($askProp.Value -isnot [array]) { return $false }
            $ask = @($askProp.Value)
        }
        if ($Mode -ceq 'confirm') {
            $added = @($OmsAskRules | Where-Object { $ask -cnotcontains $_ })
            $keep = @($OmsAskRules | Where-Object { ($recorded -ccontains $_) -or ($added -ccontains $_) })
            $newAsk = @($ask + $added)
        } else {
            $keep = @()
            $newAsk = @($ask | Where-Object { $recorded -cnotcontains $_ })
        }
        if ($newAsk.Count -ne $ask.Count) {
            if ($newAsk.Count -gt 0) {
                if ($askProp) { $perms.ask = $newAsk }
                else { $perms | Add-Member -NotePropertyName 'ask' -NotePropertyValue $newAsk }
            } else {
                $perms.PSObject.Properties.Remove('ask')
            }
            if (-not $permsProp) { $settings | Add-Member -NotePropertyName 'permissions' -NotePropertyValue $perms }
            # Through a file beside the target, so an install interrupted here
            # cannot leave Claude Code a half-written settings file.
            $tmp = "$path.oms.tmp"
            Write-TextFile $tmp ((ConvertTo-Json -InputObject $settings -Depth 100) + "`n")
            Move-Item -LiteralPath $tmp -Destination $path -Force
        }
        if ($keep.Count -gt 0) { Write-TextFile $OmsAskRecord (($keep -join "`n") + "`n") }
        elseif (Test-Path -LiteralPath $OmsAskRecord) { Remove-Item -LiteralPath $OmsAskRecord -Force }
        if ($Mode -cne 'confirm' -and $newAsk.Count -lt $ask.Count) {
            Write-Host "  Claude Code: removed the approval rule confirm mode added to $path"
        }
        return $true
    } catch {
        return $false
    }
}
$OmsClaudeAsks = $false
$OmsAskMode = $null
if ($OmsContributionMode -ceq 'confirm') {
    if ($HAVE_CLAUDE -and $OmsRootFile -ceq '@@CONFIRM_FILE@@') { $OmsAskMode = 'confirm' }
} elseif (Test-Path -LiteralPath $OmsAskRecord) {
    $OmsAskMode = 'automatic'
}
if ($OmsAskMode) {
    $OmsAskDone = Update-ClaudeAskRules $OmsAskMode
    if ($OmsAskMode -ceq 'confirm') {
        if ($OmsAskDone) {
            $OmsClaudeAsks = $true
            Write-Host "  Claude Code: asks the person before each OMS contribution (a permission rule in $CLAUDE_DIR/settings.json)."
        } else {
            Write-Host "  Claude Code: could not add the approval rule to $CLAUDE_DIR/settings.json"
            Write-Host "    (it needs a settings file that is a JSON object), so"
            Write-Host "    Claude Code agents will ask in the conversation before sharing."
        }
    } elseif (-not $OmsAskDone) {
        Write-Host "  Claude Code: could not take the approval rule confirm mode added out of"
        Write-Host "    $CLAUDE_DIR/settings.json. Remove mcp__oms__log_correction and"
        Write-Host "    mcp__oms__log_signal from permissions.ask there if you no longer want it."
    }
}
@@BEFORE_LINKS@@
# 1. Skills into the user scope, as links, so the daily pull updates them in
#    place. A real directory with the same name belongs to the user: skip it.
function New-SkillLink($LinkPath, $TargetPath) {
    if (Test-Path -LiteralPath $LinkPath) {
        # -Recurse would delete THROUGH a link into the bundle it points at.
        # Removing the link itself is what is wanted, and what this does.
        (Get-Item -LiteralPath $LinkPath -Force).Delete()
    }
    # Junction, not SymbolicLink: creating a symbolic link on Windows needs
    # Developer Mode or an elevated prompt, and asking every person in the
    # organisation to enable one or run the other is not an install step. A
    # junction needs neither, and works for directories, which every skill is.
    $type = if ($OnWindows) { 'Junction' } else { 'SymbolicLink' }
    New-Item -ItemType $type -Path $LinkPath -Value $TargetPath | Out-Null
}
function Install-Skills($SkillsDir, $Label) {
    New-Item -ItemType Directory -Path $SkillsDir -Force | Out-Null
    # Sweep OMS-owned garbage first: a link into this tree that no longer
    # resolves is a skill OMS retired upstream and the pull removed. Anything
    # not pointing into $SRC is the user's and stays.
    foreach ($entry in Get-ChildItem -LiteralPath $SkillsDir -Force -ErrorAction SilentlyContinue) {
        if (-not $entry.LinkType) { continue }
        $target = @($entry.Target)[0]
        if ($target -and $target.StartsWith($SRC) -and -not (Test-Path -LiteralPath $target)) {
            $entry.Delete()
            Write-Host "  removed retired skill $($entry.Name)"
        }
    }
    $n = 0@@LINK_INIT@@
    foreach ($dir in Get-ChildItem -LiteralPath "$SRC/skills" -Directory -ErrorAction SilentlyContinue) {
        $link = "$SkillsDir/$($dir.Name)"
@@SKIP_SKILL@@        if ((Test-Path -LiteralPath $link) -and -not (Get-Item -LiteralPath $link -Force).LinkType) {
            Write-Host "  skip skills/$($dir.Name): you already have a directory with this name"
            continue
        }
        New-SkillLink $link $dir.FullName
        $n++
    }
@@LINK_REPORT@@
}

# 2. Instructions, in whichever form the harness reads.
#
#    import - a reference to the bundle file, so the daily pull refreshes the
#             content without this file changing again. Claude Code's `@path`
#             syntax, and nothing else supports it: emitted anywhere else it is
#             a literal line starting with @, and no constraints load at all.
#    copy   - the content itself, kept current by the daily re-run.
#
#    Everything outside the markers is the user's and is preserved verbatim.
function Remove-OmsBlock($Text) {
    $kept = @()
    $skipping = $false
    # Single-quoted, so the regex engine gets \r?\n rather than PowerShell
    # first expanding those escapes into the control characters themselves.
    # Both happen to match, and only one of them says so to the next reader.
    foreach ($line in ($Text -split '\r?\n')) {
        if ($line -eq $BEGIN_MARK) { $skipping = $true; continue }
        if ($skipping) {
            if ($line -eq $END_MARK) { $skipping = $false }
            continue
        }
        $kept += $line
    }
    return ($kept -join "`n")
}
function Write-Instructions($File, $Style, $Label, $Cap) {
    # Windsurf caps global_rules.md, and truncating organisational constraints
    # to fit is not an available option: what would be dropped is unknowable
    # from here, and the agent would report a full set of rules either way.
    $content = Read-TextFile "@@ROOT_MD@@"
    if ($Cap -gt 0 -and $content.Length -gt $Cap) {
        Write-Host "  ${Label}: instructions NOT written. @@ROOT_MD@@ is"
        Write-Host "    $($content.Length) characters and $File caps at $Cap. The skills above"
        Write-Host "    are installed; the organisational constraints are not. Ask whoever"
        Write-Host "    administers OMS to shorten them."
        # After a change of mode only, what the last run left between the
        # markers is the other mode's, so it goes and the tool is not counted
        # as configured (install.sh gives the reasons). In an unchanged mode
        # this path is as it always was.
        if ($OmsModeBefore -cne $OmsContributionMode) {
            $existing = Read-TextFile $File
            if (($existing -split '\r?\n') -ccontains $BEGIN_MARK) {
                $body = (Remove-OmsBlock $existing).TrimEnd()
                if ($body) { $body += "`n" }
                Write-TextFile $File $body
                Write-Err "  ${Label}: removed the OMS instructions an earlier run left in $File."
                Write-Err "    $Label has no OMS instructions until they fit, rather than another mode's."
            }
            Add-NotUpdated $Label
        }
        return
    }
    $kept = Remove-OmsBlock (Read-TextFile $File)
    $block = if ($Style -eq 'import') {
        # The line in single quotes: in double quotes a backtick is an escape.
        "@@ROOT_IMPORT@@`n@$OMS_DIR/status.md" + $(if ($OmsClaudeAsks) { "`n" + '@@CLAUDE_NOTE@@' } else { '' })
    } else {
        $content.TrimEnd() + "`n`nIf the user asks whether their organisational skills are current, read $OMS_DIR/status.md. Empty means healthy."
    }
    $body = $kept.TrimEnd()
    if ($body) { $body += "`n" }
    Write-TextFile $File ($body + $BEGIN_MARK + "`n" + $block + "`n" + $END_MARK + "`n")
    Write-Host "  ${Label}: instructions imported into $File"
}

# The manual style, for a harness whose global instructions are not a file at
# all. Cursor's User Rules are plain text in its settings, so the most this can
# honestly do is stage the exact text and name the destination. Overwritten
# every run, so the copy a user pastes from is never a version behind.
function Set-ManualRules($Label, $File) {
    # Staged here before means it may have been pasted, and after a change of
    # mode that paste is the other mode's rules (install.sh gives the reasons).
    $staged = Test-Path -LiteralPath $File
    Write-TextFile $File (Read-TextFile "@@ROOT_MD@@")
    if ($staged -and $OmsModeBefore -cne $OmsContributionMode) {
        Write-Host "  ${Label}: skills are linked, but $Label has no global instructions"
        Write-Host "    file, and these rules changed with the contribution mode. Paste this"
        Write-Host "    into $Label Settings > Rules again, replacing the rules you pasted"
        Write-Host "    before:"
        Write-Host "      $File"
    } else {
        Write-Host "  ${Label}: skills are linked, but $Label has no global instructions"
        Write-Host "    file. Paste this into $Label Settings > Rules, once, and it applies"
        Write-Host "    to every project:"
        Write-Host "      $File"
    }
}

@@INSTALL@@
@@MCP@@
# 4. Daily refresh. Pulls, then re-runs this (idempotent) installer so new
#    skills get linked and retired ones unlinked without any user action, and
#    records a staleness notice when the pull fails instead of swallowing it.
#    Written unconditionally: a machine with no scheduler, or a tree that is
#    not a clone, still needs the status file the import above points at, and
#    still needs a way to refresh by hand.
$refresh = @"
# Generated by OMS install.ps1. Pulls the published bundle and re-runs the
# installer, or records why it could not.
`$src = '$SRC'
`$status = '$OMS_DIR/status.md'
`$utf8 = New-Object System.Text.UTF8Encoding(`$false)
Set-Location -LiteralPath `$src
& git pull --ff-only 2>&1 | Out-Null
if (`$LASTEXITCODE -eq 0) {
    # A pull that lands bytes nobody wires in is not a successful refresh: the
    # new skills sit unlinked and the machine runs a version behind. So the
    # status file is cleared only once the re-install has also succeeded.
    & powershell -ExecutionPolicy Bypass -File "`$src/install.ps1" 2>&1 | Out-Null
    if (`$LASTEXITCODE -eq 0) {
@@REFRESH_OK@@
    } else {
        [System.IO.File]::WriteAllText(`$status, "**The organisational skills on this machine were downloaded but not installed.**``n" +
            "The update on `$(Get-Date -Format yyyy-MM-dd) pulled new content, then the installer failed, so what is loaded here is the previous version.``n" +
            "Tell the user their OMS skills did not install, and that running install.ps1 in a terminal will show the error to send to whoever administers OMS.``n", `$utf8)
    }
} else {
    [System.IO.File]::WriteAllText(`$status, "**The organisational skills on this machine are out of date.**``n" +
        "The daily update failed on `$(Get-Date -Format yyyy-MM-dd). The most likely cause is an expired access token.``n" +
        "Tell the user their OMS skills are stale, that the most likely cause is an expired access token, and that the install line in the bundle's README.md is what re-clones it once they have access again.``n", `$utf8)
}
"@
Write-TextFile "$OMS_DIR/oms-refresh.ps1" $refresh

# Scheduling is the conditional half: only a clone has anything to pull, and
# only a machine with the ScheduledTasks module can be given a nightly slot.
# Every branch that does not schedule says so and names the manual alternative,
# because a bundle that silently never refreshes is the failure this whole file
# exists to avoid.
if (-not (Test-Path -LiteralPath "$SRC/.git")) {
    Write-Host "  no daily refresh: this tree is not a git clone, so there is nothing to pull."
    Write-Host "  To update, copy in a newer bundle and re-run install.ps1. From a clone,"
    Write-Host "  '$OMS_DIR/oms-refresh.ps1' would do it for you."
} elseif (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    Write-Host "  no daily refresh: this PowerShell has no scheduled-task support."
    Write-Host "  Update by hand any time with: $OMS_DIR/oms-refresh.ps1"
} else {
    # Unregistered first, then registered: -Force alone updates a task whose
    # trigger has drifted, but leaves one registered under an older name.
    try {
        Unregister-ScheduledTask -TaskName 'OMS daily refresh' -Confirm:$false -ErrorAction SilentlyContinue
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$OMS_DIR/oms-refresh.ps1`""
        $trigger = New-ScheduledTaskTrigger -Daily -At 9am
        # Interactive token, not SYSTEM: the task writes into this user's
        # profile and pulls with this user's git credentials, neither of which
        # SYSTEM has.
        Register-ScheduledTask -TaskName 'OMS daily refresh' -Action $action `
            -Trigger $trigger -Description 'Pulls the OMS skills bundle and re-runs its installer.' | Out-Null
        Write-Host "  daily refresh scheduled"
    } catch {
        Write-Host "  no daily refresh: registering the scheduled task failed."
        Write-Host "  $($_.Exception.Message)"
        Write-Host "  Update by hand any time with: $OMS_DIR/oms-refresh.ps1"
    }
}

# Built from what was actually configured, not asserted about the machine.
# The mode goes to the terminal, not into status.md, where empty means healthy,
# and a tool that was not brought up to date is named, never counted.
# Every tool step is behind us, so this is what the tools were given.
Write-TextFile $OmsInstalledModeFile "$OmsContributionMode`n"
Write-Host "Contribution mode: $OmsContributionMode"
if ($NOT_UPDATED.Count -gt 0) {
    Write-Err ("Not updated: " + ($NOT_UPDATED -join ', ') + ". The reasons are above.")
    Write-Err "  Until each is fixed, that tool keeps an earlier run's OMS instructions, or none."
}
if ($READY.Count -gt 0) {
    Write-Host ("Done. Configured: " + ($READY -join ', ') + ". Open any project and the skills are live.")
} else {
    Write-Host "Done, with no agent tool fully configured. The reasons are above."
}
'''


def _ps_detection_blocks() -> str:
    """One detection block per harness, generated from the same table the
    shell installer reads, for the same reason: the blocks differ only in
    their paths, and no-graph copies is how one ends up testing a
    directory it does not write to."""
    blocks = []
    for h in HARNESSES:
        tests = []
        if h.cli:
            tests.append(f"(Get-Command {h.cli} -ErrorAction SilentlyContinue)")
        if h.detect_env:
            tests.append(f"$env:{h.detect_env}")
        tests.append(f'(Test-Path -LiteralPath "{h.detect}")')
        blocks.append(
            f'${_have(h)} = $false\n'
            f'if ({" -or ".join(tests)}) {{\n'
            f'    $DETECTED += "{h.label}"\n'
            f'    if (Test-HarnessReady "{h.label}" "{h.detect}" '
            f'"{h.skills_dir or ""}" "{h.instructions or ""}" "{h.hint or ""}") {{\n'
            f'        ${_have(h)} = $true\n'
            f'        $READY += "{h.label}"\n'
            f'    }} else {{\n'
            f'        Add-NotUpdated "{h.label}"\n'
            f'    }}\n'
            f'}}'
        )
    return "\n".join(blocks)


def _ps_install_blocks() -> str:
    """The per-harness install, generated from the table for the same reason."""
    blocks = []
    for h in HARNESSES:
        lines = [f'if (${_have(h)}) {{']
        if h.skills_dir:
            covered = _covered_by(h)
            if covered:
                for i, o in enumerate(covered):
                    kw = "if" if i == 0 else "} elseif"
                    lines.append(
                        f'    {kw} (${_have(o)}) {{\n'
                        f'        Write-Host "  {h.label}: reads {o.skills_dir} '
                        f'already; no separate copy linked"')
                lines.append(f'    }} else {{\n'
                             f'        Install-Skills "{h.skills_dir}" "{h.label}"\n'
                             f'    }}')
            else:
                lines.append(f'    Install-Skills "{h.skills_dir}" "{h.label}"')
        if h.overridden_by:
            lines.append(
                f'    if (Test-Path -LiteralPath "{h.overridden_by}") {{\n'
                f'        Write-Host "  {h.label}: {h.overridden_by} exists, and is read in"\n'
                f'        Write-Host "    preference to {h.instructions}. What is written there will"\n'
                f'        Write-Host "    NOT load until you merge it into the override file."\n'
                f'    }}')
        if h.instructions:
            lines.append(f'    Write-Instructions "{h.instructions}" "{h.style}" '
                         f'"{h.label}" {h.max_chars or 0}')
        elif h.style == "manual":
            lines.append(f'    Set-ManualRules "{h.label}" '
                         f'"$OMS_DIR/{h.key}-user-rules.md"')
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


def _ps_mcp_helpers(mcp_url: str, bundle_token: str | None,
                    credential_setup: str | None = None, notice: str = "") -> str:
    """The global-MCP writers and the per-harness calls.

    Merged rather than written, because these files hold servers the user
    configured and replacing one silently disconnects every other tool they
    have. PowerShell parses JSON itself, so unlike the shell installer there
    is no staged Python program and no machine on which this step degrades.

    Assembled by concatenation rather than as one f-string: the block below is
    made of PowerShell, and an f-string would mean doubling every brace in it
    for the sake of two substitutions.
    """
    credential = (_LOCAL_PS_CREDENTIAL if credential_setup is None
                  else credential_setup)
    lines = ['''
# 3. The log_correction / log_signal tools, user-scoped: present in every
#    project's tool list, which is what makes spontaneous contribution work.
#''' + credential + '''function Merge-McpJson($File, $UrlKey, $Label) {
    $raw = Read-TextFile $File
    try {
        $data = if ($raw.Trim()) { $raw | ConvertFrom-Json } else { [pscustomobject]@{} }
    } catch {
        Write-Host "  ${Label}: $File is not JSON this can merge into, so it was left"
        Write-Host "    alone. Add the 'oms' server to it by hand."
        return
    }
    if ($data -isnot [pscustomobject]) {
        Write-Host "  ${Label}: $File is not a JSON object, so it was left alone."
        return
    }
    if (-not $data.PSObject.Properties['mcpServers']) {
        $data | Add-Member -MemberType NoteProperty -Name 'mcpServers' -Value ([pscustomobject]@{})
    }
    $entry = [ordered]@{ $UrlKey = "@@MCP_URL@@" }
    if ($OMS_MCP_TOKEN) {
        $entry["headers"] = [ordered]@{ Authorization = "Bearer $OMS_MCP_TOKEN" }
    }
    $servers = $data.mcpServers
    if ($servers.PSObject.Properties['oms']) {
        $servers.oms = [pscustomobject]$entry
    } else {
        $servers | Add-Member -MemberType NoteProperty -Name 'oms' -Value ([pscustomobject]$entry)
    }
    Write-TextFile $File (($data | ConvertTo-Json -Depth 20) + "`n")
    Write-Host "  ${Label}: MCP server 'oms' registered in $File"
}

function Write-CodexMcp($File, $Label) {
    $kept = Remove-OmsBlock (Read-TextFile $File)
    # Ours is already stripped, so anything left is the user's own, and both
    # ways of handling it are wrong: appending makes the duplicate table that
    # breaks Codex outright, overwriting takes a decision that is theirs.
    if ($kept -match '(?m)^\\[mcp_servers\\.oms\\]') {
        Write-Host "  ${Label}: $File already has its own [mcp_servers.oms]; left alone."
        if ($OmsContributionMode -ceq 'confirm') {
            Write-Host "    Confirm mode set no per-tool approval there, so Codex may not ask before a contribution unless you configure it."
        }
        return
    }
    # Confirm mode also sets Codex's own approval, "prompt", for the two tools
    # that send a contribution; install.sh gives the reasons. A table the
    # user wrote for one of them stays theirs. Matched case-sensitively, as
    # TOML reads keys and as the sh grep does: a table differing only in case
    # is another key, and taking it for the user's would leave approval unset.
    $ask = ''
    if ($OmsContributionMode -ceq 'confirm') {
        foreach ($tool in 'log_correction', 'log_signal') {
            if ($kept -cmatch "(?m)^\\[mcp_servers\\.oms\\.tools\\.$tool\\]") {
                Write-Host "  ${Label}: $File already has its own [mcp_servers.oms.tools.$tool];"
                Write-Host "    that table, not confirm mode, decides whether Codex asks before $tool."
            } else {
                $ask += "[mcp_servers.oms.tools.$tool]`napproval_mode = `"prompt`"`n"
            }
        }
    }
    $body = $kept.TrimEnd()
    if ($body) { $body += "`n" }
    Write-TextFile $File ($body + $BEGIN_MARK + "`n" +
        "[mcp_servers.oms]`nurl = `"@@MCP_URL@@`"`n" + $(if ($OMS_MCP_TOKEN) {
            "http_headers = { Authorization = `"Bearer $OMS_MCP_TOKEN`" }`n" }) + $ask + $END_MARK + "`n")
    Write-Host "  ${Label}: MCP server 'oms' registered in $File"
}
''']
    for h in HARNESSES:
        if not h.mcp_config:
            continue
        if h.mcp_format == "toml":
            call = f'    Write-CodexMcp "{h.mcp_config}" "{h.label}"'
        else:
            call = (f'    Merge-McpJson "{h.mcp_config}" "{h.mcp_url_key}" '
                    f'"{h.label}"')
        lines.append(f'if (${_have(h)}) {{\n{call}\n}}')

    # Claude Code has a CLI for this. Run through `if`, never bare: a claude
    # whose flags have drifted since this bundle was published exits non-zero,
    # and aborting here would leave the machine with no refresh scheduled and
    # no "Done." line, after every other piece of work had succeeded.
    #
    # The `Stop` preference is lifted for the duration, and restored, for the
    # same reason: on Windows `claude` is an npm-installed .ps1 shim, so it
    # runs in a child scope and inherits this script's preferences. Under
    # `Stop`, one line on standard error from claude.exe is promoted to a
    # terminating NativeCommandError - and `mcp remove` writes one on every
    # first install, there being nothing yet to remove. try/catch as well as
    # the preference, because the two guard different things: the preference
    # keeps chatter on stderr from terminating anything, the catch keeps a
    # shim that genuinely throws from taking the installer with it.
    # See the shell installer for why the header is passed to the CLI and
    # withheld from the printed advice. Substituted as @@ tokens rather than
    # interpolated, for the reason given above _PS1_TEMPLATE: an f-string here
    # would mean doubling every brace in the block below.
    lines.append('''
if (Get-Command claude -ErrorAction SilentlyContinue) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Seeded so the read below is defined under Set-StrictMode even if neither
    # call reaches the engine. Global, because that is the scope the engine
    # writes an exit code into; a bare assignment here would shadow it and
    # report every registration as a failure.
    $global:LASTEXITCODE = 1
    try {
        & claude mcp remove --scope user oms 2>&1 | Out-Null
        & claude mcp add --scope user --transport http oms "@@MCP_URL@@"@@CLAUDE_HEADER@@ 2>&1 | Out-Null
    } catch {
        $global:LASTEXITCODE = 1
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($global:LASTEXITCODE -eq 0) {
        Write-Host "  registered MCP server 'oms' -> @@MCP_URL@@"
    } else {
        Write-Host "  could not register the MCP server 'oms' with this claude CLI."
        Write-Host "  Everything else is installed. Register it by hand with:"
        Write-Host "@@BY_HAND@@"@@TOKEN_HINT@@
    }
} else {
    Write-Host "  claude CLI not found; register the tool later with:"
    Write-Host "@@BY_HAND@@"@@TOKEN_HINT@@
}
''')

    # Names the credential in use without echoing it: this reaches a terminal,
    # and terminals get screenshotted into chat threads.
    if notice:
        lines.append(notice)
    body = "\n".join(lines)
    # Doubled quotes are PowerShell's escape inside a double-quoted string, and
    # the backtick keeps $TOKEN literal - unescaped it would be read as a
    # variable, which under Set-StrictMode is an error rather than an empty
    # string, killing the very branch that exists to report a failure.
    by_hand = ("    claude mcp add --scope user --transport http oms @@MCP_URL@@"
               + (' --header ""Authorization: Bearer `$TOKEN""' if bundle_token else ""))
    token_hint = ('\n        Write-Host "  (the token is the Authorization value in '
                  '$SRC\\.mcp.json)"' if bundle_token else "")
    return (body
            .replace("@@BUNDLE_TOKEN@@", bundle_token or "")
            # The resolved value, like every other harness: an enrolled
            # machine's Claude Code registration must carry its own
            # credential or its contributions alone stay unbound.
            .replace("@@CLAUDE_HEADER@@",
                     ' --header "Authorization: Bearer $OMS_MCP_TOKEN"')
            .replace("@@BY_HAND@@", by_hand)
            .replace("@@TOKEN_HINT@@", token_hint))


def render_install_ps1(mcp_url: str | None,
                       root_files: Sequence[str] | None = None,
                       bundle_token: str | None = None,
                       *, fragments: PowerShellInstallFragments | None = None) -> str:
    """Render the shared Windows installer with explicit trusted extension slots."""
    validate_script_literals(mcp_url=mcp_url, bundle_token=bundle_token)
    fragments = fragments or PowerShellInstallFragments()
    import_file = root_import_file(root_files)
    looked_for = " ".join(h.detect for h in HARNESSES)
    mcp = (_ps_mcp_helpers(mcp_url, bundle_token,
                           fragments.credential_setup, fragments.credential_notice)
           if mcp_url else "")
    body = (_PS1_TEMPLATE
            .replace("@@DETECTION@@", _ps_detection_blocks())
            .replace("@@INSTALL@@", _ps_install_blocks())
            .replace("@@MCP@@", mcp)
            .replace("@@BEFORE_LINKS@@", fragments.before_links)
            .replace("@@LINK_INIT@@", fragments.link_initialiser)
            .replace("@@SKIP_SKILL@@", fragments.skip_skill)
            .replace("@@LINK_REPORT@@", fragments.link_report)
            .replace("@@REFRESH_OK@@", fragments.refresh_success)
            # The root file of the mode section 0 settled, as in install.sh.
            .replace("@@ROOT_MD@@", fragments.root_source or "$SRC/$OmsRootFile")
            .replace("@@ROOT_IMPORT@@", fragments.root_import or "@$SRC/$OmsRootFile")
            .replace("@@LOOKED_FOR@@", looked_for)
            .replace("@@BUNDLE_TOKEN@@", bundle_token or "")
            # Last, and that ordering is load-bearing: several of the
            # replacements above expand to text that names the root
            # instruction file, and resolving this one first would leave the
            # token sitting in the shipped script for PowerShell to read as a
            # command. `test_no_placeholder_survives_rendering` is the guard.
            .replace("@@CLAUDE_NOTE@@", CLAUDE_APPROVAL_NOTE)
            .replace("@@CONFIRM_FILE@@", confirm_variant_name(import_file))
            .replace("@@IMPORT_FILE@@", import_file))
    if mcp_url:
        body = body.replace("@@MCP_URL@@", mcp_url)
    return body


#: Where the README tells a new starter to put the clone.
#:
#: The location is not cosmetic and choosing it for them is the point. Every
#: skill is symlinked into this tree and the refresh script bakes its path, so
#: the clone is permanent: cloned into Downloads and tidied away a fortnight
#: later, every link dangles and the daily pull dies, with no error anywhere -
#: the agent simply stops seeing any skills. `~/.oms` is already OMS's own
#: directory, created unconditionally by the installer for the credentials and
#: the staleness file, so the bundle sits beside them rather than wherever the
#: person happened to be standing when they ran git.
BUNDLE_DIR = "~/.oms/bundle"


def _getting_started(repo: str | None) -> list[str]:
    """The half of the install the README used to assume had already happened.

    `sh install.sh` presupposes the tree is on disk and somewhere sensible.
    Neither holds for a new starter, and nothing told them either.

    Rendered only when the deployment configured a distribution repository.
    Inventing a clone line for a URL nobody chose would be worse than the
    omission it replaces: it would look authoritative and fail.
    """
    if not repo:
        return []
    # Stripped again here, though WebSettings already stripped it on the way
    # in. Belt and braces is proportionate for exactly one class of mistake:
    # the value that would leak is a write token for the organisation's skills
    # repository, and the place it would leak to is a file published into that
    # same repository and installed on every machine. A future caller that
    # passes its own URL cannot reintroduce that.
    repo = public_repo_url(repo)
    return [
        "## Before you start", "",
        "You need three things on the machine, and one of them is not "
        "automatic:", "",
        "- **Read access to this repository.** If `git clone` below fails, "
        "that is what is missing - ask whoever administers OMS.",
        "- **git**, and **python3** for the enrolment step further down.",
        "- **At least one agent tool.** The installer configures "
        + ", ".join(h.label for h in HARNESSES[:-1])
        + f" and {HARNESSES[-1].label}, and refuses to run when it finds none "
        "of them.", "",
        "Then, on macOS or Linux, the whole install is one line:", "",
        "```sh",
        f"git clone {repo} {BUNDLE_DIR} && sh {BUNDLE_DIR}/install.sh",
        "```", "",
        "On Windows:", "",
        "```powershell",
        f"git clone {repo} $HOME\\.oms\\bundle; "
        "powershell -ExecutionPolicy Bypass -File $HOME\\.oms\\bundle\\install.ps1",
        "```", "",
        f"**Clone it somewhere permanent, and {BUNDLE_DIR} is the suggestion.** "
        "Every skill is linked into this tree rather than copied out of it, and "
        "the daily refresh pulls into the same path, so moving or deleting the "
        "clone later breaks both - and breaks them silently, leaving an agent "
        "that simply sees no skills.", "",
    ]


def render_readme(mcp_url: str | None, distribution_repo: str | None = None, *,
                  instructions: Sequence[str] = (),
                  troubleshooting: Sequence[str] = ()) -> str:
    """Human-facing README at the published root: what the tree is and the
    one-step install. Agents read AGENTS.md/CLAUDE.md; this is for the person
    cloning the repo.

    `distribution_repo` is the remote the organisation clones, already stripped
    of any credential by `public_repo_url` at the point the variable enters the
    process. None renders the README as it was before this existed."""
    lines = [
        "# Organisational skills (published by OMS)", "",
        "This repository is generated and overwritten by OMS on every publish - "
        "do not edit it by hand. Corrections flow back through the contribution "
        "pipeline described in `AGENTS.md`.", "",
        *_getting_started(distribution_repo),
        "## Install (once per machine)", "",
        "macOS and Linux:", "",
        "```sh",
        "sh install.sh",
        "```", "",
        "Windows:", "",
        "```powershell",
        "powershell -ExecutionPolicy Bypass -File .\\install.ps1",
        "```", "",
        "`-ExecutionPolicy Bypass` applies to that one invocation and changes "
        "no machine-wide setting. It is needed because a downloaded script is "
        "unsigned, and the default policy refuses those.", "",
        "This finds the agent tools on your machine and links the skills and "
        "instructions into each one's user scope, registers the OMS "
        "contribution tool for every project, and schedules a daily pull. Safe "
        "to re-run; it never touches content you own, and it creates nothing "
        "for a tool you do not have.", "",
        # Only where the bundle takes contributions: a read-only one has no
        # confirm-mode instructions, and asking for them fails the install.
        *([
            "By default, an agent sends a correction you give it to OMS "
            "without asking. To have it ask first and send only what you "
            "approve, run the installer as `OMS_CONTRIBUTION_MODE=confirm sh "
            "install.sh` (on Windows, run `$env:OMS_CONTRIBUTION_MODE = "
            "'confirm'` first). The machine keeps that choice until you run "
            "the installer again with `OMS_CONTRIBUTION_MODE=automatic` (on "
            "Windows, `$env:OMS_CONTRIBUTION_MODE = 'automatic'`). In confirm "
            "mode the installer also adds a permission rule to your Claude Code "
            "settings, so Claude Code asks before each contribution and your "
            "answer there is the choice. Choosing automatic again takes it out.", "",
        ] if mcp_url else []),
        "For a local folder instead of a Git clone, `install.sh` checks for "
        "completed publications every minute using a macOS launch agent or "
        "cron on Linux. Added and removed skills and copied instructions are "
        "reconciled automatically. Keep the folder in the same location. "
        "Read the final installer message: if scheduling is unavailable, "
        "run `sh ~/.oms/oms-refresh.sh` after publishing. An agent may need "
        "a new session to reload its guidance.", "",
        "It configures " + ", ".join(h.label for h in HARNESSES[:-1])
        + f" and {HARNESSES[-1].label}. Anything it could not do it says so on "
        "the way past, naming the file and the reason.", "",
        "Working inside a single repo instead? Copy this tree to that repo's "
        "root and the bundled `.mcp.json`, `CLAUDE.md` and `.cursor/` config "
        "activate there without the machine-level install.",
        *instructions,
    ]
    if mcp_url:
        lines += [
            "", "## Where things land", "",
            f"The contribution tool is MCP over HTTP at `{mcp_url}`, and "
            "`install.sh` registers it globally for each tool it finds. The "
            "three shapes differ, which is why the installer writes them rather "
            "than asking you to:", "",
            "| Tool | Skills | Instructions | MCP |",
            "| --- | --- | --- | --- |",
        ]
        for h in HARNESSES:
            mcp = (f"`{h.mcp_config}`".replace("$HOME", "~").replace("$CLAUDE_DIR", "~/.claude")
                   if h.mcp_config else "`claude mcp add --scope user`")
            instr = (f"`{h.instructions}`".replace("$HOME", "~").replace("$CLAUDE_DIR", "~/.claude")
                     if h.instructions else "Settings > Rules (paste, once)")
            skills = f"`{h.skills_dir}`".replace("$HOME", "~").replace("$CLAUDE_DIR", "~/.claude")
            lines.append(f"| {h.label} | {skills} | {instr} | {mcp} |")
        lines += [
            "",
            "Cursor is the one exception. Its User Rules are settings text "
            "rather than a file, so the installer stages the exact content at "
            "`~/.oms/cursor-user-rules.md` and you paste it into Cursor "
            "Settings > Rules once. Its skills are linked for you like every "
            "other tool's.",
            "",
            "### No hooks",
            "",
            "OMS installs nothing that runs while you work. It links skill "
            "files and registers an MCP server; nothing of ours executes on "
            "your keystrokes.",
            "",
            "**Corrections reach OMS one way only** - you state one and the "
            "agent files it with `log_correction`, asking you first where the "
            "installer ran in confirm mode. Two earlier hooks tried to "
            "be cleverer than that and both were removed: one scanned your "
            "prompts for rule-shaped sentences, and almost everything it "
            "caught was somebody testing the pipeline rather than a rule they "
            "meant; the other recorded a line per prompt so reflection could "
            "tell a finished session from a live one, which the transcript "
            "file's own modification time answers without running anything.",
            "",
            "If you installed an older bundle you may still have an "
            "`oms_session_hook` entry under `hooks.UserPromptSubmit` in "
            "`~/.claude/settings.json`. The installer removes it for you on "
            "the next daily refresh; you can also delete it by hand.",
        ]
        lines += troubleshooting
    return "\n".join(lines) + "\n"


def _inlined_resolver() -> list[str]:
    """Inline the shared resolver into the shim. The published bundle cannot
    import from OMS, so the code is copied at generation time rather than
    imported, and copied from the one source of truth (`src/oms/client/
    credentials.py`) rather than retyped, so the two cannot drift apart."""
    import inspect
    src = inspect.getsource(_credentials_module.resolve_credential)
    return src.rstrip("\n").split("\n")


def _inlined_repo_key() -> list[str]:
    """Inline `repo_key` into the shim, the same way `_inlined_resolver`
    above inlines `resolve_credential`: by `inspect.getsource` of the real
    functions in `oms.domain.repo`, so the bundled copy cannot drift from the
    one the server anchors a bundle by.

    Unlike `resolve_credential`, `repo_key` is not one self-contained
    function - it calls `_split`, which calls `_host` and `_path` against the
    module's compiled patterns - so every private helper it touches is inlined
    too, not just the public name. The patterns are read off the live compiled
    objects rather than typed out a second time: `_SCP.pattern` is the exact
    text `oms.domain.repo` compiled, so a changed pattern there changes what is
    written here without anybody having to remember to edit both places.

    Every compiled pattern in the module is emitted, rather than a named list
    of the ones `_split` happens to use today. A named list was there first and
    it is a trap: adding a pattern to `oms.domain.repo` leaves the shim
    referring to a name it never defines, and the failure is a NameError on the
    contributor's laptop, on only the remote spellings that reach the new
    branch. `test_the_shim_defines_every_pattern_its_key_function_uses` is the
    guard, and this is what makes it pass by construction."""
    import inspect

    lines = [f"{name} = re.compile({pattern.pattern!r})"
             for name, pattern in sorted(vars(_repo_module).items())
             if isinstance(pattern, re.Pattern)]
    lines.append("")
    for fn in (_repo_module._host, _repo_module._path, _repo_module._split,
               _repo_module._fold, _repo_module.repo_key):
        lines += inspect.getsource(fn).rstrip("\n").split("\n")
        lines.append("")
    return lines[:-1]


def _arguments_as_on_the_envelope(*names: str) -> list[str]:
    """The helper's docstring entries for its session-context keywords.

    Wrapped from `ExecutionContext`'s own field descriptions rather than
    retyped. The MCP input schema and the thin door already read them there,
    so every door tells an agent the same thing, limits included, and a
    changed limit reaches the helper at the next publish. Hyphens are never
    broken, so a reader of the docstring sees "Human-readable" whole.

    Copied unescaped into a plain triple-quoted docstring of generated
    source, so each description is checked first. A backslash there would be
    read as an escape, and a triple quote would end the docstring and leave
    the rest of the text to run as code on the contributor's machine. A
    missing one would print "None". Raised, not asserted: `python -O` strips
    an assert, and this check guards what the helper ships."""
    lines: list[str] = []
    for name in names:
        description = ExecutionContext.model_fields[name].description
        if (not isinstance(description, str) or not description.strip()
                or "\\" in description or '"""' in description):
            raise ValueError(
                f"ExecutionContext.{name} needs a description that is non-empty text "
                f"with no backslash and no triple quote, because the helper's "
                f"docstring carries it as written")
        lines += textwrap.wrap(f"{name}: {description}",
                               width=76, initial_indent=" " * 8, subsequent_indent=" " * 12,
                               break_on_hyphens=False, break_long_words=False)
    return lines


def render_contribute_tool(endpoint: str, bundle_token: str | None = None, *,
                           fragments: ContributionFragments | None = None) -> str:
    """Generate a standalone contribution client with optional trusted helpers."""
    validate_script_literals(endpoint=endpoint, bundle_token=bundle_token)
    fragments = fragments or ContributionFragments()
    lines = [
        '"""Contribute a learning to OMS (generated by OMS at publish time).',
        "",
        # Which contributions to send, and whether to ask the person first,
        # is for the instructions an installation loads. This file is the
        # same in every mode, so it only says what it sends.
        "Send a correction or learning that your OMS instructions say to share.",
        "OMS fills in the timestamp, tenant and routing, and a transaction id",
        "when you do not pass one. Standard library only, so it runs in any",
        "Python environment without installation.",
        *fragments.description,
        '"""',
        "from __future__ import annotations",
        "",
        "import io",
        "import json",
        "import os",
        "import re",
        "import subprocess",
        "import sys",
        "import urllib.error",
        "import urllib.request",
        "from pathlib import Path",
        "",
        f'OMS_INGEST_ENDPOINT = "{endpoint}"',
        f'CREDENTIALS_PATH = "{_credentials_module.CREDENTIALS_PATH}"',
        *([f'BUNDLE_TOKEN = "{bundle_token}"'] if bundle_token else []),
        "",
        "",
        *_inlined_resolver(),
        "",
        "",
        *_inlined_repo_key(),
        "",
        "",
        "def _repo() -> str | None:",
        '    """The normalised remote of the repository the caller is standing in.',
        "",
        "    Read here rather than asked of the agent: this helper runs on the",
        "    contributor's machine, so the fact is free and the published",
        "    instruction does not have to grow a line about it.",
        "",
        "    Every failure is None. No git, no remote, a timeout, a directory",
        "    that is not a checkout: this is context, and a contribution must",
        '    never fail over context it could not gather."""',
        "    try:",
        "        out = subprocess.run(",
        '            ["git", "remote", "get-url", "origin"],',
        "            capture_output=True, text=True, timeout=5, check=False)",
        "    except (OSError, subprocess.SubprocessError):",
        "        return None",
        "    return repo_key(out.stdout) if out.returncode == 0 else None",
        "",
        "",
        f"def _post(url: str, body: dict{fragments.request_parameter}) -> dict:",
        *fragments.request_documentation,
        '    data = json.dumps(body).encode("utf-8")',
        '    request = urllib.request.Request(url, data=data, method="POST")',
        '    request.add_header("Content-Type", "application/json")',
        fragments.credential_expression or ("    token = resolve_credential() or BUNDLE_TOKEN" if bundle_token else "    token = resolve_credential()"),
        "    if token:",
        '        request.add_header("Authorization", "Bearer " + token)',
        "    with urllib.request.urlopen(request, timeout=10) as response:",
        '        return json.loads(response.read().decode("utf-8"))',
        "",
        "",
        # Kept out of `_post`. Helpers injected through the fragments send
        # requests of their own through it and read the error bodies
        # themselves, so it keeps raising every error with its body unread.
        "def _refusal_or_raise(error: urllib.error.HTTPError) -> dict:",
        '    """Answer with the refusal an error response carries; raise anything else.',
        "",
        "    A refusal is a policy's final no: the server read the request and will",
        "    not admit it, so sending it again changes nothing. Its JSON body names",
        '    a `reason` and says `"retryable": false`, either in an error envelope,',
        '    {"error": {"message": ..., "reason": ..., "retryable": false}}, or at',
        '    the top level, {"detail": ..., "reason": ..., "retryable": false}.',
        "    Anything else is a fault and stays one, including a body that asks to",
        "    be retried later.",
        "",
        "    The check has to read the body, which can be read only once, so an",
        "    error that is not a refusal is raised again around the same bytes and",
        "    from the same place: a caller still reads the status, headers and body",
        '    the server sent, such as which field was over its limit."""',
        "    try:",
        "        raw = error.read()",
        "    except Exception:",
        "        raise error from None",
        "    try:",
        '        body = json.loads(raw.decode("utf-8"))',
        "    except ValueError:",
        "        body = None",
        '    envelope = body.get("error") if isinstance(body, dict) else None',
        "    if isinstance(envelope, dict):",
        '        refusal, text = envelope, "message"',
        "    else:",
        '        refusal, text = body, "detail"',
        '    reason = refusal.get("reason") if isinstance(refusal, dict) else None',
        '    if isinstance(reason, str) and reason and refusal.get("retryable") is False:',
        "        message = refusal.get(text)",
        '        return {"status": "refused", "reason": reason,',
        '                "message": message if isinstance(message, str) else reason,',
        '                "retryable": False}',
        "    again = urllib.error.HTTPError(error.geturl(), error.code, error.msg,",
        "                                   error.hdrs, io.BytesIO(raw))",
        "    raise again.with_traceback(error.__traceback__) from None",
        "",
        "",
        "def contribute_learning(correction: str, skill_hint: str | None = None,",
        '                        signal_type: str | None = None,',
        '                        source_ref: str | None = None,',
        '                        repo: str | None = None, *,',
        '                        session_summary: str | None = None,',
        '                        project_name: str | None = None,',
        '                        reuse_case: str | None = None,',
        '                        learning_evidence: str | None = None,',
        '                        transaction_id: str | None = None) -> dict:',
        '    """Send a correction or learning that your OMS instructions say to share.',
        "",
        "    Args:",
        "        correction: the correction or rule, in one or two sentences.",
        '            With signal_type "self_reflection" it is sent as the learning.',
        "        skill_hint: the skill you were working in, if known.",
        '        signal_type: "self_reflection" for a learning you worked out',
        "            yourself, rather than one a person told you. That is what",
        "            keeps it out of the auto-write path and in front of a",
        "            reviewer. Leave it unset for a correction a person made.",
        '        source_ref: "session:<your session id>", so every learning',
        "            from one session reaches a reviewer as a single card.",
        "        repo: the repository this correction is about, as its git",
        "            remote. Almost never worth passing by hand: left unset,",
        "            it defaults to `_repo()`, which reads the remote of the",
        "            checkout this call is made from, so the repo travels on",
        "            every contribution from this door whether or not anybody",
        "            thought about it.",
        *_arguments_as_on_the_envelope("session_summary", "project_name", "reuse_case",
                                       "learning_evidence"),
        "        transaction_id: an id of your choosing for this contribution:",
        '            letters, digits, ".", "_", ":" or "-", starting with a letter',
        "            or digit, up to 256 characters. Use a new UUID for each",
        "            contribution. Send the same id again only when you retry",
        "            after an answer that never arrived, so OMS returns the first",
        "            receipt instead of recording it twice. OMS returns that",
        "            receipt for a repeated id without comparing what was sent,",
        "            so any change to what is sent (the wording, skill_hint, the",
        "            context or the signal) needs a new id. An id that another",
        "            person used, or that was used with another signal_type, is",
        '            refused with the reason "transaction_id_reused"; send the',
        "            contribution again with a new id. Left unset, OMS assigns",
        "            one.",
        "",
        "    Apart from repo, a keyword left as None is not sent at all.",
        "",
        "    Returns the OMS acknowledgement, for example",
        '    {"transaction_id": "...", "status": "accepted"}. A policy refusal is',
        '    returned too, not raised: {"status": "refused", "reason": ...,',
        '    "message": ..., "retryable": False}. It is final, so do not send it',
        "    again or as a different signal; only a transaction_id_reused",
        "    refusal asks for the same contribution under a new id. Any other",
        "    failure raises.",
        '    """',
        '    body = {"learning" if signal_type == "self_reflection" else "correction": correction}',
        "    if skill_hint is not None:",
        '        body["skill_hint"] = skill_hint',
        "    if signal_type is not None:",
        '        body["signal_type"] = signal_type',
        "    if source_ref is not None:",
        '        body["source_ref"] = source_ref',
        "    resolved_repo = repo if repo is not None else _repo()",
        "    if resolved_repo is not None:",
        '        body["repo"] = resolved_repo',
        '    for name, value in (("session_summary", session_summary),',
        '                        ("project_name", project_name),',
        '                        ("reuse_case", reuse_case),',
        '                        ("learning_evidence", learning_evidence),',
        '                        ("transaction_id", transaction_id)):',
        "        if value is not None:",
        "            body[name] = value",
        "    # A refusal is an answer, and returning it is what lets a caller",
        "    # tell it from a fault: both used to arrive as the same exception.",
        "    try:",
        "        return _post(OMS_INGEST_ENDPOINT, body)",
        "    except urllib.error.HTTPError as error:",
        "        return _refusal_or_raise(error)",
        "",
        "",
        *fragments.helpers,
        'if __name__ == "__main__":',
        *fragments.cli_documentation,
        "    import sys",
        "",
        *fragments.cli_dispatch,
        "    def _usage() -> None:",
        "        sys.stderr.write(",
        "            \"usage: oms_contribute.py '<correction>' ['<skill_hint>']\"",
        '            " [--signal-type <type>] [--source-ref <ref>]"',
        '            " [--session-summary <text>] [--project-name <name>]"',
        '            " [--reuse-case <text>] [--evidence <text>]"',
        '            " [--transaction-id <id>]\\n"',
        *fragments.usage_lines,
        "        )",
        "        raise SystemExit(2)",
        "",
        "    if len(sys.argv) < 2:",
        "        _usage()",
        "    # sys.argv[1] is the correction, always, and is never parsed as a",
        "    # flag: a correction is arbitrary user text, so a dash at its start",
        "    # is part of that text and not an option. Only sys.argv[2:] is",
        "    # parsed, and a leading bare token there is still the skill hint, so",
        "    # every caller written against the old two-argument form keeps its",
        "    # exact meaning.",
        "    correction_arg = sys.argv[1]",
        "    skill_hint_arg = None",
        "    rest = sys.argv[2:]",
        '    if rest and not rest[0].startswith("--"):',
        "        skill_hint_arg = rest.pop(0)",
        "    # Each flag sets the contribute_learning keyword of the same name,",
        "    # except --evidence, which is short for learning_evidence.",
        '    keywords = {"--signal-type": "signal_type", "--source-ref": "source_ref",',
        '                "--session-summary": "session_summary",',
        '                "--project-name": "project_name", "--reuse-case": "reuse_case",',
        '                "--evidence": "learning_evidence",',
        '                "--transaction-id": "transaction_id"}',
        "    options = {}",
        "    while rest:",
        "        flag = rest.pop(0)",
        "        if flag not in keywords:",
        "            _usage()",
        "        # A flag with no value is a usage error, not a silent None. A",
        "        # dropped signal_type is exactly what turns a self-reflection",
        "        # into the default EXPLICIT_CORRECTION, which can be auto-written",
        "        # into a published skill with nobody reviewing it, and a dropped",
        "        # transaction id turns a retry into new work, so a half-written",
        "        # command line has to fail here rather than post something it",
        "        # was never asked to post.",
        "        if not rest:",
        "            _usage()",
        "        options[keywords[flag]] = rest.pop(0)",
        "    try:",
        "        ack = contribute_learning(correction_arg, skill_hint_arg, **options)",
        "    except Exception as exc:  # network, HTTP, or decode failure",
        '        sys.stderr.write(f"OMS contribution FAILED: {exc}\\n")',
        "        raise SystemExit(1)",
        "    print(json.dumps(ack))",
        '    if ack.get("status") != "accepted":',
        "        # A refusal says why in the server's own words, including",
        "        # whether to send it again. Any other answer has only this line.",
        '        sys.stderr.write("OMS did not accept the contribution"',
        '                         + (": " + str(ack["message"]) if ack.get("message") else "")',
        '                         + "\\n")',
        "        raise SystemExit(1)",
        "",
    ]
    return "\n".join(lines)


def _constraint_block(constraints: list[Constraint]) -> list[str]:
    """The always-in-context compliance block. Shared by the Tier 1 root file
    and the Tier 2 manifest, which inline it for the same reason: a missed
    trigger must never be able to drop a guardrail."""
    lines = [ROOT_INSTRUCTION_MARKER, "",
             "These are always in effect and must never be violated.", ""]
    for c in constraints:
        body = c.body.strip()
        if c.polarity is Polarity.PROSCRIBE and not _SELF_PROSCRIBE.match(body):
            lines.append(f"* Avoid: {body}")
        else:
            lines.append(f"* {body}")
    return lines


def render_claude_md(constraints: list[Constraint], skills: list[Skill],
                     contribution_endpoint: str | None = None, *,
                     session_learnings: bool = True,
                     contribution_mode: ContributionMode = "automatic") -> str:
    """One root instruction file: constraints, the skill index and, with an
    endpoint, the contribution block in the variant asked for (see
    `_contribution_block`). The mode is checked with or without an endpoint,
    so a misspelt one fails wherever it is passed, not only where it would
    show."""
    _check_contribution_mode(contribution_mode)
    lines = _constraint_block(constraints)
    lines += ["", "# Available Skills", ""]
    for s in sorted(skills, key=lambda x: x.name):
        lines.append(f"* **{s.name}**: {s.description}")
    if contribution_endpoint:
        lines += _contribution_block(contribution_endpoint,
                                     session_learnings=session_learnings,
                                     mode=contribution_mode)
    return "\n".join(lines) + "\n"


def version_of(body: str) -> str:
    """A skill's version: a short digest of the exact bytes served.

    Content-derived rather than a counter, so it needs no extra state and moves
    precisely when the rendered skill moves. A Tier 2 cache keys on
    (skill_name, version) and refetches only on a bump (spec §9.2)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ManifestEntry:
    """One skill's row in the Tier 2 manifest: what to match on, what to call,
    and which version the caller would be fetching."""
    id: str
    name: str
    description: str
    version: str


def _cell(text: str) -> str:
    """Flatten a value into one markdown table cell: a stray newline or pipe in
    a description would otherwise break the table the agent has to read."""
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat.replace("|", r"\|")


def render_tier2_manifest(constraints: list[Constraint], entries: list[ManifestEntry],
                          mcp_endpoint: str | None = None) -> str:
    """The Tier 2 manifest (spec §9.2): constraints inlined, a dispatch table,
    and a version map. Hosts without native skill selection read this instead
    of a bundled tree and fetch bodies over MCP on demand."""
    lines = _constraint_block(constraints)
    lines += [
        "", "# Fetching Skills", "",
        "Skill bodies are NOT bundled with this file. Before doing work that "
        "matches a trigger below, fetch that skill over MCP and follow it.",
        "",
        "* `list_skills(domain_hint?)` - the catalogue: names, descriptions, versions.",
        "* `query_skill(name)` - the full body of one skill.",
        "* `query_skill_resource(skill, resource)` - a Level 3 reference the body points at.",
        "",
    ]
    if mcp_endpoint:
        lines += [f"MCP endpoint: `{mcp_endpoint}`", ""]
    lines += [
        "## When to fetch which skill", "",
        "| When the task matches | Fetch |",
        "| --- | --- |",
    ]
    for e in sorted(entries, key=lambda x: x.name):
        lines.append(f'| {_cell(e.description)} | `query_skill("{e.id}")` |')
    lines += [
        "", "## Versions", "",
        "Cache each fetched skill under (name, version). Refetch a skill only "
        "when its version here differs from the copy you hold; serve from cache "
        "otherwise, including when OMS is unreachable.",
        "",
        "| Skill | Version |",
        "| --- | --- |",
    ]
    for e in sorted(entries, key=lambda x: x.name):
        lines.append(f"| {_cell(e.id)} | `{e.version}` |")
    return "\n".join(lines) + "\n"


_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\n]+)\)")
_TICKED = re.compile(r"`([^`\n]+)`")


def _fetch_call(skill_id: str, resource: str) -> str:
    return f'`query_skill_resource("{skill_id}", "{resource}")`'


def rewrite_resource_links(body: str, skill_id: str, resources: list[str]) -> str:
    """Point a fetching host at the read tool instead of at a file path.

    A published body links its Level 3 references as relative paths, which is
    correct for Tier 1: the file sits next to SKILL.md and the agent opens it
    with its file tools. A Tier 2 host has no such directory, so every pointer
    to one of this skill's own resources is rewritten to name the
    `query_skill_resource` call that fetches it (spec §9.2).

    Only paths OMS itself published are touched: a workspace path, an external
    URL, or a reference to another skill is left exactly as the author wrote
    it. Fenced code is skipped, so examples and templates survive intact."""
    if not resources:
        return body
    known = set(resources)
    lines: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        lines.append(line if in_fence else _rewrite_pointers(line, skill_id, known))
    return "\n".join(lines)


def _rewrite_pointers(line: str, skill_id: str, known: set[str]) -> str:
    def link(match: re.Match) -> str:
        label, target = match.group(1), _strip_relative(match.group(2))
        if target not in known:
            return match.group(0)
        call = _fetch_call(skill_id, target)
        # A link whose label is just the path reads as a bare pointer, so the
        # call replaces it outright; a real label is worth keeping.
        if label.strip().strip("`") == target:
            return call
        return f"{label} ({call})"

    def ticked(match: re.Match) -> str:
        target = _strip_relative(match.group(1))
        return _fetch_call(skill_id, target) if target in known else match.group(0)

    return _TICKED.sub(ticked, _MD_LINK.sub(link, line))


def _strip_relative(target: str) -> str:
    stripped = target.strip()
    return stripped[2:] if stripped.startswith("./") else stripped




def _local_credential_block(bundle_token: str | None) -> str:
    """Read an explicitly configured local HTTP credential without user workflow."""
    return f'''
OMS_MCP_TOKEN='{bundle_token or ""}'
if [ -r "$OMS_DIR/credentials" ]; then
  OMS_FROM_FILE="$(cat "$OMS_DIR/credentials" 2>/dev/null || true)"
  if [ -n "$OMS_FROM_FILE" ]; then
    OMS_MCP_TOKEN="$OMS_FROM_FILE"
  fi
fi
'''


_LOCAL_PS_CREDENTIAL = '''
$OMS_MCP_TOKEN = "@@BUNDLE_TOKEN@@"
if (Test-Path -LiteralPath "$OMS_DIR/credentials") {
    $fromFile = (Read-TextFile "$OMS_DIR/credentials").Trim()
    if ($fromFile) { $OMS_MCP_TOKEN = $fromFile }
}
'''


class LocalPublishingRenderer:
    """The complete local publication support files, without remote workflows."""

    install_script = staticmethod(render_install_script)
    install_ps1 = staticmethod(render_install_ps1)
    contribute_tool = staticmethod(render_contribute_tool)
    readme = staticmethod(render_readme)
