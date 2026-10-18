"""The routes answer a refused second skill with a message, never a server error.

One rule, one skill: a write that would put a rule in a second skill is
refused by the store and the whole request writes nothing. The person who
asked for it gets the refusal, which names the skill the rule is in and the
one it cannot also join."""
from io import BytesIO
from zipfile import ZipFile

from oms.domain.models import Rule, Section
from oms.domain.types import Mutability, SectionKind
from oms.ports.graph_store import RuleOwnershipError
from oms.sources.service import SourceService
from tests.community.test_github_sources import source_application  # noqa: F401
from tests.community.test_todo_critic import (
    ORIGIN, RULE, _files, _import, _seed_skill,
    critic_neo4j_driver, workspace,  # noqa: F401: shared parametrised fixtures
)


def _skills_of(workspace, rule_id: str) -> list[str]:
    return [skill.id for skill in workspace.services.store.skills_for_rule(rule_id, tenant_id="acme")]


def _reinforce(workspace, transaction_id: str, rule_id: str, skill_ids: list[str]):
    """Send a correction worded as the rule, then reinforce the rule with it."""
    accepted = workspace.client.post("/api/ingest", headers=ORIGIN,
                                     json={"transaction_id": transaction_id, "correction": RULE})
    assert accepted.status_code == 202, accepted.text
    return workspace.client.post(f"/api/review/{transaction_id}/decision", headers=ORIGIN, json={
        "action": "reinforce", "body": RULE, "skill_ids": skill_ids, "rule_id": rule_id})


def _post_archive(workspace):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for path, body in _files().items():
            archive.writestr(path, body)
    return workspace.client.post("/api/import", headers=ORIGIN,
                                 files={"file": ("skills.zip", buffer.getvalue(), "application/zip")})


def test_an_import_that_would_give_a_rule_a_second_skill_is_refused_with_its_reason(workspace) -> None:
    # Deleting a skill leaves its rules in no skill; a reinforcement puts one
    # in travel; importing the deleted skill's file again would give the same
    # rule id back to the old skill.
    skill_id = _import(workspace)
    rule = workspace.services.store.rules_for_skill(skill_id, tenant_id="acme")[0]
    assert workspace.client.delete(f"/api/skills/{skill_id}", headers=ORIGIN).status_code == 200
    _seed_skill(workspace, "travel")
    moved = _reinforce(workspace, "moved-rule", rule.id, ["travel"])
    assert moved.status_code == 200, moved.text
    count = workspace.services.store.get_rule(rule.id).corroboration_count

    response = _post_archive(workspace)

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": f"rule {rule.id} is in travel; one rule belongs to one skill, "
                                         f"so it cannot also be in {skill_id}"}
    assert [row["id"] for row in workspace.client.get("/api/skills").json()] == ["travel"]
    assert _skills_of(workspace, rule.id) == ["travel"]
    assert workspace.services.store.get_rule(rule.id).corroboration_count == count


def test_a_reinforcement_that_would_give_a_placed_rule_a_second_skill_is_a_conflict(workspace) -> None:
    # The schema 3 split leaves a copy placed in its skill's section and in
    # no skill; reinforcing it into another skill would give it two skills.
    store = workspace.services.store
    _seed_skill(workspace, "expenses")
    _seed_skill(workspace, "audit")
    store.upsert_section(Section(id="expenses-rules", skill_id="expenses", kind=SectionKind.RULES,
                                 heading="Rules", order=0, mutability=Mutability.SYSTEM_AGGREGATED,
                                 tenant_id="acme"))
    store.upsert_rule(Rule(id="placed-only", body=RULE, tenant_id="acme"))
    store.attach_rule(store.get_rule("placed-only"), "expenses-rules", order=0, tenant_id="acme")

    response = _reinforce(workspace, "placed-correction", "placed-only", ["audit"])

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "rule placed-only is in expenses; one rule belongs to one skill, "
                                         "so it cannot also be in audit"}
    assert _skills_of(workspace, "placed-only") == []
    assert store.get_rule("placed-only").corroboration_count == 1
    assert "placed-correction" in {row["txn_id"] for row in workspace.client.get("/api/review").json()}


def test_a_source_request_that_meets_the_refusal_answers_a_conflict_with_its_reason(
        source_application, monkeypatch) -> None:
    # The refusal comes from the service here; the route's answer is under test.
    _, client = source_application
    reason = "rule rule-1 is in travel; one rule belongs to one skill, so it cannot also be in expenses"

    def refused(*args, **kwargs):
        raise RuleOwnershipError(reason)
    monkeypatch.setattr(SourceService, "submit_local_batch", refused)
    workspace_id = client.get("/api/skill-source-workspace").json()["workspace_id"]
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: Expenses\ndescription: Record expenses\n---\n\n"
                                     "## Rules\n\n- Keep original receipts.\n")
    staged = client.post("/api/import", files={"file": ("expenses.zip", content.getvalue(), "application/zip")})
    assert staged.status_code == 200, staged.text

    response = client.post("/api/skill-local-imports", headers={"Idempotency-Key": "refused-import",
                                                                 "X-Source-Workspace": workspace_id},
                           json={"upload_id": staged.json()["upload_id"], "selections": [
                               {"package_path": "", "local_name": "Expenses", "domain": "finance"}]})

    assert response.status_code == 409, response.text
    assert (response.json()["code"], response.json()["message"]) == ("rule_in_another_skill", reason)
