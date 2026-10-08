from dataclasses import dataclass, field
from enum import Enum

Vector = list[float]


class SignalType(str, Enum):
    EXPLICIT_CORRECTION = "explicit_correction"
    EDIT = "edit"
    RERUN = "rerun"
    THUMBS_DOWN = "thumbs_down"
    ABANDONMENT = "abandonment"
    TELEMETRY_ANOMALY = "telemetry_anomaly"
    SKILL_IMPORT = "skill_import"
    # An agent's own proposed learning, distinct from a person's correction.
    SELF_REFLECTION = "self_reflection"


# Preserve machine origin independently of the authenticated submitter.
MACHINE_ORIGINATED = frozenset({SignalType.SELF_REFLECTION})


class SourceRuntime(str, Enum):
    MCP = "mcp"
    HTTP = "http"           # posted through the HTTP front door (spec §6)
    MANUAL = "manual"


class CompileStatus(str, Enum):
    """Why a transaction is out of the pending pool. `pending_transactions`
    already filters on this property; existing nodes have none and are
    unaffected."""
    FAILED = "failed"                          # unprocessable; never retried
    AWAITING_ADMISSION = "awaiting_admission"  # unbound; needs a human (§9.2)
    # Scope review is distinct from admission; preserve both stored states.
    AWAITING_SCOPE_REVIEW = "awaiting_scope_review"


class RuleStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FLAGGED = "flagged"
    PENDING = "pending"     # standard writer's rule HELD for review; never published (G2)
    RETIRED = "retired"     # rejected in review or retracted; kept for lineage, never published


class ConstraintStatus(str, Enum):
    # Two members where RuleStatus has five, because a constraint has no
    # lifecycle to speak of: it is not compiled, not reviewed, and never
    # superseded by a competitor. It binds or it does not.
    ACTIVE = "active"
    RETIRED = "retired"     # withdrawn; kept so the record of what once bound survives


class ConstraintSource(str, Enum):
    """Who wrote a constraint, which decides whether the console may edit it.

    An imported constraint is a projection of a file in the source tree
    (`import_skills/parser.py` reads them out of CLAUDE.md), so retiring one
    from the console would last exactly until the next import put it back.
    The console shows those and refuses to touch them, which is a truthful
    answer rather than a button that silently un-presses itself.
    """
    IMPORT = "import"
    CONSOLE = "console"


class BlockStatus(str, Enum):
    # An authorial section has exactly one ACTIVE block; revisions supersede it
    # while the predecessor stays attached for audit (Section 6.5).
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class SkillStatus(str, Enum):
    ACTIVE = "active"     # accepted for use
    AUTO = "auto"         # automatically created, awaiting review
    FLAGGED = "flagged"   # needs review
    # A human turned the auto-creation down. Publish skips it, and the skill
    # stays in the graph rather than being deleted so the decision, and the
    # lineage of whatever was anchored to it, survive. Distinct from FLAGGED:
    # flagged means nobody has looked yet, and those publish normally.
    REJECTED = "rejected"


class SkillOrigin(str, Enum):
    """Who created a skill, as opposed to what state it is in.

    Separate from `SkillStatus` because the two answer different questions and
    have different lifetimes. A status changes: an AUTO skill becomes ACTIVE
    the moment somebody approves the `new_skill` review item, and with it the
    only evidence that OMS minted the skill rather than a person. An origin is
    a fact about the past and never changes.
    """
    IMPORTED = "imported"    # the skill importer, from a package
    AUTHORED = "authored"    # a person, through the console
    COMPILED = "compiled"    # created by automated processing


class SkillVersionCause(str, Enum):
    """What wrote a version. `UNATTRIBUTED` is the safety net: the versions
    read route captures-if-changed, so a write path that forgot to capture
    still leaves an honest row instead of a silent hole."""
    CONSOLE_EDIT = "console_edit"
    # Keep the editing surface visible in history independently of the actor.
    SELF_SERVE_EDIT = "self_serve_edit"
    RULE_EDIT = "rule_edit"
    UPLOAD_IMPORT = "upload_import"
    COMPILE = "compile"
    REVIEW_DECISION = "review_decision"
    RESTORE = "restore"
    CREATED = "created"
    UNATTRIBUTED = "unattributed"
    SOURCE_INSTALL = "source_install"
    SOURCE_UPDATE = "source_update"
    SOURCE_UNDO = "source_undo"


class EdgeType(str, Enum):
    DERIVED_FROM = "DERIVED_FROM"
    BELONGS_TO = "BELONGS_TO"
    TAGGED_WITH = "TAGGED_WITH"
    RELATED_TO = "RELATED_TO"
    DUPLICATE_OF = "DUPLICATE_OF"
    CONFLICTS_WITH = "CONFLICTS_WITH"       # rule -> immutable Constraint, within tenant: BLOCKS publish
    SUPERSEDES = "SUPERSEDES"
    ILLUSTRATES = "ILLUSTRATES"
    # Skill-package custody edges.
    HAS_SECTION = "HAS_SECTION"
    CONTAINS_RULE = "CONTAINS_RULE"
    CONTAINS_BLOCK = "CONTAINS_BLOCK"
    HAS_ARTEFACT = "HAS_ARTEFACT"
    HAS_EXAMPLE = "HAS_EXAMPLE"
    OBSERVED_IN = "OBSERVED_IN"
    ROUTES_TO = "ROUTES_TO"
    HAS_REFERENCE = "HAS_REFERENCE"
    # Cross-skill rule relationships (informational, never block publish).
    EQUIVALENT_TO = "EQUIVALENT_TO"
    RELATES_TO = "RELATES_TO"
    CROSS_SKILL_DIVERGENCE = "CROSS_SKILL_DIVERGENCE"


class Polarity(str, Enum):
    PRESCRIBE = "prescribe"     # do this
    PROSCRIBE = "proscribe"     # do NOT do this


class ExampleKind(str, Enum):
    POSITIVE = "positive"       # an example of doing the right thing
    NEGATIVE = "negative"       # an example of the anti-pattern
    NEUTRAL = "neutral"         # illustrative without polarity (Sample Input, etc.)


class SectionKind(str, Enum):
    RULES = "rules"
    EXAMPLES = "examples"
    TEMPLATE = "template"
    ROUTING = "routing"
    TRIGGER = "trigger"
    CHECKLIST = "checklist"
    PROSE = "prose"
    REFERENCE_POINTER = "reference_pointer"
    TAXONOMY = "taxonomy"
    FRONTMATTER = "frontmatter"


class ArtefactKind(str, Enum):
    SCRIPT = "script"
    REFERENCE_MD = "reference_md"
    TEMPLATE_FILE = "template_file"
    DOCUMENTATION = "documentation"
    FIXTURE = "fixture"
    OTHER = "other"


class Mutability(str, Enum):
    SYSTEM_AGGREGATED = "system_aggregated"
    AUTHORIAL_PASSTHROUGH = "authorial_passthrough"


# Section kinds whose content is aggregated (dedup/corroborate); everything else is authorial.
_AGGREGATED_SECTION_KINDS = {SectionKind.RULES, SectionKind.EXAMPLES}


def mutability_for(kind: SectionKind) -> Mutability:
    """Return the mutability mode for a section kind. The mapping is fixed by the spec."""
    return Mutability.SYSTEM_AGGREGATED if kind in _AGGREGATED_SECTION_KINDS else Mutability.AUTHORIAL_PASSTHROUGH


class Verdict(str, Enum):
    DUPLICATE = "duplicate"          # same instruction reworded; corroborates, never a new rule
    COMPATIBLE = "compatible"
    CONFLICTS_WITH = "conflicts_with"
    SUPERSEDES = "supersedes"
    AMBIGUOUS = "ambiguous"
    UNRELATED = "unrelated"        # different topics, no interaction; nothing is persisted




class Plane(str, Enum):
    """Stored content category; only DATA rules are eligible for publication."""
    DATA = "data"        # guidance for the people and agents doing the work
    CONTROL = "control"  # describes how OMS itself operates; never published






