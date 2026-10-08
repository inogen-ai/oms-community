"""The child process that parses a package's SKILL.md.

Run by `ProjectionBuilder` in a separate interpreter under a budget, reading the
request on stdin and writing the parsed skill on stdout, so untrusted document text
is never parsed inside the server process.
"""
import json
import sys

import yaml
from pydantic import TypeAdapter

from oms.domain.models import TenantVocabulary
from oms.domain.types import SectionKind
from oms.import_skills.classifier import DefaultSectionClassifier
from oms.import_skills.parser import ParsedSkill, _FRONTMATTER, parse_references, parse_skill_file
from oms.import_skills.vocabulary import load_default_vocabulary
from oms.ports.section_classifier import ClassificationCandidate, ClassificationResult


class InstalledKinds:
    """An installed section keeps its kind on refresh; only a new heading is classified."""
    def __init__(self, kinds: dict[str, str]):
        self.kinds = {heading: SectionKind(kind) for heading, kind in kinds.items()}
        self.default = DefaultSectionClassifier()

    def classify(self, heading, body_preview, parent_skill_id, parent_kind, vocabulary):
        kind = self.kinds.get(heading)
        if kind is None:
            return self.default.classify(heading, body_preview, parent_skill_id, parent_kind, vocabulary)
        return ClassificationResult(kind=kind, confidence=1.0, candidates=[ClassificationCandidate(kind, 1.0)])


def parse_request(request: dict) -> dict:
    text = request["text"]
    vocabulary = load_default_vocabulary()
    vocabulary["domain_extractor"] = "explicit_or_general"
    skill = parse_skill_file(text, source_ref=request["source_ref"],
        vocabulary=TenantVocabulary(tenant_id=request["tenant_id"], version=0, body=vocabulary),
        classifier=InstalledKinds(request.get("kinds") or {}))
    if skill is None:
        raise ValueError("Package does not contain a valid skill document")
    if request.get("overflow") is not None:
        skill.rules.extend(parse_references(request["overflow"]))
    skill.source_mode = request.get("mode")
    match = _FRONTMATTER.match(text)
    raw = match.group(1) if match else None
    licence = {"kind": "absent", "value": None}
    if raw is not None:
        try:
            document = yaml.compose(raw, Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            document = None
            licence["kind"] = "unknown"
        if isinstance(document, yaml.MappingNode):
            values = [value for key, value in document.value
                      if isinstance(key, yaml.ScalarNode) and key.value == "license"]
            if values:
                licence["kind"] = "unknown"
                if (len(values) == 1 and isinstance(values[0], yaml.ScalarNode)
                        and values[0].tag == "tag:yaml.org,2002:str"):
                    licence = {"kind": "known", "value": values[0].value}
        elif raw.strip():
            licence["kind"] = "unknown"
    return {"skill": TypeAdapter(ParsedSkill).dump_python(skill, mode="json"),
            "frontmatter": raw, "license": licence}


def main() -> None:
    # Parsing remains outside the service process, including YAML construction.
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    result = parse_request(json.load(sys.stdin))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
