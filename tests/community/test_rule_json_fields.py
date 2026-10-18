"""The rules the Community API serves say where a split copy came from.

`split_from` and `split_at` are set only by the schema 3 split. Every rule the
API serves carries both keys: null for a rule that was never split, and for a
copy the split made, the original rule's id and the time of the split. A
client that reads rules can rely on the keys being there."""
from datetime import datetime, timezone

from tests.community.test_todo_critic import (
    ORIGIN, _detail, _seed_rule, _seed_skill,
    critic_neo4j_driver, workspace,  # noqa: F401: shared parametrised fixtures
)
from tests.conformance.test_copy_primitives import _mark_split

SPLIT_AT = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)


def _as_served(workspace, skill_id: str, rule_id: str) -> dict[str, dict]:
    """One rule as each route that serves rules answers for it."""
    listed = [row for row in workspace.client.get("/api/rules").json() if row["id"] == rule_id]
    assert len(listed) == 1, listed
    in_detail = [row for row in _detail(workspace, skill_id)["rules"] if row["id"] == rule_id]
    assert len(in_detail) == 1, in_detail
    patched = workspace.client.patch(f"/api/rules/{rule_id}", headers=ORIGIN, json={"action": "restore"})
    assert patched.status_code == 200, patched.text
    return {"list": listed[0], "skill": in_detail[0], "patch": patched.json()}


def test_a_rule_that_was_never_split_serves_both_fields_as_null(workspace) -> None:
    _seed_skill(workspace, "expenses")
    rule = _seed_rule(workspace, "receipts", "expenses")

    for route, served in _as_served(workspace, "expenses", rule.id).items():
        assert "split_from" in served and "split_at" in served, route
        assert (served["split_from"], served["split_at"]) == (None, None), route


def test_a_split_copy_serves_the_rule_it_came_from_and_when(workspace) -> None:
    _seed_skill(workspace, "expenses")
    copy = _seed_rule(workspace, "receipts-split", "expenses")
    _mark_split(workspace.services.store, copy.id, "receipts", SPLIT_AT)

    for route, served in _as_served(workspace, "expenses", copy.id).items():
        assert served["split_from"] == "receipts", route
        assert datetime.fromisoformat(served["split_at"]) == SPLIT_AT, route
