"""Default tenant vocabulary.

The values shipped here cover the Anthropic SDK conventions used by the seed
corpus. They map organisation-specific words (heading text, marker labels, file
and directory names) to the universal concepts defined in `oms.domain.types`
(SectionKind, ExampleKind, Polarity, ArtefactKind).

A `TenantVocabulary` node is created per tenant by deep-copying these defaults
on first import. From that point on, the tenant's vocabulary is mutable; the
default is consulted only as a fallback when a tenant value for a field is
empty.
"""
from __future__ import annotations

import copy
from typing import Any


DEFAULT_VOCABULARY: dict[str, Any] = {
    # Heading patterns that signal each SectionKind. Case-insensitive substring
    # match; longer patterns win (more specific).
    "section_headings": {
        "rules": [
            "communication", "style", "practice", "rules", "guidelines",
            "principles", "standards", "conventions", "patterns",
            "best practices", "core principles", "writing style rules",
            "writing principles", "core writing principles",
            # Anti-patterns sections are rule-shaped but with PROSCRIBE polarity;
            # the parser's polarity heuristics catch the heading cue at bullet level.
            "anti-patterns", "anti-pattern", "antipatterns", "antipattern",
            "what to avoid", "common mistakes", "gotchas", "pitfalls",
        ],
        "examples": [
            "examples", "case studies", "case study", "worked examples",
            "worked example", "illustrations", "before/after examples",
            "before/after example", "evidence & examples", "full example",
            "sample input", "sample output", "demonstrations",
        ],
        "template": [
            "output format", "output schema", "report structure", "format",
            "output artifacts", "deliverables", "report template",
            "output artefacts",
        ],
        "routing": [
            "related skills", "see also", "cross-references", "cross references",
            "skill map", "routing", "use instead of",
        ],
        "trigger": [
            "proactive triggers", "when to use", "invocation", "activation",
            "triggers", "auto-invoke",
        ],
        "checklist": [
            "initial assessment", "pre-flight", "pre-audit", "setup",
            "preparation", "before starting", "task-specific questions",
            "pre-launch checklist",
        ],
        "reference_pointer": [
            "references", "resources", "further reading", "links",
        ],
        "taxonomy": [
            "tools referenced", "stack", "inventory", "categories",
            "tools and references",
        ],
        "prose": [],   # default; matches nothing explicitly, used as fallback
    },

    # Authorial labels for example polarity. Recognised only inside an examples
    # context (see SectionClassifier and parser).
    "example_markers": {
        "positive": ["good", "strong", "after", "do", "right", "correct"],
        "negative": ["bad", "weak", "before", "don't", "do not", "wrong",
                     "incorrect", "avoid", "never", "counter-example",
                     "anti-example"],
        "neutral":  ["sample input", "sample output", "input", "output",
                     "result", "example"],
    },

    # File and directory conventions.
    "primary_entry_patterns": ["SKILL.md", ".skill.md"],
    "constraint_file_names": ["CLAUDE.md", "AGENTS.md", "MEMORY.md",
                              "INSTRUCTIONS.md"],

    "artefact_dirs": {
        "script":        ["scripts", "tools", "bin", "helpers"],
        "reference_md":  ["references", "docs", "notes"],
        "template_file": ["templates", "layouts"],
        "fixture":       ["fixtures", "samples", "assets", "data"],
    },
    "documentation_files": ["README.md", "README", "CHANGELOG.md"],

    # Frontmatter handling.
    "domain_extractor": "hyphen_prefix",   # hyphen_prefix | dot_prefix | explicit_field | none
    "domain_extractor_field": None,

    # The classifier and cross-skill thresholds used to live here. They moved
    # to the tenant SETTINGS overlay (administrator settings design §5.3): the
    # vocabulary keeps what its name says - word mapping, consulted at import -
    # and the numbers that tune the pipeline live with the other numbers that
    # tune the pipeline. See oms/settings/models.py DEFAULT_SETTINGS.
}


def load_default_vocabulary() -> dict[str, Any]:
    """Return a fresh deep copy of the default vocabulary."""
    return copy.deepcopy(DEFAULT_VOCABULARY)
