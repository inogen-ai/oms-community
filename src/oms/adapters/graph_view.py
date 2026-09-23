"""Safe, endpoint-neutral graph records shared by the two core adapters."""
from enum import Enum

GRAPH_LABELS = ("Skill", "Rule", "Transaction", "Section", "ContentBlock", "Artefact", "Example", "Constraint", "Tag")


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
    # Payload references are internal storage addresses, not graph evidence.
    props = {k: plain(v) for k, v in properties.items()
             if k not in ("embedding", "sanitised_payload_ref", "workflow_safety_digest")}
    name = next((props[k] for k in ("name", "heading", "body", "summary") if props.get(k)), props["id"])
    return {"id": props["id"], "name": name, "kind": label.lower(), "properties": props}
