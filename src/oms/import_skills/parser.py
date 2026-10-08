from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from oms.domain.models import TenantVocabulary
from oms.domain.repo import (
    REPO_ID_PREFIX, ReservedSkillId, reserved_id_refusal,
)
from oms.domain.types import (
    ArtefactKind, ExampleKind, Mutability, Polarity, SectionKind, mutability_for,
)
from oms.import_skills.classifier import DefaultSectionClassifier
from oms.import_skills.vocabulary import load_default_vocabulary
from oms.publish.render import REFERENCES_FILE
from oms.ports.section_classifier import (
    ClassificationCandidate, SectionClassifier,
)

_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")   # bullet or numbered item
_KV = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")
_YAML_ITEM = re.compile(r"^-\s+(.*)$")                    # a block-list entry under a key
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")                # markdown horizontal rule
_FENCE = re.compile(r"^\s*(```|~~~)")                     # fenced code block delimiter

# Polarity cues.
_PROSCRIBE_HEADING = re.compile(
    r"(?i)anti.?pattern|avoid|don[''’]?t|do\s*not|never|gotcha|pitfall|common\s+mistake|"
    r"what\s+not\s+to\s+do|bad\s+practice"
)
_NEGATIVE_LEAD = re.compile(r"(?i)^\s*(don[''’]?t|do\s*not|never|avoid)\b")
_AVOID_PREFIX = re.compile(r"(?i)^\s*avoid\s*:\s*")
_PROSCRIBE_SYMBOL = re.compile(r"^\s*(❌|🚫|🛑)\s*")
_PRESCRIBE_SYMBOL = re.compile(r"^\s*(✅|✔)\s*")

# Example cues.
_EXAMPLE_HEADING = re.compile(
    r"(?i)\bexample(s)?\b|\bcase\s*stud(y|ies)\b|\bworked\s+example\b|\bdemo\b|\billustration\b"
)
_GOOD_MARKER = re.compile(r"^\*\*\s*good\s*:?\s*\*\*\s*:?\s*$", re.IGNORECASE)
_BAD_MARKER = re.compile(r"^\*\*\s*bad\s*:?\s*\*\*\s*:?\s*$", re.IGNORECASE)
_INDENTED_EXAMPLE = re.compile(
    r"^(?P<indent>\s+)(?P<kind>Example|Counter-example)\s*:\s*(?P<body>.+\S)\s*$"
)
_COUNTER_PREFIX = re.compile(r"(?i)^\s*counter-example\s*:\s*")


@dataclass
class ParsedExample:
    body: str
    kind: ExampleKind = ExampleKind.POSITIVE
    name: str | None = None              # heading-derived: "Pricing Page Test"
    original_label: str | None = None    # authorial marker: "Strong", "Sample Input"
    rule_index: int | None = None        # set only for per-rule attachment


@dataclass
class ParsedRule:
    body: str
    polarity: Polarity = Polarity.PRESCRIBE
    reference_only: bool = False
    tags: list[str] = field(default_factory=list)
    examples: list[ParsedExample] = field(default_factory=list)
    group: str | None = None    # the ### subsection heading the rule sat under, if any


@dataclass
class ParsedRoutingTarget:
    skill_id: str
    prose: str


@dataclass
class ParsedReferenceTarget:
    path: str
    prose: str


@dataclass
class ParsedSection:
    """A `##` block. Kind drives mutability and merge behaviour."""
    kind: SectionKind
    heading: str
    order: int
    mutability: Mutability
    rules: list[ParsedRule] = field(default_factory=list)
    examples: list[ParsedExample] = field(default_factory=list)
    body: str = ""
    routing_targets: list[ParsedRoutingTarget] = field(default_factory=list)
    reference_targets: list[ParsedReferenceTarget] = field(default_factory=list)
    classification_uncertain: bool = False
    classification_candidates: list[ClassificationCandidate] = field(default_factory=list)


#: Path components written by an operating system or a build tool rather than
#: by the skill's author. Every one of these arrives inside an ordinary upload -
#: Finder writes .DS_Store into each folder it displays, "Compress" mirrors the
#: tree under __MACOSX, a zip made from a checkout carries .git - and each used
#: to become an Artefact: stored as a blob, restored into the skill directory
#: at publish, and shipped to every machine in the organisation. One of them
#: also leaks a directory listing, since .DS_Store records the names of files
#: that were in the folder, including deleted ones.
#:
#: Names only, never extensions. A skill legitimately ships .py scripts, .css
#: and .png; what it never ships is bytecode, a thumbnail cache, or a resource
#: fork. The test is who wrote the file, not what type it is.
OS_DEBRIS_NAMES = frozenset({
    ".DS_Store", ".Spotlight-V100", ".Trashes", ".fseventsd", "__MACOSX",
    "Thumbs.db", "ehthumbs.db", "desktop.ini",
    "__pycache__", ".git",
})


def is_os_debris(name: str) -> bool:
    """Whether one path component was written by a tool rather than an author.

    Shared with `scripts/migrate_drop_os_debris.py`, which removes what got in
    before this existed: two definitions of "debris" would eventually disagree
    about what the filter lets through and what the cleanup takes out.
    """
    if name in OS_DEBRIS_NAMES:
        return True
    # macOS writes ._<name> beside <name> on any non-HFS filesystem, holding
    # that file's resource fork. It is never the file itself. A name that
    # merely opens with an underscore (_variables.css) is an author's choice
    # and is kept, which is why this tests the pair and not the character.
    return name.startswith("._")


def _under_debris(path: Path, root: Path) -> bool:
    """Whether any component of `path` below `root` is debris.

    Every component, not just the last: the bytecode is fine as a name and
    damning as `scripts/__pycache__/x.pyc`.
    """
    return any(is_os_debris(part) for part in path.relative_to(root).parts)


@dataclass
class ParsedArtefact:
    """A file alongside SKILL.md: script, reference markdown, README, fixture, OTHER."""
    name: str
    path: str            # relative to skill root
    kind: ArtefactKind
    body: bytes
    source_ref: str
    mode: int | None = None


@dataclass(frozen=True)
class ParsedFile:
    path: str
    digest: str
    size: int
    mode: int | None


@dataclass
class ParsedSkill:
    id: str
    name: str
    description: str
    domain: str
    tags: list[str]
    rules: list[ParsedRule]            # back-compat: flattened from sections' rules
    source_ref: str
    examples: list[ParsedExample] = field(default_factory=list)
    sections: list[ParsedSection] = field(default_factory=list)
    artefacts: list[ParsedArtefact] = field(default_factory=list)
    frontmatter_body: str = ""
    import_name: str | None = None
    source_path: str | None = None
    declared_license: str | None = None
    source_digest: str | None = None
    source_mode: int | None = None
    package_manifest: list[ParsedFile] = field(default_factory=list)
    manifest_complete: bool = False


@dataclass
class ParsedConstraint:
    body: str
    polarity: Polarity = Polarity.PRESCRIBE


@dataclass
class ParsedImport:
    skills: list[ParsedSkill] = field(default_factory=list)
    constraints: list[ParsedConstraint] = field(default_factory=list)


# --- Example marker patterns -----------------------------------------------
# Recognised ONLY inside an examples context (an `examples`-kind section, an
# `### Example` / `### Sample Input` / etc. subheading, or directly after such
# a heading). Outside that context, `**X:**` is an attribute label, not a
# polarity marker.

_BOLD_LABEL = re.compile(
    r"^\*\*\s*(?P<label>[^*]+?)\s*\*\*\s*:?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_SUBHEADING_EXAMPLE = re.compile(
    r"^(?P<marker>example|case\s*study|sample\s*input|sample\s*output|"
    r"worked\s*example|illustration|before\s*/\s*after\s+example|"
    r"full\s+example|counter-example|anti-example|results?\s+after\b)"
    r"(?:\s*[0-9]+)?(?:\s*[:\-]\s*(?P<name>.+))?$",
    re.IGNORECASE,
)
# Distinguishes ### Example 1: Foo from ### 1. Foo
_NUMBERED_SUBSECTION = re.compile(r"^\d+\.\s+\S")
# A bold label that reads as an instruction ('**Always use British English**')
# is guidance, not a group marker: keep it a rule despite being fully bold.
_LABEL_IMPERATIVE = re.compile(
    r"^(always|never|don[''’]?t|do not|avoid|use|prefer|lead|ensure|write|keep|"
    r"make|provide|include|match|start|open|close|add|remove|check|set)\b",
    re.IGNORECASE,
)


def _bold_group_label(text: str) -> str | None:
    """The label of a bullet that is ONLY a bold span with nothing after it -
    a structural group marker like '**Hero (Above the Fold)**' - else None.
    A bold lead-in with trailing guidance ('**Front-load**: put it early') and
    a fully-bold imperative ('**Always use X**') both read as rules, so return
    None for them. A trailing colon inside the label is dropped."""
    m = _BOLD_LABEL.match(text.strip())
    if m is None or m.group("rest").strip():
        return None
    label = m.group("label").strip().rstrip(":").strip()
    if not label or _LABEL_IMPERATIVE.match(label):
        return None
    return label


def _unquote(value: str) -> str:
    """Strip surrounding quotes, so `name: "x"` and `name: x` are equivalent,
    and drop a backslash written in front of an inner quote.

    The unescaping is not YAML's rule, deliberately. In a plain scalar YAML
    keeps `\\"` as two characters, and that is exactly what reached the
    console: a skill whose description was authored with JSON escaping
    rendered its quotes as `\\"write copy for,\\"` on the Details tab, because
    the string really did contain the backslashes. A lone backslash before a
    quote means nothing anyone wants in a description, in either the quoted or
    the plain spelling, so it is read here as the escape it was meant to be
    rather than carried into the graph and shown.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\'", "'")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter fields, body with frontmatter removed).

    YAML scalar values retain their text. Lists are returned as the same
    comma-separated string used by older packages:

        tags:            is read exactly as    tags: marketing, seo
          - marketing
          - seo

    The raw source block remains available through `_extract_frontmatter_block`
    for import provenance. Loose legacy key/value syntax is still accepted.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    # Read YAML's folded/literal blocks, quoted continuations and lists as
    # values, rather than mistaking `description: >` for the description.
    # BaseLoader constructs only strings, lists and mappings: it cannot
    # instantiate Python objects or turn names such as "on" into booleans.
    try:
        metadata = yaml.load(m.group(1), Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        metadata = None
    if isinstance(metadata, dict):
        fields = {}
        for key, value in metadata.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, list):
                value = ", ".join(item.strip() for item in value if isinstance(item, str))
            if isinstance(value, str):
                fields[key] = value.strip().replace('\\"', '"').replace("\\'", "'")
        return fields, text[m.end():]

    # Older skill packages also use loose key/value frontmatter, including
    # unquoted colons and JSON-style quote escaping. Keep accepting it.
    fields: dict[str, str] = {}
    listing: str | None = None   # the key an open block list is filling, if any
    for line in m.group(1).splitlines():
        stripped = line.strip()
        item = _YAML_ITEM.match(stripped)
        if item is not None and listing is not None:
            value = _unquote(item.group(1).strip())
            fields[listing] = f"{fields[listing]}, {value}" if fields[listing] else value
            continue
        kv = _KV.match(stripped)
        if kv is None:
            continue
        value = _unquote(kv.group(2).strip())
        fields[kv.group(1)] = value
        # `tags:` with nothing after it opens a list; any key with a value on
        # the line closes one, so a stray dash cannot append to the wrong key.
        listing = kv.group(1) if value == "" else None
    return fields, text[m.end():]


def _classify(text: str, section_pol: Polarity) -> ParsedRule:
    """Apply per-bullet polarity cues on top of the section default."""
    polarity = section_pol
    if _PROSCRIBE_SYMBOL.match(text):
        polarity = Polarity.PROSCRIBE
        text = _PROSCRIBE_SYMBOL.sub("", text, count=1).strip()
    elif _PRESCRIBE_SYMBOL.match(text):
        polarity = Polarity.PRESCRIBE
        text = _PRESCRIBE_SYMBOL.sub("", text, count=1).strip()
    if _AVOID_PREFIX.match(text):
        polarity = Polarity.PROSCRIBE
        text = _AVOID_PREFIX.sub("", text, count=1).strip()
    elif polarity is Polarity.PRESCRIBE and _NEGATIVE_LEAD.match(text):
        polarity = Polarity.PROSCRIBE
    return ParsedRule(body=text, polarity=polarity)


def walk_body(body: str) -> tuple[list[ParsedRule], list[ParsedExample]]:
    """Walk a skill body and emit:
       - the list of rules (with examples attached when authorial context binds them),
       - a list of skill-level orphan examples (examples appearing under an Examples
         heading before any rule was emitted)."""
    rules: list[ParsedRule] = []
    orphan_examples: list[ParsedExample] = []
    para: list[str] = []
    heading_stack: list[str] = []
    in_fence = False
    fence_buf: list[str] = []
    in_examples_section = False
    next_example_kind: ExampleKind | None = None
    subheading: str | None = None  # deepest ### (or lower) heading: the rule's group
    section_floor = 0  # rules before this index belong to a previous ## section

    def section_polarity() -> Polarity:
        for h in reversed(heading_stack):
            if _PROSCRIBE_HEADING.search(h):
                return Polarity.PROSCRIBE
        return Polarity.PRESCRIBE

    def emit_rule(text: str) -> None:
        nonlocal subheading
        # A bullet that is only a bold label ('**Hero (Above the Fold)**') is a
        # structural group marker, not guidance: fold it into the current
        # subheading so the rules beneath it are grouped (and it republishes as
        # a `###`), instead of storing a phantom rule that pollutes dedup
        # and the review queue.
        label = _bold_group_label(text)
        if label is not None:
            subheading = label
            return
        pr = _classify(text, section_polarity())
        # _classify can strip a bare lead-in (e.g. a lone 'Avoid:') down to
        # nothing; an empty body must never become a rule, or the importer
        # embeds '' and dedup matches every other empty embedding to it.
        if not pr.body.strip():
            return
        pr.group = subheading
        rules.append(pr)

    def attach_example(body_text: str, kind: ExampleKind) -> None:
        # An example binds to the most recent rule, but never across a `##`
        # section boundary; with no rule in the current section it is a
        # skill-level orphan instead.
        if len(rules) > section_floor:
            rules[-1].examples.append(ParsedExample(body=body_text, kind=kind))
        else:
            orphan_examples.append(ParsedExample(body=body_text, kind=kind))

    def flush_para() -> None:
        nonlocal para, next_example_kind
        if not para:
            return
        text = " ".join(para).strip()
        para = []
        if in_examples_section:
            kind = next_example_kind or ExampleKind.POSITIVE
            if _COUNTER_PREFIX.match(text):
                kind = ExampleKind.NEGATIVE
            cleaned = _COUNTER_PREFIX.sub("", text, count=1).strip()
            attach_example(cleaned, kind)
            next_example_kind = None
        else:
            emit_rule(text)

    for line in body.splitlines():
        if _FENCE.match(line):
            if in_fence and fence_buf:
                kind = next_example_kind or ExampleKind.POSITIVE
                attach_example("\n".join(fence_buf).strip(), kind)
                next_example_kind = None
                fence_buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            if in_examples_section:
                fence_buf.append(line)
            continue

        stripped = line.strip()

        if stripped.startswith("#"):
            flush_para()
            depth = len(stripped) - len(stripped.lstrip("#"))
            heading_stack[:] = heading_stack[: depth - 1]
            heading_text = stripped.lstrip("# ").strip()
            heading_stack.append(heading_text)
            in_examples_section = any(_EXAMPLE_HEADING.search(h) for h in heading_stack)
            subheading = heading_text if depth >= 3 else None
            if depth <= 2:
                section_floor = len(rules)
            continue

        if in_examples_section and _GOOD_MARKER.match(stripped):
            flush_para()
            next_example_kind = ExampleKind.POSITIVE
            continue
        if in_examples_section and _BAD_MARKER.match(stripped):
            flush_para()
            next_example_kind = ExampleKind.NEGATIVE
            continue

        if (not stripped or stripped.startswith("|")
                or _HR.match(line) or REFERENCES_FILE in stripped):
            flush_para()
            continue

        inline = _INDENTED_EXAMPLE.match(line)
        if inline and len(rules) > section_floor:
            flush_para()
            kind = ExampleKind.NEGATIVE if inline.group("kind").lower() == "counter-example" else ExampleKind.POSITIVE
            rules[-1].examples.append(ParsedExample(body=inline.group("body").strip(), kind=kind))
            continue

        item = _ITEM.match(line)
        if item:
            flush_para()
            text = item.group(1).strip()
            if in_examples_section:
                kind = next_example_kind or ExampleKind.POSITIVE
                if _COUNTER_PREFIX.match(text):
                    kind = ExampleKind.NEGATIVE
                    text = _COUNTER_PREFIX.sub("", text, count=1).strip()
                attach_example(text, kind)
                next_example_kind = None
            else:
                emit_rule(text)
            continue

        para.append(stripped)

    flush_para()
    return rules, orphan_examples


# --- Section-aware walker --------------------------------------------------

def _example_label_to_kind(label: str, vocab: TenantVocabulary) -> ExampleKind | None:
    """Map an authorial example label (case-insensitive) to an ExampleKind via
    the tenant vocabulary. Returns None if the label is not a recognised marker."""
    label_low = label.strip().lower().rstrip(":")
    markers = vocab.body.get("example_markers", {})
    for kind_value, labels in markers.items():
        if any(label_low == lbl.lower() for lbl in labels):
            return ExampleKind(kind_value)
    return None


def _is_examples_subheading(heading_text: str) -> tuple[bool, str | None, ExampleKind]:
    """Return (is_example_subheading, name, kind_hint) for a `### ...` heading.

    A heading is treated as an example block when it starts with one of the
    marker words (Example, Case Study, Sample Input/Output, Worked Example,
    Illustration, Counter-example, Anti-example). The trailing text after
    `:` or `-` becomes the name. A `### 1. Foo` bare-numbered heading is not
    treated as an example."""
    # Discount bare-numbered subsections.
    if _NUMBERED_SUBSECTION.match(heading_text):
        return False, None, ExampleKind.POSITIVE
    m = _SUBHEADING_EXAMPLE.match(heading_text.strip())
    if not m:
        return False, None, ExampleKind.POSITIVE
    marker = (m.group("marker") or "").lower()
    name = (m.group("name") or "").strip() or None
    if "counter" in marker or "anti-example" in marker:
        kind = ExampleKind.NEGATIVE
    elif "sample" in marker or "results after" in marker:
        kind = ExampleKind.NEUTRAL
    else:
        kind = ExampleKind.POSITIVE
    return True, name, kind


def _routing_target_from_bullet(text: str) -> ParsedRoutingTarget | None:
    """A routing bullet looks like `- **skill-name** - WHEN: ... ; WHEN NOT: ...`
    or `- **skill-name**: ...`. Return (skill_name, full_prose) or None."""
    m = re.match(r"^\s*\*\*([a-z][a-z0-9-]+)\*\*\s*[:\-—]?\s*(.*)$", text)
    if not m:
        return None
    return ParsedRoutingTarget(skill_id=m.group(1), prose=m.group(2).strip())


def _reference_target_from_bullet(text: str) -> ParsedReferenceTarget | None:
    """A reference bullet looks like `- [Title](references/foo.md): prose...`
    or `- references/foo.md - prose...`. Returns (path, prose) when a path
    ending in `.md` is present."""
    m = re.search(r"\[(?P<title>[^\]]+)\]\((?P<path>[^)]+\.md)\)", text)
    if m:
        prose = text.replace(m.group(0), "").strip().lstrip(":-—").strip()
        return ParsedReferenceTarget(path=m.group("path"), prose=prose)
    m = re.search(r"(?P<path>[A-Za-z0-9_./-]+\.md)", text)
    if m:
        return ParsedReferenceTarget(path=m.group("path"), prose=text)
    return None


def walk_skill_body_sections(
    body: str,
    vocabulary: TenantVocabulary | None = None,
    classifier: SectionClassifier | None = None,
    skill_id: str = "",
) -> list[ParsedSection]:
    """Walk a SKILL.md body into a list of ParsedSection.

    Each `##` heading opens a new section, classified by the SectionClassifier
    against the tenant vocabulary. `###` subheadings stay inside the current
    section. The H1 (skill title) is skipped; content between the H1 and the
    first `##` becomes an '(intro)' section, classified like any other (so a
    flat bullet-list skill with no `##` headings still yields a rules section).
    """
    vocabulary = vocabulary or TenantVocabulary(
        tenant_id="(default)", version=0, body=load_default_vocabulary())
    classifier = classifier or DefaultSectionClassifier()

    lines = body.splitlines()
    i = 0
    n = len(lines)

    # Skip leading H1 if present.
    while i < n and not lines[i].strip():
        i += 1
    if i < n and lines[i].lstrip().startswith("# ") and not lines[i].lstrip().startswith("## "):
        i += 1   # consume the H1

    # Split into (heading, body) segments: the intro, then each `##` block.
    segments: list[tuple[str, str]] = []
    intro_buf: list[str] = []
    while i < n:
        stripped = lines[i].lstrip()
        if stripped.startswith("## "):
            break
        intro_buf.append(lines[i])
        i += 1
    if any(line.strip() for line in intro_buf):
        segments.append(("(intro)", "\n".join(intro_buf).strip()))
    while i < n:
        stripped = lines[i].lstrip()
        if not stripped.startswith("## "):
            i += 1
            continue
        heading = stripped[3:].strip()
        i += 1
        body_lines: list[str] = []
        while i < n and not lines[i].lstrip().startswith("## "):
            body_lines.append(lines[i])
            i += 1
        segments.append((heading, "\n".join(body_lines)))

    sections: list[ParsedSection] = []
    for order, (heading, section_body) in enumerate(segments):
        result = classifier.classify(
            heading=heading,
            body_preview=section_body[:500],
            parent_skill_id=skill_id,
            parent_kind=None,
            vocabulary=vocabulary,
        )
        kind = result.kind
        section = ParsedSection(
            kind=kind, heading=heading, order=order,
            mutability=mutability_for(kind),
            body=section_body,
            classification_uncertain=result.uncertain,
            classification_candidates=list(result.candidates),
        )

        # Per-kind dispatch.
        if kind is SectionKind.RULES:
            # Prepend a synthetic `#` heading so walk_body's polarity heuristics
            # see the section heading (e.g. 'Anti-Patterns' -> PROSCRIBE).
            synthetic = f"# {heading}\n\n" + section_body
            sec_rules, sec_orphans = walk_body(synthetic)
            if not sec_rules and not sec_orphans and section_body.strip():
                # Nothing extractable (e.g. the content is a table): a rules
                # classification would discard the body entirely. Demote to
                # authorial prose so the section round-trips verbatim.
                section.kind = SectionKind.PROSE
                section.mutability = mutability_for(SectionKind.PROSE)
            else:
                section.rules = sec_rules
                section.examples = sec_orphans
            for ex in section.examples:
                if ex.kind not in (ExampleKind.POSITIVE, ExampleKind.NEGATIVE):
                    ex.kind = ExampleKind.POSITIVE
        elif kind is SectionKind.EXAMPLES:
            section.examples = _walk_examples_section_body(section_body, vocabulary)
        elif kind is SectionKind.ROUTING:
            for line in section_body.splitlines():
                m = _ITEM.match(line)
                if m is None:
                    continue
                tgt = _routing_target_from_bullet(m.group(1))
                if tgt is not None:
                    section.routing_targets.append(tgt)
        elif kind is SectionKind.REFERENCE_POINTER:
            for line in section_body.splitlines():
                m = _ITEM.match(line)
                if m is None:
                    continue
                ref = _reference_target_from_bullet(m.group(1))
                if ref is not None:
                    section.reference_targets.append(ref)
        # All other authorial kinds: body is captured verbatim, no further parsing.

        sections.append(section)
    return sections


def _walk_examples_section_body(body: str, vocab: TenantVocabulary) -> list[ParsedExample]:
    """Parse the body of an `examples`-kind section.

    Handles three patterns:
      - `### Example` / `### Example: <name>` / `### Example N:` / `### Sample Input`
        subheadings open a new example block; the heading-derived name is captured.
      - Inside such a subheading, `**Good:**` / `**Bad:**` / `**Strong:**` / etc.
        line markers split into separate examples with the marker as
        `original_label` and the polarity from the vocabulary.
      - A bare bullet outside any label block is one example on its own
        (with `Counter-example:` polarity detection), so the graph carries
        per-example polarity; the sectioned renderer reconstructs the list
        from unlabelled one-liners.
      - Tables and other authorial content are preserved as part of the most
        recent example's body (NOT extracted as discrete examples per row).
    """
    examples: list[ParsedExample] = []
    current_heading_name: str | None = None
    current_heading_kind = ExampleKind.POSITIVE
    current_body: list[str] = []
    current_label: str | None = None
    current_kind: ExampleKind = ExampleKind.POSITIVE

    in_fence = False

    def flush_current_block() -> None:
        nonlocal current_body, current_label, current_kind
        if current_body and any(line.strip() for line in current_body):
            text = "\n".join(current_body).strip()
            examples.append(ParsedExample(
                body=text,
                kind=current_kind if current_label else current_heading_kind,
                name=current_heading_name,
                original_label=current_label,
            ))
        current_body = []
        current_label = None
        current_kind = current_heading_kind

    for line in body.splitlines():
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
            current_body.append(line)
            continue
        if in_fence:
            current_body.append(line)
            continue

        stripped = line.lstrip()

        if stripped.startswith("### "):
            # New subheading -> flush and open new block.
            flush_current_block()
            heading_text = stripped[4:].strip()
            is_ex, name, kind_hint = _is_examples_subheading(heading_text)
            current_heading_name = name if is_ex else heading_text
            current_heading_kind = kind_hint if is_ex else ExampleKind.POSITIVE
            current_kind = current_heading_kind
            continue

        item = _ITEM.match(line)
        if item is not None and current_label is None and current_heading_name is None:
            flush_current_block()
            text = item.group(1).strip()
            kind = current_heading_kind
            if _COUNTER_PREFIX.match(text):
                kind = ExampleKind.NEGATIVE
                text = _COUNTER_PREFIX.sub("", text, count=1).strip()
            examples.append(ParsedExample(body=text, kind=kind, name=current_heading_name))
            continue

        # Recognise inline bold-label markers (only within an examples context).
        m = _BOLD_LABEL.match(stripped) if not in_fence else None
        if m is not None:
            label = m.group("label").strip()
            inferred = _example_label_to_kind(label, vocab)
            if inferred is not None:
                flush_current_block()
                current_label = label
                current_kind = inferred
                rest = m.group("rest").strip()
                if rest:
                    current_body.append(rest)
                continue

        current_body.append(line)

    flush_current_block()
    return examples


def _tags(fields: dict[str, str]) -> list[str]:
    raw = fields.get("tags", "")
    return [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]


_H1_TITLE = re.compile(r"^#\s+(.+?)\s*$")


def _extract_title(body: str) -> str | None:
    """The first top-level ATX heading (`# Title`) is the human-readable skill
    title; the frontmatter `name` slug remains the stable id."""
    for line in body.splitlines():
        m = _H1_TITLE.match(line)
        if m:
            return m.group(1).strip()
    return None


def parse_skill_file(
    text: str,
    source_ref: str,
    reference_only: bool = False,
    vocabulary: TenantVocabulary | None = None,
    classifier: SectionClassifier | None = None,
) -> ParsedSkill | None:
    fields, body = parse_frontmatter(text)
    name = fields.get("name")
    if not name:
        return None
    # The frontmatter name is provisional. Whole-import identity resolution
    # assigns the storage ID after parsing, preserving package namespaces.
    # A human wrote this file and can pick another name, so this raises rather
    # than silently skipping the file (returning None here) or renaming it
    # behind their back: either would leave a skill they authored missing from
    # the graph with nothing to explain why.
    if name.startswith(REPO_ID_PREFIX):
        # `ReservedSkillId`, not a bare `ValueError`: `UploadService.stage`
        # catches this one by name to turn it into an `UploadRefused`, which is
        # the only exception either upload router handles. Catching plain
        # `ValueError` there would swallow every unrelated parser fault and
        # report it to the uploader as a bad archive.
        raise ReservedSkillId(reserved_id_refusal(name))
    tags = _tags(fields)
    domain = _extract_domain(name, vocabulary, fields)
    # Section-aware view: the source of truth for what becomes a Rule.
    sections = walk_skill_body_sections(
        body, vocabulary=vocabulary, classifier=classifier, skill_id=name,
    )
    # Rules are extracted only from rules-kind sections; authorial sections
    # (prose, templates, checklists, routing, ...) are carried verbatim by
    # their content blocks and must not shadow the rule layer.
    rules = [r for sec in sections if sec.kind is SectionKind.RULES for r in sec.rules]
    # `reference_only` is a fact about where this file sits (a references/*.md
    # long-tail rule), so it belongs on every rule the file yields. The skill's
    # tags used to be stamped on here too, and they do not: `tags: legal` in
    # frontmatter says the SKILL is about legal, and copying it onto forty rules
    # asserts that each of the forty is, which is stronger and usually false.
    # They are attached to the Skill instead (SkillImporter._import_tags).
    # ParsedRule.tags stays, and the merge step still honours it, for tags that
    # are genuinely per-rule; the current file format does not express those.
    for pr in rules:
        pr.reference_only = reference_only
    # Skill-level orphan examples come from the same section walk: RULES
    # sections contribute their unbound examples, EXAMPLES sections all of
    # theirs. A second flat pass over the body would parse the same content
    # again with subtly different results (fenced bodies flatten differently).
    orphan_examples = [ex for sec in sections for ex in sec.examples]
    title = _extract_title(body) or name
    return ParsedSkill(
        id=name, name=title, description=" ".join(fields.get("description", "").split()),
        domain=domain, tags=tags, rules=rules, source_ref=source_ref,
        examples=orphan_examples, sections=sections, frontmatter_body=_extract_frontmatter_block(text),
        source_path=source_ref, declared_license=declared_license(text),
        source_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def declared_license(text: str) -> str | None:
    """Retain a supported YAML string scalar exactly; absence is checked separately."""
    raw = _extract_frontmatter_block(text)
    try:
        document = yaml.compose(raw, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(document, yaml.MappingNode):
        return None
    values = [value for key, value in document.value
              if isinstance(key, yaml.ScalarNode) and key.value == "license"]
    if len(values) == 1 and isinstance(values[0], yaml.ScalarNode) and values[0].tag == "tag:yaml.org,2002:str":
        return values[0].value
    return None


def _file_mode(path: Path) -> int | None:
    if os.name == "nt" or path.is_symlink():
        return None
    return 0o100755 if path.stat().st_mode & 0o111 else 0o100644


def _extract_domain(name: str, vocab: TenantVocabulary | None, fields: dict[str, str]) -> str:
    extractor = (vocab.body.get("domain_extractor", "hyphen_prefix")
                 if vocab is not None else "hyphen_prefix")
    if extractor == "explicit_or_general":
        return fields.get("domain", "").strip() or "general"
    if extractor == "hyphen_prefix":
        return name.split("-", 1)[0]
    if extractor == "dot_prefix":
        return name.split(".", 1)[0]
    if extractor == "explicit_field" and vocab is not None:
        field_name = vocab.body.get("domain_extractor_field")
        if field_name and field_name in fields:
            return fields[field_name]
        return name.split("-", 1)[0]    # fallback
    if extractor == "none":
        return ""
    return name.split("-", 1)[0]


def _extract_frontmatter_block(text: str) -> str:
    """Return the raw frontmatter block (between the two `---` markers), or ''."""
    m = _FRONTMATTER.match(text)
    return m.group(1) if m else ""


def parse_references(text: str) -> list[ParsedRule]:
    """Rules from the publisher-owned overflow file (REFERENCES_FILE) are
    long-tail: reference_only=True. Called only for that file - author-owned
    reference markdown is carried as an Artefact, never parsed into rules.
    Skill-level orphan examples are discarded (no parent skill context to
    attach them to here)."""
    rules, _orphans = walk_body(text)
    for pr in rules:
        pr.reference_only = True
    return rules


def parse_constraints(claude_md: str) -> list[ParsedConstraint]:
    """Read constraint blocks under either a constraints heading (PRESCRIBE) or
    an anti-patterns heading (PROSCRIBE). Stops at the next heading. The skill
    index is ignored because publish regenerates it."""
    out: list[ParsedConstraint] = []
    polarity: Polarity | None = None
    for line in claude_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("# ").strip().lower()
            if "constraint" in heading:
                polarity = Polarity.PRESCRIBE
            elif "anti-pattern" in heading or "antipattern" in heading:
                polarity = Polarity.PROSCRIBE
            else:
                polarity = None
            continue
        if polarity is None:
            continue
        # Standard bullets ('* X', '- X', '1. X') and bare polarity-symbol lines.
        item = _ITEM.match(line)
        body: str | None = None
        item_polarity = polarity
        if item is not None:
            body = item.group(1).strip()
        elif polarity is Polarity.PROSCRIBE:
            s = line.lstrip()
            if _PROSCRIBE_SYMBOL.match(s):
                body = s
        if body is None:
            continue
        if _PROSCRIBE_SYMBOL.match(body):
            body = _PROSCRIBE_SYMBOL.sub("", body, count=1).strip()
            item_polarity = Polarity.PROSCRIBE
        elif _PRESCRIBE_SYMBOL.match(body):
            body = _PRESCRIBE_SYMBOL.sub("", body, count=1).strip()
        if not body:
            continue    # a bare polarity symbol carries no constraint text
        out.append(ParsedConstraint(body=body, polarity=item_polarity))
    return out


def parse_directory(
    root: Path,
    vocabulary: TenantVocabulary | None = None,
    classifier: SectionClassifier | None = None,
    *,
    selected_roots: tuple[str, ...] | None = None,
    extract_constraints: bool = True,
) -> ParsedImport:
    """Walk a skill tree using the tenant vocabulary for layout discovery.

    Each primary-entry file (`SKILL.md` by default) is one skill. Constraint
    files (`CLAUDE.md` by default) yield organisation-level constraints. Files
    under directories named in `vocabulary.artefact_dirs` become typed
    `Artefact`s; files matching `vocabulary.documentation_files` (e.g.
    `README.md`) become `DOCUMENTATION` artefacts; anything else under a skill
    root becomes `OTHER`. The skill index in the constraint file is ignored
    because publish regenerates it.
    """
    root = Path(root)
    vocabulary = vocabulary or TenantVocabulary(
        tenant_id="(default)", version=0, body=load_default_vocabulary())
    classifier = classifier or DefaultSectionClassifier()

    result = ParsedImport()

    # Constraints from any file matching vocabulary.constraint_file_names.
    constraint_names = set(vocabulary.body.get("constraint_file_names", ["CLAUDE.md"]))
    for path in sorted(root.rglob("*")) if extract_constraints else ():
        if not path.is_file() or _under_debris(path, root):
            continue
        if path.name in constraint_names:
            result.constraints.extend(
                parse_constraints(path.read_text(encoding="utf-8")))

    # Primary entries (typically SKILL.md). Filtered too, and not only for
    # tidiness: macOS "Compress" mirrors the whole tree under __MACOSX, so an
    # unfiltered walk can find a second SKILL.md there and import a phantom
    # duplicate of the skill under a source_ref nobody can account for.
    entry_patterns = vocabulary.body.get("primary_entry_patterns", ["SKILL.md"])
    skill_files: list[Path] = []
    for pattern in entry_patterns:
        skill_files.extend(root.rglob(pattern))
    skill_files = sorted({f for f in skill_files if not _under_debris(f, root)})
    package_roots = {path.parent for path in skill_files}
    if selected_roots is not None:
        selected = _selected_package_roots(root, selected_roots, package_roots)
        skill_files = [path for path in skill_files if path.parent in selected]

    artefact_dirs: dict[str, list[str]] = vocabulary.body.get("artefact_dirs", {})
    # Reverse-lookup: dir_name -> ArtefactKind.
    dir_to_kind: dict[str, ArtefactKind] = {}
    for kind_value, dirs in artefact_dirs.items():
        for d in dirs:
            dir_to_kind[d] = ArtefactKind(kind_value)
    doc_names = set(vocabulary.body.get("documentation_files", ["README.md"]))

    for skill_md in skill_files:
        source_ref = skill_md.relative_to(root).as_posix()
        primary_bytes = skill_md.read_bytes()
        skill = parse_skill_file(
            primary_bytes.decode("utf-8"),
            source_ref=source_ref, vocabulary=vocabulary, classifier=classifier,
        )
        if skill is None:
            continue

        skill_root = skill_md.parent
        nested_roots = {path for path in package_roots if skill_root in path.parents}
        skill.source_digest = hashlib.sha256(primary_bytes).hexdigest()
        skill.source_mode = _file_mode(skill_md)
        package_files = [path for path in sorted(skill_root.rglob("*"))
                         if path.is_file() and not _under_debris(path, skill_root)
                         and not any(nested in path.parents for nested in nested_roots)]
        skill.manifest_complete = not any(path.is_symlink() for path in package_files)
        for path in package_files:
            body = primary_bytes if path == skill_md else path.read_bytes()
            skill.package_manifest.append(ParsedFile(path=path.relative_to(skill_root).as_posix(),
                digest=hashlib.sha256(body).hexdigest(), size=len(body), mode=_file_mode(path)))

        # Only the publisher-owned overflow file round-trips back into
        # reference_only rules; it is a flat rule list by construction, so
        # re-parsing it is lossless. Every other references/*.md is an
        # author-owned document whose custody is its byte-perfect Artefact
        # copy below - shredding one into rules destroys its structure and
        # floods the graph with phrase-bank fragments.
        overflow_md = skill_root / REFERENCES_FILE
        if overflow_md.is_file() and not any(path in overflow_md.parents for path in nested_roots):
            skill.rules.extend(parse_references(overflow_md.read_text(encoding="utf-8")))

        # Layout discovery for artefacts.
        for sub in sorted(skill_root.iterdir()):
            if is_os_debris(sub.name) or sub in nested_roots:
                continue
            if sub.is_file():
                if sub == skill_md:
                    continue
                kind = ArtefactKind.DOCUMENTATION if sub.name in doc_names else ArtefactKind.OTHER
                skill.artefacts.append(ParsedArtefact(
                    name=sub.name,
                    path=sub.name,
                    kind=kind,
                    body=sub.read_bytes(),
                    source_ref=sub.relative_to(root).as_posix(),
                    mode=_file_mode(sub),
                ))
            elif sub.is_dir():
                kind = dir_to_kind.get(sub.name, ArtefactKind.OTHER)
                for f in sorted(sub.rglob("*")):
                    if (not f.is_file() or _under_debris(f, skill_root)
                            or any(path in f.parents for path in nested_roots)):
                        continue
                    rel_path = f.relative_to(skill_root).as_posix()
                    # Publisher-owned: regenerated from overflow rules at
                    # every publish, so a captured copy would only go stale.
                    if rel_path == REFERENCES_FILE:
                        continue
                    skill.artefacts.append(ParsedArtefact(
                        name=f.name,
                        path=rel_path,
                        kind=kind,
                        body=f.read_bytes(),
                        source_ref=f.relative_to(root).as_posix(),
                        mode=_file_mode(f),
                    ))

        result.skills.append(skill)

    return result


def _selected_package_roots(root: Path, selections: tuple[str, ...],
                            available: set[Path]) -> set[Path]:
    selected: set[Path] = set()
    for value in selections:
        if (value.startswith("/") or "\\" in value or "\x00" in value
                or (value and any(part in {"", ".", ".."} for part in value.split("/")))):
            raise ValueError("Selected package must be a safe relative directory")
        path = root / value
        if path not in available or path in selected:
            raise ValueError("Selected package is missing or repeated")
        if any(parent.is_symlink() for parent in (path, *path.parents)
               if parent == root or root in parent.parents):
            raise ValueError("Selected package cannot follow a symbolic link")
        selected.add(path)
    return selected
