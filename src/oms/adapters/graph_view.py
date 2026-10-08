"""Safe, endpoint-neutral graph records shared by the two core adapters."""
from enum import Enum
from urllib.parse import quote, unquote

GRAPH_LABELS = ("Skill", "Rule", "Transaction", "Section", "ContentBlock", "Artefact", "Example", "Constraint", "Tag")

# Payload references are internal storage addresses, not graph evidence.
# Session context is a contributor's own account of their work, served only
# by reads that check who may see it; the graph reads here check only the
# workspace, so they never carry it.
_OMITTED = frozenset({
    "embedding", "sanitised_payload_ref", "workflow_safety_digest",
    "session_summary", "project_name", "reuse_case", "learning_evidence",
})


def _escape(part):
    # Percent-encoding with `~` for `%`: a key part never holds a slash, colon
    # or percent sign, so it stays one path segment however often a proxy or
    # client decodes the URL.
    return quote(part, safe="").replace("~", "%7E").replace("%", "~")


def _unescape(part):
    return unquote(part.replace("~", "%"))


def graph_key(properties, label):
    """The explorer's name for one node, unambiguous within a workspace.

    Most nodes keep their id. A tag's id is its verbatim text, which a skill's
    slug can equal, and one file digest is stored once per occurrence, so
    those two are qualified instead.
    """
    if label == "Tag":
        return "tag:" + _escape(properties["id"])
    if label == "Artefact" and properties.get("owner_skill_id") and properties.get("occurrence_path"):
        return "artefact:%s:%s" % (_escape(properties["owner_skill_id"]), _escape(properties["occurrence_path"]))
    return properties["id"]


def parse_graph_key(key):
    """(label, parts) for a qualified key; None for a plain node id."""
    kind, _, rest = key.partition(":")
    if kind == "tag" and rest:
        return "Tag", (_unescape(rest),)
    skill, _, path = rest.partition(":")
    if kind == "artefact" and skill and path:
        return "Artefact", (_unescape(skill), _unescape(path))
    return None


def graph_record(properties, label):
    def plain(value):
        if isinstance(value, Enum):
            return value.value
        if hasattr(value, "to_native"):
            return value.to_native().isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, (set, frozenset, tuple, list)):
            return [plain(v) for v in value]
        return value
    props = {k: plain(v) for k, v in properties.items() if k not in _OMITTED}
    name = next((props[k] for k in ("name", "heading", "body", "summary") if props.get(k)), props["id"])
    return {"id": graph_key(props, label), "name": name, "kind": label.lower(), "properties": props}
