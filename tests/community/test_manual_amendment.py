"""A correction and its rule/prose edits commit together, with portable output."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from oms.domain.models import Edge
from oms.domain.types import EdgeType
from tests.community.test_todo_critic import (
    workspace, _import, _document, _part, _detail, _save, _seed_skill,
    INTRO, RULE, NEW_INTRO, NEW_RULE, ORIGIN,
)
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


def contribute(workspace, transaction_id="amend-correction"):
    response = workspace.client.post("/api/ingest", headers=ORIGIN, json={
        "transaction_id": transaction_id, "correction": "Keep the approval record with each expense claim.",
    })
    assert response.status_code == 202, response.text
    return transaction_id


def decision(skill_id, document, edits, affected=None):
    return {"action": "amend", "body": "", "skill_ids": [skill_id], "amendment": {
        "skill_id": skill_id, "revision": document["revision"],
        "parts": [{"anchor": _part(document, old)["anchor"], "text": new, "expected_text": old} for old, new in edits],
        "affected_skill_ids": affected or [skill_id],
    }}


def apply(workspace, body, transaction_id="amend-correction"):
    return workspace.client.post(f"/api/review/{transaction_id}/decision", headers=ORIGIN, json=body)


@pytest.mark.parametrize("edits", [[(RULE, NEW_RULE)], [(INTRO, NEW_INTRO)], [(RULE, NEW_RULE), (INTRO, NEW_INTRO)]])
def test_amendment_updates_guidance_lineage_history_and_publication_exactly_once(workspace, edits):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    original_rules = {rule["id"] for rule in _detail(workspace, skill_id)["rules"]}
    contribute(workspace)
    body = decision(skill_id, document, edits)
    first = apply(workspace, body)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "applied"
    versions = workspace.client.get(f"/api/skills/{skill_id}/versions").json()
    assert "amend-correction" in versions[0]["detail"]
    assert workspace.client.get("/api/review").json() == []
    detail = _detail(workspace, skill_id)
    assert {rule["id"] for rule in detail["rules"]} == original_rules
    for old, new in edits:
        assert new in detail["body"] and old not in detail["body"]
        part = _part(_document(workspace, skill_id), new)
        neighbours = workspace.services.store.graph_neighbours(part["source_id"], "acme")
        assert {"source": part["source_id"], "target": "amend-correction", "type": "DERIVED_FROM"} in neighbours["edges"]
    assert apply(workspace, body).json() == first.json()
    assert workspace.client.get(f"/api/skills/{skill_id}/versions").json() == versions
    preview = workspace.client.get("/api/publish/preview", params={"skill_id": skill_id})
    assert preview.status_code == 200, preview.text
    for _, new in edits:
        assert new in preview.text
    original_version = versions[-1]["id"]
    compared = workspace.client.get(f"/api/skills/{skill_id}/versions/{original_version}/compare").json()
    assert any(row["restorable"] and row["state"] != "same" for row in compared["rows"])


@pytest.mark.parametrize("blank", ["", " \n"])
def test_clearing_a_rule_removes_it_with_evidence_and_restorable_history(workspace, blank):
    skill_id = _import(workspace)
    before = _document(workspace, skill_id)
    rule_id = _part(before, RULE)["source_id"]
    versions = workspace.client.get(f"/api/skills/{skill_id}/versions").json()
    contribute(workspace)
    body = decision(skill_id, before, [(RULE, blank), (INTRO, NEW_INTRO)])
    response = apply(workspace, body)
    assert response.status_code == 200, response.text
    after = _document(workspace, skill_id)
    assert all(part.get("source_id") != rule_id for part in after["parts"])
    rule = workspace.services.store.get_rule(rule_id)
    assert rule.status.value == "retired" and rule.body == RULE
    assert "amend-correction" in [txn.id for txn in workspace.services.store.lineage(rule_id)]
    assert NEW_INTRO in _detail(workspace, skill_id)["body"]
    assert RULE not in workspace.client.get("/api/publish/preview", params={"skill_id": skill_id}).text
    assert workspace.client.get("/api/review").json() == []
    assert apply(workspace, body).json() == response.json()
    assert _document(workspace, skill_id) == after
    history = workspace.client.get(f"/api/skills/{skill_id}/versions").json()
    assert len([version for version in history if "amend-correction" in (version["detail"] or "")]) == 1
    version_id = versions[0]["id"]
    comparison = workspace.client.get(f"/api/skills/{skill_id}/versions/{version_id}/compare").json()
    removed, = [row for row in comparison["rows"] if row["rule_id"] == rule_id]
    assert removed["state"] == "only_old" and removed["restorable"]
    restored = workspace.client.post(f"/api/skills/{skill_id}/versions/{version_id}/restore", headers=ORIGIN,
        json={"revision": comparison["revision"], "anchors": [removed["anchor"]]})
    assert restored.status_code == 200, restored.text
    assert RULE in _detail(workspace, skill_id)["body"]
    assert NEW_INTRO in _detail(workspace, skill_id)["body"]


def test_removing_a_shared_rule_requires_the_complete_reviewed_impact(workspace):
    skill_id = _import(workspace)
    _seed_skill(workspace, "another-skill")
    rule_id = _part(_document(workspace, skill_id), RULE)["source_id"]
    workspace.services.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule_id, to_id="another-skill"))
    document = _document(workspace, skill_id)
    contribute(workspace)
    body = decision(skill_id, document, [(RULE, "")])
    assert apply(workspace, body).status_code == 409
    assert workspace.services.store.get_rule(rule_id).status.value == "active"
    body["amendment"]["affected_skill_ids"].append("another-skill")
    assert apply(workspace, body).status_code == 200
    for affected in (skill_id, "another-skill"):
        assert RULE not in _detail(workspace, affected)["body"]
        assert "amend-correction" in workspace.services.store.latest_skill_version("acme", affected).detail


def test_an_invalid_empty_passage_cannot_partially_remove_a_rule(workspace):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    response = apply(workspace, decision(skill_id, document, [(RULE, ""), (INTRO, "")]))
    assert response.status_code == 422 and "must not be empty" in response.text
    assert _document(workspace, skill_id) == document
    assert len(workspace.client.get("/api/review").json()) == 1


def test_shared_rule_requires_exact_reviewed_impact_and_captures_both_skills(workspace):
    skill_id = _import(workspace)
    _seed_skill(workspace, "another-skill")
    rule_id = _part(_document(workspace, skill_id), RULE)["source_id"]
    workspace.services.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule_id, to_id="another-skill"))
    document = _document(workspace, skill_id)
    assert {row["id"] for row in _part(document, RULE)["affected_skills"]} == {skill_id, "another-skill"}
    contribute(workspace)
    body = decision(skill_id, document, [(RULE, NEW_RULE)])
    response = apply(workspace, body)
    assert response.status_code == 409, response.text
    assert workspace.services.store.get_rule(rule_id).body == RULE
    body["amendment"]["affected_skill_ids"].append("another-skill")
    response = apply(workspace, body)
    assert response.status_code == 200, response.text
    for affected in (skill_id, "another-skill"):
        assert NEW_RULE in _detail(workspace, affected)["body"]
        assert "amend-correction" in workspace.services.store.latest_skill_version("acme", affected).detail


@pytest.mark.parametrize("edits", [[(INTRO, NEW_INTRO)], [(RULE, "")]])
def test_stale_revision_refuses_entire_edit_and_keeps_correction_pending(workspace, edits):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    assert _save(workspace, skill_id, document, [(_part(document, RULE)["anchor"], "Retain receipts for auditing.")]).status_code == 200
    response = apply(workspace, decision(skill_id, document, edits))
    assert response.status_code == 409, response.text
    assert INTRO in _detail(workspace, skill_id)["body"]
    assert len(workspace.client.get("/api/review").json()) == 1


@pytest.mark.parametrize("bad_part", [
    {"anchor": "title", "text": "A new title"},
    {"anchor": "rule:missing", "text": "A new instruction"},
    {"anchor": "references", "text": "A new reference"},
])
def test_invalid_part_does_not_apply_any_of_the_other_edits(workspace, bad_part):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    body = decision(skill_id, document, [(INTRO, NEW_INTRO)])
    body["amendment"]["parts"].append({**bad_part, "expected_text": "Old text"})
    response = apply(workspace, body)
    assert response.status_code == 422, response.text
    assert _document(workspace, skill_id)["revision"] == document["revision"]
    assert len(workspace.client.get("/api/review").json()) == 1


@pytest.mark.parametrize("rule_text", [NEW_RULE, ""])
def test_history_failure_rolls_back_rule_prose_lineage_and_resolution(workspace, monkeypatch, rule_text):
    from oms.skills.history import SkillHistory
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    original = SkillHistory.capture_required
    def fail_after_edit(self, *args, **kwargs):
        if "manual amend" in (kwargs.get("detail") or ""):
            raise RuntimeError("history unavailable")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SkillHistory, "capture_required", fail_after_edit)
    with pytest.raises(RuntimeError, match="history unavailable"):
        apply(workspace, decision(skill_id, document, [(RULE, rule_text), (INTRO, NEW_INTRO)]))
    assert _document(workspace, skill_id)["revision"] == document["revision"]
    assert not workspace.services.queue.get("manual-amend-correction").resolved
    assert workspace.services.store.get_transaction("amend-correction").workflow_state == "awaiting_manual_review"


def test_two_corrections_against_same_revision_cannot_overwrite_each_other(workspace):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace, "first")
    contribute(workspace, "second")
    body = decision(skill_id, document, [(RULE, NEW_RULE)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda txn: apply(workspace, body, txn), ["first", "second"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(workspace.client.get("/api/review").json()) == 1


def test_amendment_cannot_bypass_safety_hold_or_edited_wording_screen(workspace):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    body = decision(skill_id, document, [(RULE, "Contact alice@example.com for receipts.")])
    assert apply(workspace, body).status_code == 422
    txn = workspace.services.store.get_transaction("amend-correction")
    txn.held_reason = "requires safety review"
    txn.workflow_state = "held_safety"
    workspace.services.store.upsert_transaction(txn)
    response = apply(workspace, decision(skill_id, document, [(RULE, NEW_RULE)]))
    assert response.status_code == 422 and "safety hold" in response.text
    assert _document(workspace, skill_id)["revision"] == document["revision"]


@pytest.mark.parametrize("position", ["start", "end", "after"])
def test_new_rule_is_placed_in_selected_section_with_lineage_and_retry_safety(workspace, position):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    section, = document["rule_sections"]
    contribute(workspace)
    placement = {"skill_id": skill_id, "revision": document["revision"], "section_id": section["id"], "position": position}
    if position == "after":
        placement["after_rule_id"] = section["rules"][0]["id"]
    body = {"action": "create", "body": NEW_RULE, "skill_ids": [skill_id], "placements": [placement]}
    response = apply(workspace, body)
    assert response.status_code == 200, response.text
    result = _document(workspace, skill_id)
    ordered = [rule["body"] for rule in result["rule_sections"][0]["rules"]]
    assert ordered == ([NEW_RULE, RULE] if position == "start" else [RULE, NEW_RULE])
    published = _detail(workspace, skill_id)["body"]
    assert "## Additional rules" not in published
    assert "amend-correction" in [txn.id for txn in workspace.services.store.lineage(response.json()["rule_id"])]
    assert apply(workspace, body).json() == response.json()
    assert _document(workspace, skill_id) == result


@pytest.mark.parametrize("change", ["stale", "foreign-section", "foreign-rule", "duplicate", "wrong-action"])
def test_invalid_placement_cannot_create_a_rule_or_resolve_the_correction(workspace, change):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    section, = document["rule_sections"]
    contribute(workspace)
    placement = {"skill_id": skill_id, "revision": document["revision"], "section_id": section["id"], "position": "end"}
    body = {"action": "create", "body": NEW_RULE, "skill_ids": [skill_id], "placements": [placement]}
    if change == "stale":
        placement["revision"] = "old-revision"
    elif change == "foreign-section":
        placement["section_id"] = "someone-elses-section"
    elif change == "foreign-rule":
        placement.update(position="after", after_rule_id="someone-elses-rule")
    elif change == "duplicate":
        body["placements"].append(placement)
    else:
        body["action"] = "reject"
    response = apply(workspace, body)
    assert response.status_code in (409, 422), response.text
    assert workspace.services.store.get_rule("rule-amend-correction") is None
    assert _document(workspace, skill_id)["revision"] == document["revision"]
    assert len(workspace.client.get("/api/review").json()) == 1


def test_reference_only_rule_amendment_checks_wording_when_document_revision_is_unchanged(workspace):
    skill_id = _import(workspace)
    rule_id = _part(_document(workspace, skill_id), RULE)["source_id"]
    rule = workspace.services.store.get_rule(rule_id)
    rule.reference_only = True
    workspace.services.store.upsert_rule(rule)
    document = _document(workspace, skill_id)
    assert _part(document, RULE)["kind"] == "overflow-rule"
    contribute(workspace, "reference-first")
    contribute(workspace, "reference-stale")
    body = decision(skill_id, document, [(RULE, NEW_RULE)])
    assert apply(workspace, body, "reference-first").status_code == 200
    assert _document(workspace, skill_id)["revision"] == document["revision"]
    response = apply(workspace, body, "reference-stale")
    assert response.status_code == 409 and "guidance changed" in response.text
    assert len(workspace.client.get("/api/review").json()) == 1


@pytest.mark.parametrize("ambiguous", [{"rule_id": "existing"}, {"confirm_reinforcement": True}, {"expected_rule_body": RULE}])
def test_creation_cannot_silently_ignore_a_selected_existing_rule(workspace, ambiguous):
    skill_id = _import(workspace)
    document = _document(workspace, skill_id)
    contribute(workspace)
    response = apply(workspace, {"action": "create", "body": NEW_RULE, "skill_ids": [skill_id], **ambiguous})
    assert response.status_code == 422, response.text
    assert workspace.services.store.get_rule("rule-amend-correction") is None
    assert _document(workspace, skill_id)["revision"] == document["revision"]
    assert len(workspace.client.get("/api/review").json()) == 1
