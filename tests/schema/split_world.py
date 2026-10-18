"""A graph shaped the way OMS 1.5 left it, with rules shared between skills.

Built through the core Neo4j store, with the shared parts written directly
(`tests.legacy_rules`), then marked schema 2. Expected outcomes, by rule:

* IMPORTED: email's sentence (its import transaction names email's file),
  reinforced into blog and placed in blog's section. Email keeps it; blog's
  copy keeps the BELONGS_TO properties and the place.
* rule-t-hint: in press, blog and email; the first contribution named
  "Press office". Press keeps it; blog and email get copies; its conflict with
  a blog-only rule moves to blog's copy, its RELATED_TO an email-only rule to
  email's, its RELATED_TO a press rule stays; a held portal edit opened on
  blog follows blog's copy.
* rule-t-plain: held, in blog and press, nothing named: blog keeps it (lowest
  key); press's copy gets its own pending item.
* rule-t-placed: in email, placed only in press's section: email keeps it;
  press's copy is placed at the same order and group and belongs nowhere.
* rule-sup: in blog and email, superseded by a blog-only rule: both members
  stay superseded (Review Focus 2).
* rule-flagged: flagged for a constraint, in aardvark and blog: aardvark keeps
  it; blog's copy is flagged, keeps the constraint edge and gets the item.
* rule-beta-shared: the other tenant's own shared rule.
* One open divergence item: closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
from oms.adapters.neo4j.store import Neo4jGraphStore
from oms.domain.models import (Constraint, ContentBlock, Edge, Example, ReviewItem, Rule, Section, Skill,
                               Transaction)
from oms.domain.types import (EdgeType, ExampleKind, Mutability, RuleStatus, SectionKind, SignalType,
                              SourceRuntime, Verdict)
from tests.legacy_rules import place_rule, share_rule

TENANT, OTHER = "acme", "beta"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
IMPORTED = "rule-import-aaaaaaaaaaaaaaaa"


def reset(driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()


def mark_schema_2(driver) -> None:
    """What a graph 1.5.x finished with: schema 2, ready."""
    with driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) SET m.core_version=2, m.schema_version=2, "
                    "m.migration_state='ready', m.last_successful_migration='core-2', "
                    "m.minimum_core_version='1.5.0'").consume()


def _skill(store, skill_id, *, tenant=TENANT, name=None, import_source_ref=None):
    store.upsert_skill(Skill(id=skill_id, name=name or skill_id.title(), description=f"{skill_id} work",
                             domain="comms", tenant_id=tenant, import_source_ref=import_source_ref))


def _section(store, skill_id, *, tenant=TENANT, kind=SectionKind.RULES, order=1) -> str:
    mutability = (Mutability.SYSTEM_AGGREGATED if kind is SectionKind.RULES
                  else Mutability.AUTHORIAL_PASSTHROUGH)
    section_id = f"{skill_id}-{kind.value}"
    store.upsert_section(Section(id=section_id, skill_id=skill_id, kind=kind, heading=kind.value.title(),
                                 order=order, mutability=mutability, tenant_id=tenant))
    return section_id


def _txn(store, txn_id, days, *, tenant=TENANT, hint=None, signal=SignalType.EXPLICIT_CORRECTION,
         source_ref=None):
    store.upsert_transaction(Transaction(
        id=txn_id, signal_type=signal, source_runtime=SourceRuntime.MCP, sanitised_payload_ref=f"{txn_id}.json",
        timestamp=T0 + timedelta(days=days), tenant_id=tenant, skill_hint=hint, source_ref=source_ref))


def _rule(store, rule_id, body, skill_id, txn_id, *, tenant=TENANT, count=1, status=RuleStatus.ACTIVE):
    store.upsert_rule(Rule(id=rule_id, body=body, tenant_id=tenant, corroboration_count=count, status=status))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, rule_id, skill_id), tenant_id=tenant)
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, rule_id, txn_id), tenant_id=tenant)


def build(driver) -> Neo4jGraphStore:
    reset(driver)
    store = Neo4jGraphStore(driver)
    store.ensure_schema()
    queue = Neo4jReviewQueue(driver)
    _skill(store, "aardvark")
    _skill(store, "blog")
    _skill(store, "email", import_source_ref="skills/email/SKILL.md")
    _skill(store, "press", name="Press office")
    _skill(store, "email", tenant=OTHER)
    _skill(store, "blog", tenant=OTHER)
    intro = _section(store, "email", kind=SectionKind.PROSE, order=0)
    block = ContentBlock(id="block-email-intro", content_ref="sha256-" + "1" * 64, kind=SectionKind.PROSE,
                         tenant_id=TENANT, source_ref="skills/email/SKILL.md#overview",
                         body="Emails speak for the company.")
    store.upsert_content_block(block)
    store.attach_block(block, intro, tenant_id=TENANT)
    sections = {skill_id: _section(store, skill_id) for skill_id in ("blog", "email", "press")}
    _txn(store, "t-import", 0, signal=SignalType.SKILL_IMPORT, source_ref="skills/email/SKILL.md")
    _txn(store, "t-hint", 1, hint="Press office")
    _txn(store, "t-plain", 2)
    _txn(store, "t-later", 3)
    _txn(store, "t-beta", 1, tenant=OTHER)
    store.upsert_tag("tone", "tone")

    _rule(store, IMPORTED, "Write in a formal register.", "email", "t-import", count=2)
    store.attach_edge(Edge(EdgeType.TAGGED_WITH, IMPORTED, "tone"), tenant_id=TENANT)
    store.attach_rule(store.get_rule(IMPORTED), sections["email"], order=0, group="Tone", tenant_id=TENANT)
    for example_id, body in (("example-aaaaaaaaaaaaaaaa", "Dear Ms Smith,"),
                             ("example-bbbbbbbbbbbbbbbb", "Yours sincerely,")):
        store.upsert_example(Example(id=example_id, body=body, kind=ExampleKind.POSITIVE, tenant_id=TENANT,
                                     parent_rule_id=IMPORTED), tenant_id=TENANT)
    share_rule(store, IMPORTED, "blog", tenant_id=TENANT, properties={"repo_fallback": True})
    place_rule(store, store.get_rule(IMPORTED), sections["blog"], tenant_id=TENANT, order=2, group="Voice")

    _rule(store, "rule-t-hint", "Sign off with the team name.", "press", "t-hint", count=2)
    share_rule(store, "rule-t-hint", "blog", tenant_id=TENANT)
    share_rule(store, "rule-t-hint", "email", tenant_id=TENANT)
    for skill_id in ("blog", "email", "press"):
        for suffix in ("a", "z"):
            # Equal counts beside the shared rules, so ties break on id.
            _rule(store, f"rule-{skill_id}-{suffix}", f"{skill_id.title()} rule {suffix}.", skill_id, "t-later", count=2)

    _rule(store, "rule-t-plain", "Name the product in the first line.", "blog", "t-plain",
          status=RuleStatus.PENDING)
    share_rule(store, "rule-t-plain", "press", tenant_id=TENANT)
    queue.enqueue(ReviewItem(id="review-pending-rule-t-plain", kind="pending_rule", subject_id="rule-t-plain",
                             other_id=None, verdict=Verdict.AMBIGUOUS, reason="held", tenant_id=TENANT,
                             group_id="session-1", transaction_id="t-plain"))

    _rule(store, "rule-t-placed", "Quote the order number.", "email", "t-later")
    place_rule(store, store.get_rule("rule-t-placed"), sections["press"], tenant_id=TENANT, order=7, group="Orders")

    _rule(store, "rule-sup", "Use the old footer.", "blog", "t-later", status=RuleStatus.SUPERSEDED)
    share_rule(store, "rule-sup", "email", tenant_id=TENANT)
    _rule(store, "rule-blog-new-footer", "Use the new footer.", "blog", "t-later")
    store.attach_edge(Edge(EdgeType.SUPERSEDES, "rule-blog-new-footer", "rule-sup"), tenant_id=TENANT)

    store.upsert_constraint(Constraint(id="constraint-acme-plain", body="Write plainly.", tenant_id=TENANT))
    _rule(store, "rule-flagged", "Use ornate language.", "aardvark", "t-later", status=RuleStatus.FLAGGED)
    share_rule(store, "rule-flagged", "blog", tenant_id=TENANT)
    store.attach_edge(Edge(EdgeType.CONFLICTS_WITH, "rule-flagged", "constraint-acme-plain"), tenant_id=TENANT)
    queue.enqueue(ReviewItem(id="review-rule-flagged-constraint-acme-plain", kind="conflict",
                             subject_id="rule-flagged", other_id="constraint-acme-plain",
                             verdict=Verdict.CONFLICTS_WITH, reason="conflicts with constraint", tenant_id=TENANT))

    store.attach_edge(Edge(EdgeType.CONFLICTS_WITH, "rule-t-hint", "rule-blog-a"), tenant_id=TENANT)
    queue.enqueue(ReviewItem(id="review-rule-t-hint-rule-blog-a", kind="conflict", subject_id="rule-t-hint",
                             other_id="rule-blog-a", verdict=Verdict.CONFLICTS_WITH,
                             reason="rule-rule conflict", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.RELATED_TO, "rule-t-hint", "rule-email-a"), tenant_id=TENANT)
    store.attach_edge(Edge(EdgeType.RELATED_TO, "rule-t-hint", "rule-press-a"), tenant_id=TENANT)
    queue.enqueue(ReviewItem(id="proposed-edit-000000000001", kind="proposed_edit", subject_id="blog",
                             other_id="rule:rule-t-hint", verdict=Verdict.AMBIGUOUS, reason="edit",
                             tenant_id=TENANT, proposed_body="Sign off with the team's name.",
                             base_revision="sha256:unchanged"))
    store.upsert_cross_skill_edge(EdgeType.CROSS_SKILL_DIVERGENCE, "rule-blog-z", "rule-email-z", 0.97)
    queue.enqueue(ReviewItem(id="review-cross-rule-blog-z-rule-email-z", kind="cross_skill_divergence",
                             subject_id="rule-blog-z", other_id="rule-email-z", verdict=Verdict.AMBIGUOUS,
                             reason="similar body, opposite polarity (sim=0.97)", tenant_id=TENANT))

    _rule(store, "rule-beta-shared", "Reply within a day.", "email", "t-beta", tenant=OTHER)
    share_rule(store, "rule-beta-shared", "blog", tenant_id=OTHER)

    mark_schema_2(driver)
    return store
