"""Matching proposes choices; only a checked human decision mutates a rule."""
import pytest

from oms.community.matching import LocalMatcher
from oms.community.workflow import Decision, DecisionConflict, ManualDecisionError
from oms.domain.models import Rule, Skill
from oms.domain.types import Plane, RuleStatus
from oms.ingestion.schema import ExecutionContext
from tests.community.test_critic_workflow import world, critic_neo4j_driver  # noqa: F401 — shared memory/isolated-Neo4j contract


def skill(identifier, name, description=""):
    return Skill(id=identifier, name=name, description=description, domain="testing", tenant_id="acme")


def test_exact_hint_wins_over_content_but_unrelated_skills_are_not_suggested():
    matcher = LocalMatcher([
        skill("expenses", "Expense review", "Check receipts and approval for expense claims"),
        skill("tests", "Testing", "Unit tests for public functions"),
        skill("writing", "Copywriting", "Write advertisements"),
    ], [])
    suggestions = matcher.skills_for("Check receipts and approval for expense claims", "TESTING")
    assert [row["id"] for row in suggestions] == ["tests", "expenses"]
    assert suggestions[0]["reason"] == "Matches the supplied skill hint."
    assert "expense" in suggestions[1]["reason"]


@pytest.mark.parametrize("hint", [None, "unknown-area", "Expnse reveiw"])
def test_missing_unknown_and_misspelled_hints_still_find_relevant_skills(hint):
    matcher = LocalMatcher([
        skill("expenses", "Expense review", "Keep receipts and approval for expense claims"),
        skill("writing", "Copywriting", "Create advertisements"),
    ], [])
    suggestions = matcher.skills_for("Check the receipt before an expense claim", hint)
    assert [row["id"] for row in suggestions] == ["expenses"]
    assert suggestions[0]["reason"]


def test_short_list_is_deterministic_and_does_not_require_a_hint_or_network():
    skills = [skill(f"skill-{i}", f"Review {i}", "Validate receipts for expense claims") for i in range(12)]
    first = LocalMatcher(skills, []).skills_for("Validate receipt before expense claim", None)
    assert len(first) == 5
    assert first == LocalMatcher(list(reversed(skills)), []).skills_for("Validate receipt before expense claim", None)
    assert LocalMatcher(skills, []).skills_for("Publish a container image", None) == []


def test_rule_suggestions_keep_exact_punctuation_and_unicode_distinctions():
    matcher = LocalMatcher([], [
        Rule(id="cpp", body="Use C++", tenant_id="acme"),
        Rule(id="unicode", body="Café receipts", tenant_id="acme"),
        Rule(id="retired", body="Café receipts", tenant_id="acme", status=RuleStatus.RETIRED),
        Rule(id="control", body="Café receipts", tenant_id="acme", plane=Plane.CONTROL),
    ])
    exact, matches = matcher.rules_for(" CAFE\u0301   RECEIPTS ")
    assert [match["id"] for match in exact] == ["unicode"]
    assert [match["id"] for match in matches] == ["unicode"]
    assert matches[0]["exact"] is True
    exact, _ = matcher.rules_for("Use C")
    assert exact == []


def test_negation_is_a_review_warning_not_evidence_of_equivalence():
    matcher = LocalMatcher([], [Rule(id="existing", body="Always publish private credentials", tenant_id="acme")])
    exact, candidates = matcher.rules_for("Never publish private credentials")
    assert exact == []
    assert candidates[0]["id"] == "existing" and not candidates[0]["exact"]
    assert "Negation differs" in candidates[0]["reason"]


def test_inbox_suggests_without_applying_or_exposing_other_workspace(world):
    world.skill()
    world.skill("foreign", tenant="other")
    world.store.upsert_rule(Rule(id="existing", body="Keep original receipts for every expense claim", tenant_id="acme"))
    world.store.upsert_rule(Rule(id="foreign-rule", body="Keep receipts for expense claims.", tenant_id="other"))
    world.ingest("suggested")
    row, = world.service.inbox("acme")
    assert row["skill_suggestions"][0]["id"] == "expenses"
    assert [candidate["id"] for candidate in row["similar_matches"]] == ["existing"]
    assert row["exact_matches"] == []
    assert "foreign" not in str(row)
    assert world.store.get_rule("existing").corroboration_count == 1
    assert world.store.lineage("existing") == []
    assert world.store.get_rule("rule-suggested") is None
    assert not world.queue.get("manual-suggested").resolved


def test_fuzzy_skill_suggestions_are_not_preselected(world):
    world.skill()
    world.ingest("typo")
    txn = world.store.get_transaction("typo")
    txn.skill_hint = "Expnses"
    world.store.upsert_transaction(txn)
    row, = world.service.inbox("acme")
    assert row["candidate_skill_ids"] == []
    assert row["skill_suggestions"][0]["id"] == "expenses"


def test_inbox_includes_sanitised_context_and_provenance_even_with_a_queued_body(world):
    world.skill()
    payload, principal = world.payload("context-review")
    payload.repo = "github.com/acme/expenses"
    payload.source_agent_id = "expense-agent"
    payload.execution_context = ExecutionContext(
        user_input="Review the expense from alice@example.com",
        agent_raw_output="The receipt is missing.",
        user_correction="Keep receipts for expense claims.",
    )
    world.coordinator.ingest(payload, principal)
    assert world.queue.get("manual-context-review").proposed_body
    row, = world.service.inbox("acme")
    assert row["text"] == payload.execution_context.user_correction
    assert row["skill_hint"] == "Expenses"
    assert row["repo"] == "github.com/acme/expenses"
    assert row["posted_at"] == payload.timestamp.isoformat()
    assert row["source_agent_id"] == "expense-agent"
    assert row["source_runtime"] == "mcp"
    assert row["source_ref"] == "session:critic"
    assert "alice@example.com" not in row["context"]["user_input"]
    assert "Review the expense" in row["context"]["user_input"]
    assert row["context"]["agent_output"] == "The receipt is missing."
    assert row["context_truncated"] is False


def test_inbox_bounds_context_and_still_reviews_items_after_payload_expiry(world):
    world.ingest("context-expiry")
    world.payloads.put("context-expiry", ExecutionContext(
        user_input="x" * 9000, agent_raw_output="y" * 9000,
        user_correction="Keep receipts for expense claims.",
    ))
    row, = world.service.inbox("acme")
    assert row["context_truncated"] is True
    assert len(row["context"]["user_input"]) == len(row["context"]["agent_output"]) == 8000
    world.payloads.delete("context-expiry")
    row, = world.service.inbox("acme")
    assert row["text"] == "Keep receipts for expense claims."
    assert row["context"] is None
    assert row["context_truncated"] is False


def reinforce(world, transaction_id, existing_body, **overrides):
    fields = {"action": "reinforce", "body": "Keep receipts for expense claims.",
              "skill_ids": ["expenses"], "rule_id": "existing",
              "confirm_reinforcement": True, "expected_rule_body": existing_body}
    fields.update(overrides)
    return world.service.decide(transaction_id, "acme", "local-reviewer", Decision(**fields))


@pytest.mark.parametrize("overrides", [
    {"confirm_reinforcement": False}, {"expected_rule_body": None},
    {"confirm_reinforcement": False, "body": "Keep original receipts for every expense claim"},
])
def test_different_wording_cannot_reinforce_without_confirmation_and_reviewed_snapshot(world, overrides):
    world.skill()
    original = "Keep original receipts for every expense claim"
    world.store.upsert_rule(Rule(id="existing", body=original, tenant_id="acme"))
    world.ingest("unconfirmed")
    with pytest.raises(ManualDecisionError, match="explicit confirmation"):
        reinforce(world, "unconfirmed", original, **overrides)
    assert world.store.get_rule("existing").corroboration_count == 1
    assert not world.queue.get("manual-unconfirmed").resolved


def test_confirmed_different_wording_preserves_rule_and_records_evidence_exactly_once(world):
    world.skill()
    original = "Keep original receipts for every expense claim"
    world.store.upsert_rule(Rule(id="existing", body=original, tenant_id="acme", corroboration_count=3))
    world.ingest("confirmed")
    first = reinforce(world, "confirmed", original)
    assert reinforce(world, "confirmed", original) == first
    assert first.state == "applied" and first.rule_id == "existing"
    rule = world.store.get_rule("existing")
    assert rule.body == original and rule.corroboration_count == 4
    assert [txn.id for txn in world.store.lineage("existing")] == ["confirmed"]
    assert world.payloads.get("confirmed").user_correction == "Keep receipts for expense claims."
    assert world.queue.get("manual-confirmed").decided_by == "local-reviewer"
    assert world.store.get_rule("rule-confirmed") is None
    assert world.store.latest_skill_version("acme", "expenses") is not None


def test_stale_candidate_requires_a_fresh_comparison_without_partial_writes(world):
    world.skill()
    world.store.upsert_rule(Rule(id="existing", body="Discard all original receipts", tenant_id="acme"))
    world.ingest("stale")
    with pytest.raises(DecisionConflict, match="changed"):
        reinforce(world, "stale", "Keep original receipts for every expense claim")
    assert world.store.get_rule("existing").corroboration_count == 1
    assert world.store.lineage("existing") == []
    assert not world.queue.get("manual-stale").resolved


@pytest.mark.parametrize("fields", [
    {"tenant_id": "other"}, {"status": RuleStatus.RETIRED}, {"plane": Plane.CONTROL},
])
def test_confirmation_does_not_override_workspace_or_active_data_rule_boundaries(world, fields):
    world.skill()
    original = "Keep original receipts for every expense claim"
    rule_fields = {"id": "existing", "body": original, "tenant_id": "acme", **fields}
    world.store.upsert_rule(Rule(**rule_fields))
    world.ingest("not-eligible")
    with pytest.raises(ManualDecisionError, match="active data rule"):
        reinforce(world, "not-eligible", original)
    assert world.store.get_rule("existing").corroboration_count == 1


def test_confirmation_cannot_bypass_a_safety_hold(world):
    world.skill()
    original = "Keep original receipts for every expense claim"
    world.store.upsert_rule(Rule(id="existing", body=original, tenant_id="acme"))
    world.ingest("held-matching", "Keep alice@example.com in expense receipts")
    with pytest.raises(ManualDecisionError, match="safety hold"):
        reinforce(world, "held-matching", original)
    assert world.store.get_rule("existing").corroboration_count == 1
