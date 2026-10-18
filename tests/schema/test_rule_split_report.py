"""The split plan and the report-only command (spec 8.6): read-only, per tenant."""
import pytest

from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
from oms.domain.copies import split_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import Edge, ReviewItem, Rule
from oms.domain.types import EdgeType, Verdict
from oms.schema.migrations.rule_split import format_report, plan_split
from tests.legacy_rules import place_rule
from tests.schema import split_world
from tests.schema.test_critic_schema import _snapshot, schema_driver  # noqa: F401

pytestmark = pytest.mark.integration


def _by_rule(plans):
    return {rule.rule_id: rule for plan in plans for rule in plan.rules}


def test_the_plan_names_every_shared_rule_its_keeper_and_its_copies(schema_driver):
    split_world.build(schema_driver)
    plans = plan_split(schema_driver)
    assert [plan.tenant_id for plan in plans] == ["acme", "beta"]
    rules = _by_rule(plans)
    assert sorted(rules) == sorted([split_world.IMPORTED, "rule-t-hint", "rule-t-plain", "rule-t-placed",
                                    "rule-sup", "rule-flagged", "rule-beta-shared"])
    expected = {split_world.IMPORTED: ("email", "import", ["blog"]),
                "rule-t-hint": ("press", "skill_hint", ["blog", "email"]),
                "rule-t-plain": ("blog", "lowest_key", ["press"]),
                "rule-t-placed": ("email", "lowest_key", ["press"]),
                "rule-sup": ("blog", "lowest_key", ["email"]),
                "rule-flagged": ("aardvark", "lowest_key", ["blog"]),
                "rule-beta-shared": ("blog", "lowest_key", ["email"])}
    for rule_id, (keeper, reason, copies) in expected.items():
        plan = rules[rule_id]
        assert (plan.keeper, plan.keeper_reason, [c.skill_id for c in plan.copies]) == (keeper, reason, copies)
        tenant = "beta" if rule_id == "rule-beta-shared" else "acme"
        assert [c.copy_id for c in plan.copies] == [split_rule_id(rule_id, SkillRef(tenant, s)) for s in copies]


def test_each_copy_plan_carries_the_originals_membership_and_places(schema_driver):
    split_world.build(schema_driver)
    rules = _by_rule(plan_split(schema_driver))
    imported = rules[split_world.IMPORTED].copies[0]
    assert (imported.belongs, imported.belongs_properties) == (True, {"repo_fallback": True})
    assert [(s.section_id, s.order, s.group) for s in imported.slots] == [("blog-rules", 2, "Voice")]
    placed = rules["rule-t-placed"].copies[0]
    assert (placed.belongs, [(s.section_id, s.order, s.group) for s in placed.slots]) == (
        False, [("press-rules", 7, "Orders")])


def test_the_plan_counts_held_items_and_divergence_per_tenant(schema_driver):
    split_world.build(schema_driver)
    acme, beta = plan_split(schema_driver)
    assert acme.counts() == {"shared_rules": 6, "skills_affected": 4, "copies": 7, "held_items": 4,
                             "divergence_items": 1}
    assert beta.counts() == {"shared_rules": 1, "skills_affected": 2, "copies": 1, "held_items": 0,
                             "divergence_items": 0}
    rules = _by_rule((acme,))
    assert rules["rule-t-plain"].items_copied == ("review-pending-rule-t-plain",)
    assert set(rules["rule-t-hint"].items_moved) == {"review-rule-t-hint-rule-blog-a", "proposed-edit-000000000001"}
    assert rules["rule-flagged"].items_copied == ("review-rule-flagged-constraint-acme-plain",)


def test_the_report_writes_nothing(schema_driver):
    split_world.build(schema_driver)
    before = _snapshot(schema_driver)
    report = format_report(plan_split(schema_driver))
    assert _snapshot(schema_driver) == before
    assert report.splitlines()[0] == "Rule split report: nothing has been changed."
    assert ("Tenant acme: 6 shared rules, 4 skills affected, 7 copies to make, 4 held items affected, "
            "1 divergence item to close") in report
    assert "  rule-t-hint: kept by press (named by the first contribution); copies for blog, email" in report


def test_a_graph_with_nothing_shared_reports_nothing_to_do(schema_driver):
    split_world.reset(schema_driver)
    assert plan_split(schema_driver) == ()
    assert "No rule is shared between skills, and no divergence item is open." in format_report(())


class _Unclosed:
    """The fixture driver without `close`, so the command cannot close it."""
    def __init__(self, driver):
        self._driver = driver

    def session(self, **kwargs):
        return self._driver.session(**kwargs)


def test_the_command_prints_the_report(schema_driver, monkeypatch, capsys):
    from oms.schema import split_rules
    split_world.build(schema_driver)
    monkeypatch.setattr(split_rules, "_driver", lambda: _Unclosed(schema_driver))
    assert split_rules.main(["--report"]) == 0
    assert "Tenant beta: 1 shared rule" in capsys.readouterr().out


def test_a_rule_placed_in_two_skills_it_does_not_belong_to_is_shared_and_a_rule_at_home_is_not(schema_driver):
    store = split_world.build(schema_driver)
    for rule_id in ("rule-unowned", "rule-placed-once", "rule-at-home"):
        store.upsert_rule(Rule(id=rule_id, body=f"The {rule_id} rule.", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-at-home", "email"), tenant_id="acme")
    store.attach_rule(store.get_rule("rule-at-home"), "email-rules", order=4, tenant_id="acme")
    store.attach_rule(store.get_rule("rule-placed-once"), "blog-rules", order=5, tenant_id="acme")
    store.attach_rule(store.get_rule("rule-unowned"), "blog-rules", order=1, tenant_id="acme")
    place_rule(store, store.get_rule("rule-unowned"), "press-rules", tenant_id="acme", order=3, group="Late")
    rules = _by_rule(plan_split(schema_driver))
    assert "rule-placed-once" not in rules and "rule-at-home" not in rules
    unowned = rules["rule-unowned"]
    assert (unowned.keeper, unowned.keeper_reason) == ("blog", "lowest_key")
    assert [(c.skill_id, c.belongs, [(s.section_id, s.order, s.group) for s in c.slots])
            for c in unowned.copies] == [("press", False, [("press-rules", 3, "Late")])]


def test_an_open_item_no_treatment_covers_stays_and_the_report_lists_it(schema_driver):
    split_world.build(schema_driver)
    Neo4jReviewQueue(schema_driver).enqueue(ReviewItem(
        id="review-withdrawn-rule-blog-a", kind="withdrawn_repeat", subject_id="rule-blog-a",
        other_id="rule-t-hint", verdict=Verdict.DUPLICATE, reason="a repeat", tenant_id="acme"))
    plans = plan_split(schema_driver)
    hint = _by_rule(plans)["rule-t-hint"]
    assert "review-withdrawn-rule-blog-a" not in (*hint.items_copied, *hint.items_moved)
    assert hint.warnings == ("open item review-withdrawn-rule-blog-a (withdrawn_repeat) stays with the original in press",)
    assert ("    warning: open item review-withdrawn-rule-blog-a (withdrawn_repeat) stays with the original in press"
            in format_report(plans).splitlines())
    assert plans[0].counts()["held_items"] == 4


def test_a_source_claim_keeps_the_rule_and_a_claim_that_loses_is_a_warning(schema_driver):
    from oms.adapters.neo4j.source_store import Neo4jSourceRepository
    from oms.sources.models import PartOwnership
    split_world.build(schema_driver)
    sources = Neo4jSourceRepository(schema_driver)
    for skill_id, rule_id in (("press", "rule-t-plain"), ("blog", "rule-t-hint")):
        sources.put_ownership(PartOwnership(skill=SkillRef("acme", skill_id), part_id=f"rule:{rule_id}",
                                            origin_ids=(f"origin-{rule_id}",), entity_ids=(rule_id,),
                                            proof_digest=f"proof-{rule_id}"))
    plans = plan_split(schema_driver)
    rules = _by_rule(plans)
    plain = rules["rule-t-plain"]
    assert (plain.keeper, plain.keeper_reason, [c.skill_id for c in plain.copies], plain.warnings) == (
        "press", "source", ["blog"], ())
    assert rules["rule-t-hint"].warnings == ("blog tracks this rule from a source but gets a copy",)
    assert "    warning: blog tracks this rule from a source but gets a copy" in format_report(plans).splitlines()


def test_another_tenants_item_naming_the_rule_is_not_counted(schema_driver):
    split_world.build(schema_driver)
    Neo4jReviewQueue(schema_driver).enqueue(ReviewItem(
        id="review-pending-elsewhere", kind="pending_rule", subject_id="rule-t-plain", other_id=None,
        verdict=Verdict.AMBIGUOUS, reason="held", tenant_id="beta"))
    acme, beta = plan_split(schema_driver)
    assert _by_rule((acme,))["rule-t-plain"].items_copied == ("review-pending-rule-t-plain",)
    assert (acme.counts()["held_items"], beta.counts()["held_items"]) == (4, 0)


class _Recording:
    """The fixture driver, noting the access mode of every session it opens."""
    def __init__(self, driver):
        self._driver, self.modes = driver, []

    def session(self, **kwargs):
        self.modes.append(kwargs.get("default_access_mode"))
        return self._driver.session(**kwargs)


def test_the_plan_reads_only_through_sessions_the_database_refuses_writes_in(schema_driver):
    from neo4j.exceptions import ClientError
    split_world.build(schema_driver)
    recording = _Recording(schema_driver)
    assert plan_split(recording)
    assert recording.modes and set(recording.modes) == {"READ"}
    with pytest.raises(ClientError):
        with recording.session(default_access_mode="READ") as session:
            session.run("CREATE (:Probe {id:'written'})").consume()
    with schema_driver.session() as session:
        assert session.run("MATCH (n:Probe) RETURN count(n) AS n").single()["n"] == 0
