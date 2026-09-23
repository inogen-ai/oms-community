import logging
from datetime import datetime, timezone

from neo4j import Driver
from neo4j.graph import Node

from oms.adapters.neo4j import schema
from oms.adapters.neo4j.fulltext import fulltext_terms
from oms.domain.models import (
    Rule, Skill, Transaction, Edge, Learning, Constraint, Example,
    Section, ContentBlock, Artefact, Publication, PublishBlock, QuarantinedPayload,
    RulePage, RulePlacement, TenantVocabulary, UsageEvent, SkillVersion, SkillChange,
)
from oms.domain.types import (
    CompileStatus, ConstraintSource, ConstraintStatus,
    EdgeType, ExampleKind, Plane, Polarity, RuleStatus, BlockStatus,
    SignalType,
    SkillStatus, SkillOrigin, SourceRuntime, SectionKind, ArtefactKind, Mutability,
    SkillVersionCause,
)
from oms.domain.auth import Assurance
from oms.ports.graph_store import RuleContext, SectionMutabilityError

logger = logging.getLogger(__name__)

# What counts as a fault an administrator has not yet acknowledged, written
# once because listing and dismissing MUST agree: a dismiss that matched a
# wider set than the list would clear rows nobody was shown.
#
# The three clauses, in the order they exclude most:
#   * skill imports are provenance for work the importer already applied;
#   * compile_fault=false is a review rejection, an administrator's own
#     decision rather than a defect;
#   * a dismissal is an administrator saying they have dealt with it.
# coalesce defaults the flag to true, so a row written before it existed stays
# visible. An unknown is safer shown than hidden.
_LISTABLE_FAULT = (
    "t.compile_status='failed' "
    "AND coalesce(t.signal_type,'') <> 'skill_import' "
    "AND coalesce(t.compile_fault, true) = true "
    "AND t.failure_dismissed_at IS NULL"
)


class Neo4jGraphStore:
    def __init__(self, driver: Driver, embedding_dim: int = 1536) -> None:
        self._driver = driver
        self._embedding_dim = embedding_dim

    def ensure_schema(self) -> None:
        from oms.schema.metadata import SchemaManager
        SchemaManager(self._driver).initialise(edition="community")

    def upsert_rule(self, rule: Rule) -> None:
        self._write_rule(rule, create_only=False)

    def create_rule_if_absent(self, rule: Rule) -> None:
        self._write_rule(rule, create_only=True)

    def _write_rule(self, rule: Rule, *, create_only: bool) -> None:
        assignment = "ON CREATE SET" if create_only else "SET"
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Rule {id: $id}) "
                f"{assignment} n:Entity, n.body=$body, n.tenant_id=$tenant_id, n.status=$status, "
                "n.corroboration_count=$cc, n.reference_only=$ref, n.polarity=$pol, "
                "n.plane=$plane, n.created_at=$created_at",
                id=rule.id, body=rule.body, tenant_id=rule.tenant_id,
                status=rule.status.value, cc=rule.corroboration_count,
                ref=rule.reference_only, pol=rule.polarity.value,
                plane=rule.plane.value,
                created_at=rule.created_at,
            )

    def get_rule(self, rule_id: str) -> Rule | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (n:Rule {id:$id}) RETURN n", id=rule_id).single()
        if rec is None:
            return None
        n = rec["n"]
        return Rule(
            id=n["id"], body=n["body"], tenant_id=n["tenant_id"],
            status=RuleStatus(n["status"]), corroboration_count=n["corroboration_count"],
            reference_only=n.get("reference_only", False),
            plane=Plane(n.get("plane", Plane.DATA.value)),
            polarity=Polarity(n.get("polarity", "prescribe")),
            created_at=n["created_at"].to_native(),
        )

    def upsert_transaction(self, transaction: Transaction) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Transaction {id:$id}) "
                "SET n:Entity, n.signal_type=$st, n.source_runtime=$sr, "
                "n.sanitised_payload_ref=$ref, n.timestamp=$ts, n.tenant_id=$tenant_id, "
                "n.source_ref=$source_ref, n.skill_hint=$skill_hint, "
                "n.principal_id=$principal_id, n.source_agent_id=$source_agent_id, "
                "n.summary=$summary, n.held_reason=$held_reason, "
                "n.person_id=$person_id, n.assurance=$assurance, "
                "n.admitted=$admitted, n.signal_confidence=$signal_confidence, "
                "n.repo=$repo, n.scope_reviewed=$scope_reviewed, n.licence_hold=$licence_hold, "
                "n.workflow_state=$workflow_state, n.workflow_decision=$workflow_decision, "
                "n.workflow_rule_id=$workflow_rule_id, n.workflow_safety_digest=$workflow_safety_digest",
                id=transaction.id, st=transaction.signal_type.value,
                sr=transaction.source_runtime.value, ref=transaction.sanitised_payload_ref,
                ts=transaction.timestamp, tenant_id=transaction.tenant_id,
                source_ref=transaction.source_ref, skill_hint=transaction.skill_hint,
                principal_id=transaction.principal_id, source_agent_id=transaction.source_agent_id,
                summary=transaction.summary,
                held_reason=transaction.held_reason,
                person_id=transaction.person_id,
                assurance=int(transaction.assurance) if transaction.assurance is not None else None,
                admitted=transaction.admitted,
                signal_confidence=transaction.signal_confidence,
                # Already the normalised key by the time it reaches here
                # (`correction_payload` is the one door that normalises); the
                # adapter carries it, it does not re-derive it.
                repo=transaction.repo,
                scope_reviewed=transaction.scope_reviewed,
                licence_hold=transaction.licence_hold,
                workflow_state=transaction.workflow_state,
                workflow_decision=transaction.workflow_decision,
                workflow_rule_id=transaction.workflow_rule_id,
                workflow_safety_digest=transaction.workflow_safety_digest,
            )

    def workflow_state_for(self, transaction_id: str) -> str:
        with self._driver.session() as session:
            record = session.run("MATCH (t:Transaction {id:$id}) RETURN t.workflow_state AS state, "
                                 "t.compile_status AS legacy", id=transaction_id).single()
        if record is None:
            return "received"
        return record["state"] or ("failed" if record["legacy"] == "failed" else "received")

    def get_transaction(self, transaction_id: str) -> Transaction | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (n:Transaction {id:$id}) RETURN n", id=transaction_id
            ).single()
        if rec is None:
            return None
        return self._transaction_from_node(rec["n"])

    def _transaction_from_node(self, n) -> Transaction:
        return Transaction(
            id=n["id"], signal_type=SignalType(n["signal_type"]),
            source_runtime=SourceRuntime(n["source_runtime"]),
            sanitised_payload_ref=n["sanitised_payload_ref"],
            timestamp=n["timestamp"].to_native(), tenant_id=n["tenant_id"],
            source_ref=n.get("source_ref"),
            skill_hint=n.get("skill_hint"),
            principal_id=n.get("principal_id"),
            source_agent_id=n.get("source_agent_id"),
            summary=n.get("summary"),
            held_reason=n.get("held_reason"),
            person_id=n.get("person_id"),
            assurance=Assurance(int(n["assurance"])) if n.get("assurance") is not None else None,
            # Pre-change nodes carry no property; absent reads as not admitted,
            # which is the safe direction (the gate parks, a human decides).
            admitted=bool(n.get("admitted")),
            # None on every node written before this was persisted, and on
            # anything a person stated: both mean "no estimate", which is what
            # a reader should see rather than a fabricated zero.
            signal_confidence=n.get("signal_confidence"),
            # Missing repository context remains absent on legacy records.
            repo=n.get("repo"),
            # An absent review marker must not imply a decision was made.
            scope_reviewed=bool(n.get("scope_reviewed")),
            licence_hold=n.get("licence_hold"),
            workflow_state=n.get("workflow_state"),
            workflow_decision=n.get("workflow_decision"),
            workflow_rule_id=n.get("workflow_rule_id"),
            workflow_safety_digest=n.get("workflow_safety_digest"),
        )

    def upsert_skill(self, skill: Skill) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Skill {id:$id}) "
                "SET n:Entity, n.name=$name, n.description=$desc, n.domain=$domain, "
                "n.tenant_id=$tenant_id, n.status=$status, n.curated=$curated, "
                "n.origin=$origin, n.repo=$repo, n.publish_enabled=$publish_enabled, "
                "n.import_source_ref=$import_source_ref, n.import_name=$import_name",
                id=skill.id, name=skill.name, desc=skill.description,
                domain=skill.domain, tenant_id=skill.tenant_id,
                status=skill.status.value, origin=skill.origin.value,
                # Sorted, so the stored list is stable and two writes of the
                # same set do not look like a change in the audit trail.
                curated=sorted(skill.curated),
                repo=skill.repo,
                publish_enabled=skill.publish_enabled,
                import_source_ref=skill.import_source_ref,
                import_name=skill.import_name,
            )

    def get_skill(self, skill_id: str) -> Skill | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (n:Skill {id:$id}) RETURN n", id=skill_id).single()
            if rec is None:
                return None
            return self._skill_from_node(rec["n"])

    def delete_skill(self, skill_id: str) -> None:
        # The skill's own custody structure goes with it. `DETACH DELETE` on the
        # Skill alone dropped the HAS_SECTION edges and left the Section,
        # ContentBlock and Example nodes behind, still carrying their text and
        # still naming the skill in `skill_id`.
        #
        # That is worse than untidy, because `upsert_section` MERGEs on the
        # section id: re-importing a package with the same skill id reattaches
        # to the orphaned sections and silently re-adopts their blocks. Deleted
        # prose would come back, from a skill an administrator believed gone,
        # without appearing in any diff.
        #
        # Rules are deliberately NOT deleted - they survive unanchored, because
        # provenance outlives the folder (SkillAdminService.delete_skill says
        # so, and reports the count). Only what the skill itself owned goes.
        #
        # Skill versions go too, and for the same reattachment reason as
        # sections: skill ids are a deterministic slug of the name
        # (_skill_slug), so deleting a skill and later creating one with the
        # same name inherits the same id. A version left behind would hand the
        # new skill the deleted skill's whole history - parts_json and
        # rules_json included - and a later restore would write back prose an
        # administrator deleted. The AdminEvent stream keeps the fact of the
        # deletion, as it does today.
        with self._driver.session() as session:
            session.run(
                "MATCH (sk:Skill {id:$id}) "
                "OPTIONAL MATCH (sk)-[:HAS_SECTION]->(sec:Section) "
                "OPTIONAL MATCH (sec)-[:CONTAINS_BLOCK]->(b:ContentBlock) "
                # Examples attach three ways (upsert_example): to a section, to
                # a rule, or straight to the skill when they arrived before any
                # section. The first and third belong to this skill and go; the
                # rule-attached ones stay, because their rule stays.
                "OPTIONAL MATCH (sec)-[:HAS_EXAMPLE]->(se:Example) "
                "OPTIONAL MATCH (sk)-[:HAS_EXAMPLE]->(ke:Example) "
                "OPTIONAL MATCH (sk)-[:HAS_VERSION]->(v:SkillVersion) "
                "DETACH DELETE sk, sec, b, se, ke, v",
                id=skill_id)

    def _skill_from_node(self, n) -> Skill:
        # Skills predating the status property (null) read as active, and those
        # predating `curated` as curated by nobody - which is what they are.
        return Skill(id=n["id"], name=n["name"], description=n["description"],
                     domain=n["domain"], tenant_id=n["tenant_id"],
                     status=SkillStatus(n.get("status", "active")),
                     curated=frozenset(n.get("curated") or ()),
                     # `or` rather than a bare get: a node written before this
                     # field exists returns None, and IMPORTED is the honest
                     # reading of an old row.
                     origin=SkillOrigin(n.get("origin") or SkillOrigin.IMPORTED.value),
                     # A bare `get`, because a node written before this field
                     # existed carries no property and None is the honest
                     # reading of an old row.
                     repo=n.get("repo"), publish_enabled=n.get("publish_enabled") is not False,
                     import_source_ref=n.get("import_source_ref"), import_name=n.get("import_name"))

    def graph_node(self, node_id, tenant_id):
        from oms.adapters.graph_view import GRAPH_LABELS, graph_record
        with self._driver.session() as session:
            row = session.run(
                "MATCH (n {id:$id}) WHERE any(label IN labels(n) WHERE label IN $labels) "
                "AND (n.tenant_id=$tenant OR (n:Tag AND EXISTS { MATCH (n)--(owner {tenant_id:$tenant}) })) "
                "RETURN properties(n) AS props, labels(n) AS kinds LIMIT 1",
                id=node_id, tenant=tenant_id, labels=list(GRAPH_LABELS)).single()
            if row is None:
                return None
            return graph_record(row["props"], next(k for k in GRAPH_LABELS if k in row["kinds"]))

    def graph_neighbours(self, node_id, tenant_id, *, offset=0, limit=100):
        from oms.adapters.graph_view import GRAPH_LABELS, graph_record
        match = ("MATCH (n {id:$id})-[r]-(other) "
            "WHERE (n.tenant_id=$tenant OR (n:Tag AND EXISTS { MATCH (n)--(:Entity {tenant_id:$tenant}) })) "
            "AND any(label IN labels(other) WHERE label IN $labels) "
            "AND (other.tenant_id=$tenant OR (other:Tag AND other.tenant_id IS NULL)) ")
        with self._driver.session() as session:
            # Count and page within one read transaction so the cursor describes
            # one consistent neighbourhood, independent of graph search limits.
            def read(tx):
                total = tx.run(match + "RETURN count(DISTINCT other) AS total", id=node_id,
                    tenant=tenant_id, labels=list(GRAPH_LABELS)).single()["total"]
                rows = list(tx.run(match +
                    "WITH other, collect(DISTINCT {source:startNode(r).id, target:endNode(r).id, type:type(r)}) AS edges "
                    "ORDER BY other.id SKIP $offset LIMIT $limit "
                    "RETURN properties(other) AS props, labels(other) AS kinds, edges",
                    id=node_id, tenant=tenant_id, labels=list(GRAPH_LABELS), offset=offset, limit=limit))
                return {"nodes": [graph_record(row["props"], next(k for k in GRAPH_LABELS if k in row["kinds"])) for row in rows],
                        "edges": [edge for row in rows for edge in row["edges"]], "total": total,
                        "next_offset": offset + limit if offset + limit < total else None}
            return session.execute_read(read)

    def upsert_tag(self, tag_id: str, name: str) -> None:
        with self._driver.session() as session:
            session.run("MERGE (n:Tag {id:$id}) SET n:Entity, n.name=$name", id=tag_id, name=name)

    def attach_edge(self, edge: Edge) -> None:
        """Create or update one edge between two nodes that already exist.

        Warns when it did nothing, and that warning is the whole point of this
        docstring. A Cypher MATCH that finds no rows is not an error: the MERGE
        simply never runs and the driver returns a healthy summary. So an edge
        onto a node missing the `:Entity` label - every node written before
        that label was introduced (see `schema.ENTITY_ID_INDEX`, which
        documents a backfill nobody had run) - is silently not created.

        A warning rather than a raise, deliberately. Bulk import attaches
        thousands of edges in a loop, and turning a data-integrity problem into
        a mid-import crash trades a quiet bug for a loud outage. The counter is
        what makes it actionable: `relationships_created` is 0 for a no-op and
        also 0 for an edge that already existed, so the endpoints are checked
        instead - that distinguishes "nothing to do" from "nothing found".
        """
        # edge.type is a controlled EdgeType enum, safe to interpolate as the relationship type.
        query = (
            "MATCH (a:Entity {id:$from_id}), (b:Entity {id:$to_id}) "
            f"MERGE (a)-[rel:{edge.type.value}]->(b) "
            "SET rel += $props "
            "RETURN 1 AS attached"
        )
        with self._driver.session() as session:
            attached = session.run(
                query, from_id=edge.from_id, to_id=edge.to_id,
                props=edge.properties).single()
            if attached is not None:
                return
            missing = session.run(
                "OPTIONAL MATCH (a:Entity {id:$from_id}) "
                "OPTIONAL MATCH (b:Entity {id:$to_id}) "
                "RETURN a IS NULL AS from_missing, b IS NULL AS to_missing",
                from_id=edge.from_id, to_id=edge.to_id).single()
        ends = []
        if missing is None or missing["from_missing"]:
            ends.append(f"from {edge.from_id!r}")
        if missing is None or missing["to_missing"]:
            ends.append(f"to {edge.to_id!r}")
        logger.warning(
            "%s edge NOT created: no :Entity node for %s. The edge was dropped "
            "silently; run scripts/migrate_entity_label.py if these nodes "
            "predate the label.",
            edge.type.value, " and ".join(ends) or "an unknown endpoint")

    def detach_edge(self, edge: Edge) -> None:
        # Type interpolation is safe as in attach_edge (controlled EdgeType enum).
        # Deletes every matching relationship; a no-op when none match.
        query = (
            "MATCH (a:Entity {id:$from_id})"
            f"-[rel:{edge.type.value}]->"
            "(b {id:$to_id}) DELETE rel"
        )
        with self._driver.session() as session:
            session.run(query, from_id=edge.from_id, to_id=edge.to_id)

    def superseders_of(self, rule_id: str) -> list[str]:
        with self._driver.session(default_access_mode="READ") as session:
            recs = session.run(
                "MATCH (survivor:Rule)-[:SUPERSEDES]->(:Rule {id:$rid}) "
                "RETURN DISTINCT survivor.id AS id ORDER BY id",
                rid=rule_id,
            )
            return [r["id"] for r in recs]

    def rules_by_tags(self, tags: set[str], tenant_id: str, limit: int = 50) -> list[Rule]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Rule)-[:TAGGED_WITH]->(t:Tag) "
                "WHERE t.name IN $tags AND n.tenant_id = $tenant "
                "RETURN DISTINCT n LIMIT $limit",
                tags=list(tags), tenant=tenant_id, limit=limit,
            )
            return [self._rule_from_node(r["n"]) for r in recs]

    def skills_by_tags(self, tags: set[str], tenant_id: str) -> list[Skill]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Skill)-[:TAGGED_WITH]->(t:Tag) "
                "WHERE t.name IN $tags AND n.tenant_id = $tenant "
                "RETURN DISTINCT n ORDER BY n.id",
                tags=list(tags), tenant=tenant_id,
            )
            return [self._skill_from_node(r["n"]) for r in recs]

    def tags_for_skill(self, skill_id: str) -> list[str]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Skill {id:$sid})-[:TAGGED_WITH]->(t:Tag) "
                "RETURN DISTINCT t.name AS name ORDER BY name",
                sid=skill_id,
            )
            return [r["name"] for r in recs]

    def rules_for_skill(self, skill_id: str) -> list[Rule]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Rule)-[:BELONGS_TO]->(:Skill {id:$sid}) RETURN n",
                sid=skill_id,
            )
            return [self._rule_from_node(r["n"]) for r in recs]

    def skills_for_rule(self, rule_id: str) -> list[Skill]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Rule {id:$rid})-[:BELONGS_TO]->(s:Skill) "
                "RETURN DISTINCT s ORDER BY s.id",
                rid=rule_id,
            )
            return [self._skill_from_node(r["s"]) for r in recs]

    def skills_by_rule(self, rule_ids: list[str]) -> dict[str, list[Skill]]:
        if not rule_ids:
            return {}
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (r:Rule)-[:BELONGS_TO]->(s:Skill) WHERE r.id IN $ids "
                "RETURN r.id AS rid, s ORDER BY r.id, s.id",
                ids=list(rule_ids),
            )
            out: dict[str, list[Skill]] = {}
            for rec in recs:
                out.setdefault(rec["rid"], []).append(self._skill_from_node(rec["s"]))
        return out

    def lineage(self, rule_id: str) -> list[Transaction]:
        with self._driver.session() as session:
            recs = session.run(
                # Oldest first, promised by the port: the rule story reads this
                # as a timeline and an unordered read renders the
                # reinforcements before the correction they reinforce.
                "MATCH (:Rule {id:$rid})-[:DERIVED_FROM]->(t:Transaction) "
                "RETURN t ORDER BY t.timestamp",
                rid=rule_id,
            )
            return [self._transaction_from_node(rec["t"]) for rec in recs]

    def rule_context(self, rule_ids: list[str]) -> dict[str, RuleContext]:
        if not rule_ids:
            return {}
        with self._driver.session() as session:
            recs = session.run(
                # One query for the whole page. OPTIONAL on both legs so a rule
                # with no skill, or none whose lineage names a person, still
                # comes back - absent means "no such rule", which is a
                # different answer the caller is entitled to.
                "MATCH (r:Rule) WHERE r.id IN $ids "
                "OPTIONAL MATCH (r)-[:BELONGS_TO]->(s:Skill) "
                "WITH r, collect(DISTINCT s.name) AS names, "
                "     collect(DISTINCT s.domain) AS domains "
                "OPTIONAL MATCH (r)-[:DERIVED_FROM]->(t:Transaction) "
                "WHERE t.person_id IS NOT NULL "
                # The correction it was BORN from, not the latest one to
                # corroborate it: oldest wins.
                "WITH r, names, domains, t ORDER BY t.timestamp "
                "WITH r, names, domains, collect(t.person_id)[0] AS contributor "
                "RETURN r.id AS rid, names, domains, contributor",
                ids=list(rule_ids),
            )
            return {
                rec["rid"]: RuleContext(
                    rule_id=rec["rid"],
                    skills=tuple(n for n in rec["names"] if n),
                    domains=tuple(sorted({d for d in rec["domains"] if d})),
                    contributor_person_id=rec["contributor"])
                for rec in recs
            }

    def rules_derived_from(self, transaction_ids: list[str]) -> dict[str, str]:
        if not transaction_ids:
            return {}
        with self._driver.session() as session:
            recs = session.run(
                # :Rule on the left, deliberately: DERIVED_FROM also hangs off a
                # revised ContentBlock, and a block id where a rule id is
                # expected would group a section edit under a rule that does
                # not exist.
                "MATCH (r:Rule)-[:DERIVED_FROM]->(t:Transaction) "
                "WHERE t.id IN $ids "
                "RETURN t.id AS tid, collect(r.id)[0] AS rid",
                ids=list(transaction_ids),
            )
            return {rec["tid"]: rec["rid"] for rec in recs}

    def transactions_for_tenant(self, tenant_id: str,
                                since: datetime | None = None,
                                limit: int = 200) -> list[Transaction]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (t:Transaction {tenant_id:$tid}) "
                "WHERE ($since IS NULL OR t.timestamp >= $since) "
                "RETURN t ORDER BY t.timestamp DESC LIMIT $limit",
                tid=tenant_id, since=since, limit=limit,
            )
            return [self._transaction_from_node(rec["t"]) for rec in recs]

    def rules_for_tenant(self, tenant_id: str, limit: int = 500) -> list[Rule]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Rule {tenant_id:$tid}) "
                "RETURN n ORDER BY n.created_at DESC LIMIT $limit",
                tid=tenant_id, limit=limit,
            )
            return [self._rule_from_node(r["n"]) for r in recs]

    def rules_page(self, tenant_id: str, *, status: RuleStatus | None = None,
                   skill_id: str | None = None, query: str | None = None,
                   limit: int = 200, offset: int = 0) -> RulePage:
        # One WHERE, written once and used by both the count and the window.
        # Two copies would eventually disagree, and the symptom is a screen
        # that says "5,437 rules" over a list of six.
        where = ("n.tenant_id = $tid "
                 "AND ($status IS NULL OR n.status = $status) "
                 "AND ($q IS NULL OR toLower(n.body) CONTAINS toLower($q)) "
                 "AND ($skill IS NULL OR (n)-[:BELONGS_TO]->(:Skill {id: $skill}))")
        params = {"tid": tenant_id,
                  "status": None if status is None else status.value,
                  "q": (query or None), "skill": skill_id,
                  "limit": limit, "offset": offset}
        with self._driver.session() as session:
            total = session.run(
                f"MATCH (n:Rule) WHERE {where} RETURN count(n) AS n", **params
            ).single()["n"]
            recs = session.run(
                # The id is the final sort key and it is load-bearing: 5,393 of
                # acme's rules were written by one bulk import and share a
                # created_at to the second, so created_at alone is not a total
                # order and a page boundary would repeat or skip rows.
                f"MATCH (n:Rule) WHERE {where} "
                "RETURN n ORDER BY n.created_at DESC, n.id "
                "SKIP $offset LIMIT $limit",
                **params,
            )
            items = [self._rule_from_node(r["n"]) for r in recs]
        return RulePage(items=items, total=total, offset=offset, limit=limit)

    def record_quarantine(self, item: QuarantinedPayload) -> None:
        # A distinct label, not a Transaction: a quarantined payload never
        # entered the pipeline, has no sanitised_payload_ref to point at, and
        # must never be picked up by a drain. MERGE on the id makes a retried
        # payload one row rather than one per attempt, matching ingest's own
        # idempotency.
        with self._driver.session() as session:
            session.run(
                "MERGE (q:QuarantinedPayload {transaction_id: $tx}) "
                "SET q:Entity, q.tenant_id=$tid, q.reason=$reason, "
                "q.quarantined_at=$at, q.principal_id=$pid",
                tx=item.transaction_id, tid=item.tenant_id, reason=item.reason,
                at=item.quarantined_at, pid=item.principal_id,
            )

    def quarantined_payloads(self, tenant_id: str,
                             limit: int = 100) -> list[QuarantinedPayload]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (q:QuarantinedPayload {tenant_id:$tid}) "
                "RETURN q ORDER BY q.quarantined_at DESC LIMIT $limit",
                tid=tenant_id, limit=limit,
            )
            return [QuarantinedPayload(
                transaction_id=r["q"]["transaction_id"],
                tenant_id=r["q"]["tenant_id"],
                # Same coalesce-in-Python reasoning as failed_transactions: a
                # row written before this carried a reason should still read.
                reason=r["q"].get("reason") or "no reason recorded",
                quarantined_at=r["q"]["quarantined_at"].to_native(),
                principal_id=r["q"].get("principal_id"),
            ) for r in recs]

    def failed_transactions(self, tenant_id: str,
                            limit: int = 100) -> list[tuple[Transaction, str]]:
        with self._driver.session() as session:
            recs = session.run(
                f"MATCH (t:Transaction {{tenant_id:$tid}}) "
                f"WHERE {_LISTABLE_FAULT} "
                "RETURN t ORDER BY t.timestamp DESC LIMIT $limit",
                tid=tenant_id, limit=limit,
            )
            # coalesce in Python rather than Cypher: a failure marked by an
            # older write path may carry no compile_error, and "no reason
            # recorded" is the honest rendering of that.
            return [(self._transaction_from_node(rec["t"]),
                     rec["t"].get("compile_error") or "no reason recorded")
                    for rec in recs]

    def text_search(self, query: str, tenant_id: str, limit: int = 20) -> list[tuple[str, float]]:
        terms = fulltext_terms(query)
        if terms is None:
            return []
        with self._driver.session() as session:
            recs = session.run(
                "CALL db.index.fulltext.queryNodes('rule_fulltext', $q) "
                "YIELD node, score WHERE node.tenant_id = $tenant "
                "RETURN node.id AS id, score LIMIT $limit",
                q=terms, tenant=tenant_id, limit=limit,
            )
            return [(r["id"], r["score"]) for r in recs]

    def upsert_learning(self, learning: Learning) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Learning {id:$id}) "
                "SET n:Entity, n.body=$body, n.source_transaction=$src, n.tenant_id=$tenant_id",
                id=learning.id, body=learning.body,
                src=learning.source_transaction, tenant_id=learning.tenant_id,
            )

    def get_learning(self, learning_id: str) -> Learning | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (n:Learning {id:$id}) RETURN n", id=learning_id).single()
        if rec is None:
            return None
        n = rec["n"]
        return Learning(id=n["id"], body=n["body"],
                        source_transaction=n["source_transaction"], tenant_id=n["tenant_id"])

    def upsert_constraint(self, constraint: Constraint) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Constraint {id:$id}) "
                "SET n:Entity, n.body=$body, n.tenant_id=$tenant_id, n.immutable=$imm, "
                "n.polarity=$pol, n.status=$status, n.source=$source",
                id=constraint.id, body=constraint.body,
                tenant_id=constraint.tenant_id, imm=constraint.immutable,
                pol=constraint.polarity.value,
                status=constraint.status.value, source=constraint.source.value,
            )

    @staticmethod
    def _constraint_from_node(n: Node) -> Constraint:
        # `coalesce` in the caller's Cypher and `.get` here, both defaulting the
        # same way, because a constraint written before these two properties
        # existed carries neither. Reading a missing `status` as RETIRED would
        # silently unbind every constraint in every graph OMS has ever written.
        return Constraint(
            id=n["id"], body=n["body"], tenant_id=n["tenant_id"],
            immutable=n["immutable"],
            polarity=Polarity(n.get("polarity", "prescribe")),
            status=ConstraintStatus(n.get("status", "active")),
            source=ConstraintSource(n.get("source", "import")))

    def active_constraints(self, tenant_id: str) -> list[Constraint]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Constraint {tenant_id:$tid}) "
                "WHERE coalesce(n.status, 'active') = 'active' "
                "RETURN n", tid=tenant_id
            )
            return [self._constraint_from_node(n) for n in (r["n"] for r in recs)]

    def all_constraints(self, tenant_id: str) -> list[Constraint]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Constraint {tenant_id:$tid}) RETURN n", tid=tenant_id
            )
            return [self._constraint_from_node(n) for n in (r["n"] for r in recs)]

    def set_constraint_status(self, constraint_id: str,
                              status: ConstraintStatus) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (n:Constraint {id:$id}) SET n.status=$status",
                id=constraint_id, status=status.value,
            )

    def upsert_example(self, example: Example) -> None:
        def _tx(tx):
            tx.run(
                "MERGE (n:Example {id:$id}) "
                "SET n:Entity, n.body=$body, n.kind=$kind, n.tenant_id=$tid, "
                "n.parent_rule_id=$prid, n.parent_skill_id=$psid, "
                "n.parent_section_id=$pseid, n.name=$name, n.original_label=$lbl, "
                "n.source_ref=$src, n.order=$order",
                id=example.id, body=example.body, kind=example.kind.value,
                tid=example.tenant_id,
                prid=example.parent_rule_id, psid=example.parent_skill_id,
                pseid=example.parent_section_id,
                name=example.name, lbl=example.original_label,
                src=example.source_ref, order=example.order,
            )
            # Attach to the appropriate parent. Section attachment is the default;
            # rule/skill attachment are the override / orphan cases.
            if example.parent_section_id is not None:
                tx.run(
                    "MATCH (e:Example {id:$id}), (s:Section {id:$sid}) "
                    "MERGE (s)-[:HAS_EXAMPLE]->(e)",
                    id=example.id, sid=example.parent_section_id,
                )
            elif example.parent_rule_id is not None:
                tx.run(
                    "MATCH (e:Example {id:$id}), (r:Rule {id:$rid}) "
                    "MERGE (r)-[:HAS_EXAMPLE]->(e) "
                    "MERGE (e)-[:ILLUSTRATES]->(r)",
                    id=example.id, rid=example.parent_rule_id,
                )
            elif example.parent_skill_id is not None:
                tx.run(
                    "MATCH (e:Example {id:$id}), (s:Skill {id:$sid}) "
                    "MERGE (s)-[:HAS_EXAMPLE]->(e) "
                    "MERGE (e)-[:ILLUSTRATES]->(s)",
                    id=example.id, sid=example.parent_skill_id,
                )

        with self._driver.session() as session:
            session.execute_write(_tx)

    def attach_example(self, example: Example) -> None:
        """Attach an example to its parent. Same as upsert_example today; kept as a
        distinct method on the protocol so callers can express intent."""
        self.upsert_example(example)

    def examples_for_rule(self, rule_id: str) -> list[Example]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (e:Example)-[:ILLUSTRATES]->(:Rule {id:$rid}) RETURN e", rid=rule_id
            )
            return [self._example_from_node(r["e"]) for r in recs]

    def examples_for_skill(self, skill_id: str) -> list[Example]:
        # Examples are stored once, under their section; skill-level retrieval
        # traverses section membership. Directly attached examples (ILLUSTRATES)
        # are kept for rows written before single-parent storage.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (e:Example)-[:ILLUSTRATES]->(:Skill {id:$sid}) RETURN e "
                "UNION "
                "MATCH (:Skill {id:$sid})-[:HAS_SECTION]->(:Section)-[:HAS_EXAMPLE]->(e:Example) "
                "RETURN e",
                sid=skill_id,
            )
            return [self._example_from_node(r["e"]) for r in recs]

    def _example_from_node(self, n) -> Example:
        return Example(
            id=n["id"], body=n["body"], kind=ExampleKind(n["kind"]),
            tenant_id=n["tenant_id"],
            name=n.get("name"),
            original_label=n.get("original_label"),
            parent_section_id=n.get("parent_section_id"),
            parent_rule_id=n.get("parent_rule_id"),
            parent_skill_id=n.get("parent_skill_id"),
            source_ref=n.get("source_ref"),
            order=n.get("order"),
        )

    def skills_for_tenant(self, tenant_id: str) -> list[Skill]:
        with self._driver.session() as session:
            recs = session.run("MATCH (n:Skill {tenant_id:$tid}) RETURN n", tid=tenant_id)
            return [self._skill_from_node(r["n"]) for r in recs]

    def transactions_for_person(self, person_id: str, tenant_id: str) -> list[Transaction]:
        # Oldest first, matching pending_transactions and the in-memory
        # adapter. `t.person_id IS NOT NULL` is written out rather than left
        # implicit in the property match: it says in the query what the
        # contract requires, that unbound work (§8.1's HELD outcome) is never
        # attributed to anybody, instead of relying on the reader knowing that
        # a null parameter silently matches nothing.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (t:Transaction {tenant_id:$tid}) "
                "WHERE t.person_id IS NOT NULL AND t.person_id = $pid "
                "RETURN t ORDER BY t.timestamp",
                pid=person_id, tid=tenant_id,
            )
            return [self._transaction_from_node(rec["t"]) for rec in recs]

    def transaction_count_for_person(self, person_id: str, tenant_id: str) -> int:
        # The same MATCH and the same WHERE as transactions_for_person - kept
        # character for character so the two cannot answer different questions -
        # aggregated in the database instead of returned. `t.person_id IS NOT
        # NULL` is again written out rather than left to the null-comparison
        # rule, and it matters more here: a list of somebody else's work is
        # visibly wrong the moment a reader opens it, whereas a wrong count is
        # just a number, and this one is what an administrator reads before
        # deciding whether to trust the person.
        #
        # No ORDER BY: order is not observable through a count, and asking for
        # one would make the store sort rows it is about to discard.
        #
        # count(t) is always a row, so `.single()` cannot be None here; the
        # query returns 0 for a person with nothing, which is the answer the
        # contract requires for an unknown person and for None.
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (t:Transaction {tenant_id:$tid}) "
                "WHERE t.person_id IS NOT NULL AND t.person_id = $pid "
                "RETURN count(t) AS n",
                pid=person_id, tid=tenant_id,
            ).single()
            return int(rec["n"])

    def transaction_counts_by_person_for_tenant(self, tenant_id: str) -> dict[str, int]:
        # The per-person count for the whole tenant in one query: the same MATCH
        # and the same aggregate, grouped by person_id instead of filtered to
        # one. Cypher groups by whatever non-aggregated key is returned, so
        # `RETURN t.person_id, count(t)` is the GROUP BY.
        #
        # `t.person_id IS NOT NULL` is doing real work here, unlike in the two
        # per-person methods above where it restates what a comparison against a
        # null parameter already does. There is no parameter to compare against
        # in a grouped query: null is a group like any other, so without this
        # clause every unattributed transaction in the tenant comes back under a
        # null key, and the caller renders somebody's row - or invents a person -
        # from work that belongs to nobody (§12). The conformance contract
        # asserts `None not in counts` for exactly this mutation.
        #
        # Backed by transaction_person index (schema.py), which is on
        # (tenant_id, person_id) in that order: the tenant match seeks and the
        # person_id grouping reads the index rather than the store.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (t:Transaction {tenant_id:$tid}) "
                "WHERE t.person_id IS NOT NULL "
                "RETURN t.person_id AS pid, count(t) AS n",
                tid=tenant_id,
            )
            return {rec["pid"]: int(rec["n"]) for rec in recs}

    def pending_transactions(self, limit: int = 100,
                             tenant_id: str | None = None) -> list[Transaction]:
        # Oldest first; any transaction carrying a compile_status (marked
        # compile-failed, e.g. missing payload, or parked awaiting admission
        # per §9.2) is excluded so it does not starve new work out of the
        # LIMIT window forever.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (t:Transaction) WHERE NOT (:Rule)-[:DERIVED_FROM]->(t) "
                "AND t.compile_status IS NULL "
                "AND t.compile_owner IS NULL "
                "AND (t.workflow_state IS NULL OR t.workflow_state='queued_automation') "
                "AND t.held_reason IS NULL "
                "AND ($tenant IS NULL OR t.tenant_id = $tenant) "
                "RETURN t ORDER BY t.timestamp LIMIT $limit",
                limit=limit, tenant=tenant_id,
            )
            return [self._transaction_from_node(rec["t"]) for rec in recs]

    def pending_tenants(self) -> list[str]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (t:Transaction) WHERE NOT (:Rule)-[:DERIVED_FROM]->(t) "
                "AND t.compile_status IS NULL "
                "AND t.compile_owner IS NULL "
                "AND (t.workflow_state IS NULL OR t.workflow_state='queued_automation') "
                "AND t.held_reason IS NULL "
                "RETURN DISTINCT t.tenant_id AS tenant ORDER BY tenant",
            )
            return [rec["tenant"] for rec in recs]

    def release_transaction(self, transaction_id: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (t:Transaction {id:$id}) REMOVE t.held_reason "
                "SET t.workflow_state=CASE WHEN t.workflow_state='held_safety' THEN 'queued_automation' ELSE t.workflow_state END",
                id=transaction_id,
            )

    def mark_compile_failed(self, transaction_id: str, reason: str,
                            fault: bool = True) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (t:Transaction {id:$id}) "
                "SET t.compile_status='failed', t.compile_error=$reason, "
                "t.compile_fault=$fault, t.workflow_state=CASE WHEN t.workflow_state IS NULL THEN null ELSE 'failed' END",
                id=transaction_id, reason=reason, fault=fault,
            )

    def dismiss_failure(self, transaction_id: str, tenant_id: str,
                        actor_person_id: str | None = None) -> bool:
        with self._driver.session() as session:
            rec = session.run(
                # The same predicate failed_transactions lists by, so an id
                # that is not a listable fault - already dismissed, a skill
                # import, a review rejection, another tenant's - returns
                # nothing and the route answers 404 rather than reporting
                # success over a write that matched no node.
                f"MATCH (t:Transaction {{id:$id, tenant_id:$tid}}) "
                f"WHERE {_LISTABLE_FAULT} "
                "SET t.failure_dismissed_at=$at, t.failure_dismissed_by=$by "
                "RETURN t.id AS id",
                id=transaction_id, tid=tenant_id,
                at=datetime.now(timezone.utc).isoformat(), by=actor_person_id,
            ).single()
        return rec is not None

    def dismiss_all_failures(self, tenant_id: str,
                             actor_person_id: str | None = None) -> int:
        with self._driver.session() as session:
            rec = session.run(
                f"MATCH (t:Transaction {{tenant_id:$tid}}) "
                f"WHERE {_LISTABLE_FAULT} "
                "SET t.failure_dismissed_at=$at, t.failure_dismissed_by=$by "
                "RETURN count(t) AS n",
                tid=tenant_id,
                at=datetime.now(timezone.utc).isoformat(), by=actor_person_id,
            ).single()
        return int(rec["n"]) if rec is not None else 0

    def claim_compile(self, transaction_id: str, owner: str) -> bool:
        def claim(tx):
            tx.run("MERGE (c:ContributionLock {id:$id}) SET c.locked=true REMOVE c.locked",
                   id=transaction_id).consume()
            # Acquire the node's write lock BEFORE checking eligibility. Both
            # statements execute in one transaction; another worker waits.
            tx.run("MATCH (t:Transaction {id:$id}) SET t._compile_lock=true "
                   "REMOVE t._compile_lock", id=transaction_id).consume()
            result = tx.run(
                "MATCH (t:Transaction {id:$id}) "
                "WHERE t.compile_owner IS NULL AND t.compile_status IS NULL "
                "AND (t.workflow_state IS NULL OR t.workflow_state='queued_automation') "
                "AND t.held_reason IS NULL AND NOT (:Rule)-[:DERIVED_FROM]->(t) "
                "SET t.compile_owner=$owner, t.compile_started_at=datetime(), "
                "t.workflow_state=CASE WHEN t.workflow_state IS NULL THEN null ELSE 'processing' END RETURN t.id AS id",
                id=transaction_id, owner=owner).single()
            return result is not None
        with self._driver.session() as session:
            return session.execute_write(claim)

    def release_compile(self, transaction_id: str, owner: str) -> None:
        with self._driver.session() as session:
            session.run("MATCH (t:Transaction {id:$id}) WHERE t.compile_owner=$owner "
                        "REMOVE t.compile_owner, t.compile_started_at "
                        "SET t.workflow_state=CASE WHEN t.workflow_state='processing' THEN 'queued_automation' ELSE t.workflow_state END",
                        id=transaction_id, owner=owner).consume()

    def set_licence_hold(self, transaction_id: str, reason: str | None) -> None:
        with self._driver.session() as session:
            session.run("MATCH (t:Transaction {id:$id}) SET t.licence_hold=$reason",
                        id=transaction_id, reason=reason)

    def set_compile_status(self, transaction_id: str, status: CompileStatus) -> None:
        # FAILED is terminal ("never retried"), so an existing FAILED is never
        # overwritten by a later park. This adapter is where that matters
        # most: pending_transactions filters on `t.compile_status IS NULL`
        # alone, so demoting a rejected transaction to 'awaiting_admission'
        # would put it straight back in the pool AND let admit_transaction's
        # `<> 'failed'` guard pass, clearing the §9.2 gate. Explicitly
        # rejected work would then resume. mark_compile_failed still SETS the
        # failure in the first place; only overwriting one is refused.
        with self._driver.session() as session:
            session.run(
                "MATCH (t:Transaction {id:$id}) "
                "WHERE coalesce(t.compile_status, '') <> 'failed' "
                "SET t.compile_status=$status, t.workflow_state=CASE "
                "WHEN t.workflow_state IS NULL THEN null WHEN $status='failed' THEN 'failed' ELSE 'awaiting_manual_review' END",
                id=transaction_id, status=status.value,
            )

    def admit_transaction(self, transaction_id: str) -> None:
        # A permanently FAILED transaction (mark_compile_failed: "never
        # retried") stays excluded even across admission - admit_transaction
        # exists to lift the AWAITING_ADMISSION park (§9.2), not to override a
        # terminal failure. The WHERE guard makes this a no-op on 'failed',
        # matching the memory adapter, whose separate compile_failures map
        # keeps excluding a failed transaction regardless of what this method
        # clears; it also leaves compile_error untouched on a transaction that
        # stays out of the pool, rather than stranding a stale reason on one
        # that doesn't.
        #
        # `t.admitted=true` is the half that makes admission stick: clearing
        # the status alone only returns the transaction to the pending pool,
        # and the §9.2 gate would re-park it on the next drain because it is
        # still unattributed. It stays unattributed - person_id is untouched.
        with self._driver.session() as session:
            session.run(
                "MATCH (t:Transaction {id:$id}) "
                "WHERE coalesce(t.compile_status, '') <> 'failed' "
                "REMOVE t.compile_status "
                "SET t.admitted=true, t.workflow_state=CASE WHEN t.workflow_state IS NULL THEN null ELSE 'queued_automation' END",
                id=transaction_id,
            )

    def admit_scope_review(self, transaction_id: str) -> None:
        # Record the decision and clear the hold atomically. Preserve the
        # independent admission flag and never reopen terminal work.
        with self._driver.session() as session:
            session.run(
                "MATCH (t:Transaction {id:$id}) "
                "WHERE coalesce(t.compile_status, '') <> 'failed' "
                "REMOVE t.compile_status "
                "SET t.scope_reviewed=true, t.workflow_state=CASE WHEN t.workflow_state IS NULL THEN null ELSE 'queued_automation' END",
                id=transaction_id,
            )

    def rules_conflicting_with_constraints(self, tenant_id: str) -> list[Rule]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Rule {tenant_id:$tid})-[:CONFLICTS_WITH]->(:Constraint) "
                "RETURN DISTINCT n", tid=tenant_id,
            )
            return [self._rule_from_node(r["n"]) for r in recs]

    def _rule_from_node(self, n) -> Rule:
        return Rule(
            id=n["id"], body=n["body"], tenant_id=n["tenant_id"],
            status=RuleStatus(n["status"]), corroboration_count=n["corroboration_count"],
            reference_only=n.get("reference_only", False),
            plane=Plane(n.get("plane", Plane.DATA.value)),
            polarity=Polarity(n.get("polarity", "prescribe")),
            created_at=n["created_at"].to_native(),
        )




    # -- Skill-package custody operations ----------------------------------

    def upsert_section(self, section: Section) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (s:Section {id:$id}) "
                "SET s:Entity, s.skill_id=$skill_id, s.kind=$kind, s.heading=$heading, "
                "s.order=$order, s.mutability=$mut, s.tenant_id=$tid, "
                "s.created_at = coalesce(s.created_at, $created_at) "
                "WITH s "
                "MATCH (sk:Skill {id:$skill_id}) "
                "MERGE (sk)-[r:HAS_SECTION]->(s) "
                "SET r.order=$order",
                id=section.id, skill_id=section.skill_id, kind=section.kind.value,
                heading=section.heading, order=section.order,
                mut=section.mutability.value, tid=section.tenant_id,
                created_at=section.created_at,
            )

    def get_section(self, section_id: str) -> Section | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (s:Section {id:$id}) RETURN s", id=section_id).single()
        if rec is None:
            return None
        return self._section_from_node(rec["s"])

    def sections_for_skill(self, skill_id: str) -> list[Section]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (sk:Skill {id:$sid})-[:HAS_SECTION]->(s:Section) "
                "RETURN s ORDER BY s.order",
                sid=skill_id,
            )
            return [self._section_from_node(r["s"]) for r in recs]

    def _section_from_node(self, n) -> Section:
        return Section(
            id=n["id"], skill_id=n["skill_id"], kind=SectionKind(n["kind"]),
            heading=n["heading"], order=n["order"],
            mutability=Mutability(n["mutability"]),
            tenant_id=n["tenant_id"],
            created_at=n["created_at"].to_native() if n.get("created_at") else _now(),
        )

    def attach_rule(self, rule: Rule, section_id: str,
                    order: int | None = None, group: str | None = None) -> None:
        sec = self.get_section(section_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.SYSTEM_AGGREGATED:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach rule"
            )
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section {id:$sid}), (r:Rule {id:$rid}) "
                "MERGE (s)-[rel:CONTAINS_RULE]->(r) "
                "SET rel.order = $order, rel.group = $group",
                sid=section_id, rid=rule.id, order=order, group=group,
            )

    def inherit_placements(self, retired_rule_id: str, successor: Rule) -> None:
        # Copy every CONTAINS_RULE membership of the retired rule (with its order
        # and group) onto the successor, so a superseding rule renders in the
        # retired rule's place. CONTAINS_RULE only lives on system-aggregated
        # sections, so no mutability guard is needed.
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section)-[old:CONTAINS_RULE]->(:Rule {id:$rid}) "
                "MATCH (succ:Rule {id:$sid}) "
                "MERGE (s)-[new:CONTAINS_RULE]->(succ) "
                "SET new.order = old.order, new.group = old.group",
                rid=retired_rule_id, sid=successor.id,
            )

    def attach_block(self, block: ContentBlock, section_id: str) -> None:
        sec = self.get_section(section_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach block"
            )
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section {id:$sid}), (b:ContentBlock {id:$bid}) "
                "MERGE (s)-[:CONTAINS_BLOCK]->(b)",
                sid=section_id, bid=block.id,
            )

    def rules_for_section(self, section_id: str) -> list[Rule]:
        # Authorially placed rules first, in source order; unplaced (learned)
        # rules after, by corroboration.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid})-[rel:CONTAINS_RULE]->(r:Rule) "
                "RETURN r ORDER BY coalesce(rel.order, 2147483647), r.corroboration_count DESC",
                sid=section_id,
            )
            return [self._rule_from_node(r["r"]) for r in recs]

    def rule_placements_for_section(self, section_id: str) -> list[RulePlacement]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid})-[rel:CONTAINS_RULE]->(r:Rule) "
                "RETURN r.id AS rid, rel.order AS ord, rel.group AS grp",
                sid=section_id,
            )
            return [RulePlacement(rule_id=r["rid"], order=r["ord"], group=r["grp"]) for r in recs]

    def blocks_for_section(self, section_id: str) -> list[ContentBlock]:
        # Active blocks only; blocks predating the status property (null) read as
        # active for backward compatibility.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid})-[:CONTAINS_BLOCK]->(b:ContentBlock) "
                "WHERE coalesce(b.status, 'active') = 'active' RETURN b",
                sid=section_id,
            )
            return [self._block_from_node(r["b"]) for r in recs]

    def blocks_revised_for_rule(self, rule_id: str) -> list[ContentBlock]:
        # Rule and block share a transaction: the rule DERIVED_FROM it when it
        # was created or corroborated, the block DERIVED_FROM it when a
        # revision rewrote it for that rule. Active blocks only, with the
        # same null-as-active reading as `blocks_for_section`.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (r:Rule {id:$rid})-[:DERIVED_FROM]->(t:Transaction)"
                "<-[:DERIVED_FROM]-(b:ContentBlock) "
                "WHERE coalesce(b.status, 'active') = 'active' "
                "RETURN DISTINCT b",
                rid=rule_id,
            )
            return [self._block_from_node(r["b"]) for r in recs]

    def get_content_block(self, block_id: str) -> ContentBlock | None:
        # No status filter, deliberately: a review item can outlive the block it
        # names, and the reviewer needs the text the verdict was about even once
        # a later revision has superseded it.
        with self._driver.session() as session:
            rec = session.run("MATCH (b:ContentBlock {id:$id}) RETURN b",
                              id=block_id).single()
        if rec is None:
            return None
        return self._block_from_node(rec["b"])

    def section_for_block(self, block_id: str) -> Section | None:
        # The CONTAINS_BLOCK edge run backwards. A superseded block stays
        # attached for history, so this resolves for those too.
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (s:Section)-[:CONTAINS_BLOCK]->(:ContentBlock {id:$id}) "
                "RETURN s LIMIT 1",
                id=block_id,
            ).single()
        if rec is None:
            return None
        return self._section_from_node(rec["s"])

    def sections_with_multiple_active_blocks(self, tenant_id: str) -> list[str]:
        # Custody invariant (Section 7.3): count this tenant's CONTAINS_BLOCK
        # targets that read as active (null status is active for legacy blocks),
        # grouped by section; any section with more than one is a violation.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {tenant_id:$tid})-[:CONTAINS_BLOCK]->(b:ContentBlock) "
                "WHERE coalesce(b.status, 'active') = 'active' "
                "WITH s, count(b) AS active_blocks "
                "WHERE active_blocks > 1 "
                "RETURN s.id AS sid",
                tid=tenant_id,
            )
            return [r["sid"] for r in recs]

    def active_blocks_without_body(self, tenant_id: str) -> list[str]:
        # Null and empty-string both count: a legacy blob-only block has no
        # body property at all, and `_block_from_node` coerces that to "".
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (b:ContentBlock {tenant_id:$tid}) "
                "WHERE coalesce(b.status, 'active') = 'active' "
                "AND coalesce(b.body, '') = '' "
                "RETURN b.id AS bid",
                tid=tenant_id,
            )
            return [r["bid"] for r in recs]

    def supersede_block(
        self, old_block_id: str, new_block: ContentBlock, section_id: str,
        transaction_id: str | None = None,
    ) -> None:
        # Validate mutability up front (reads), then perform every write in a
        # single transaction: a crash mid-way must not leave the section with
        # two ACTIVE blocks (the custody invariant the publish gate checks).
        sec = self.get_section(section_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach block"
            )
        new_block.status = BlockStatus.ACTIVE

        def _tx(tx):
            self._run_upsert_content_block(tx, new_block)
            tx.run(
                "MATCH (s:Section {id:$sid}), (b:ContentBlock {id:$bid}) "
                "MERGE (s)-[:CONTAINS_BLOCK]->(b)",
                sid=section_id, bid=new_block.id,
            )
            # Mark the predecessor superseded; it stays attached for history.
            tx.run(
                "MATCH (b:ContentBlock {id:$bid}) SET b.status=$status",
                bid=old_block_id, status=BlockStatus.SUPERSEDED.value,
            )
            tx.run(
                "MATCH (new:ContentBlock {id:$nid}), (old:ContentBlock {id:$oid}) "
                "MERGE (new)-[:SUPERSEDES]->(old)",
                nid=new_block.id, oid=old_block_id,
            )
            if transaction_id is not None:
                tx.run(
                    "MATCH (new:ContentBlock {id:$nid}), (t:Transaction {id:$tid}) "
                    "MERGE (new)-[:DERIVED_FROM]->(t)",
                    nid=new_block.id, tid=transaction_id,
                )

        with self._driver.session() as session:
            session.execute_write(_tx)

    def _block_from_node(self, n) -> ContentBlock:
        return ContentBlock(
            id=n["id"], content_ref=n["content_ref"], kind=SectionKind(n["kind"]),
            tenant_id=n["tenant_id"], source_ref=n.get("source_ref", ""),
            # A block written before bodies moved onto the node has its text
            # only in the blob store and reads as empty here. Coerced rather
            # than raised so the console and the backfill script can still open
            # the graph; the publish gate is what refuses to ship one, and
            # scripts/migrate_block_bodies.py is what fixes it.
            body=n.get("body") or "",
            status=BlockStatus(n.get("status", "active")),
            created_at=n["created_at"].to_native() if n.get("created_at") else _now(),
        )

    def upsert_content_block(self, block: ContentBlock) -> None:
        with self._driver.session() as session:
            self._run_upsert_content_block(session, block)

    @staticmethod
    def _run_upsert_content_block(runner, block: ContentBlock) -> None:
        # `runner` is a session or an open transaction; both expose .run.
        runner.run(
            "MERGE (b:ContentBlock {id:$id}) "
            "SET b:Entity, b.content_ref=$cref, b.kind=$kind, b.tenant_id=$tid, "
            "b.source_ref=$src, b.body=$body, b.status=$status, "
            "b.created_at = coalesce(b.created_at, $created_at)",
            id=block.id, cref=block.content_ref, kind=block.kind.value,
            tid=block.tenant_id, src=block.source_ref, body=block.body,
            status=(block.status or BlockStatus.ACTIVE).value,
            created_at=block.created_at,
        )

    def upsert_artefact(self, artefact: Artefact, skill_id: str, path: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (a:Artefact {id:$id}) "
                "SET a:Entity, a.content_ref=$cref, a.kind=$kind, a.name=$name, a.size=$size, "
                "a.tenant_id=$tid, a.source_ref=$src, "
                "a.created_at = coalesce(a.created_at, $created_at) "
                "WITH a "
                "MATCH (sk:Skill {id:$sid}) "
                "OPTIONAL MATCH (sk)-[old:HAS_ARTEFACT {path:$path}]->(:Artefact) "
                "DELETE old "
                "WITH sk, a "
                "MERGE (sk)-[r:HAS_ARTEFACT {path:$path}]->(a) "
                "SET r.path=$path",
                id=artefact.id, cref=artefact.content_ref, kind=artefact.kind.value,
                name=artefact.name, size=artefact.size, tid=artefact.tenant_id,
                src=artefact.source_ref, created_at=artefact.created_at,
                sid=skill_id, path=path,
            )

    def artefacts_for_skill(self, skill_id: str) -> list[tuple[Artefact, str]]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (sk:Skill {id:$sid})-[r:HAS_ARTEFACT]->(a:Artefact) "
                "RETURN a, r.path AS path",
                sid=skill_id,
            )
            return [(self._artefact_from_node(rec["a"]), rec["path"]) for rec in recs]

    def _artefact_from_node(self, n) -> Artefact:
        return Artefact(
            id=n["id"], content_ref=n["content_ref"], kind=ArtefactKind(n["kind"]),
            name=n["name"], size=n["size"], tenant_id=n["tenant_id"],
            source_ref=n["source_ref"],
            created_at=n["created_at"].to_native() if n.get("created_at") else _now(),
        )

    def upsert_publication(self, publication: Publication) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (p:Publication {id:$id}) "
                "SET p:Entity, p.skill_id=$skill_id, p.source_ref=$src, p.content_hash=$hash, "
                "p.published_at=$pa, p.tenant_id=$tid",
                id=publication.id, skill_id=publication.skill_id,
                src=publication.source_ref, hash=publication.content_hash,
                pa=publication.published_at, tid=publication.tenant_id,
            )

    def get_publication(self, tenant_id: str, source_ref: str) -> Publication | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (p:Publication {tenant_id:$tid, source_ref:$src}) RETURN p",
                tid=tenant_id, src=source_ref,
            ).single()
        if rec is None:
            return None
        return self._publication_from_node(rec["p"])

    def publications_for_skill(self, tenant_id: str, skill_id: str) -> list[Publication]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (p:Publication {tenant_id:$tid, skill_id:$sid}) RETURN p",
                tid=tenant_id, sid=skill_id,
            )
            return [self._publication_from_node(r["p"]) for r in recs]

    def delete_publications_for_skill(self, tenant_id: str, skill_id: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (p:Publication {tenant_id:$tid, skill_id:$sid}) DETACH DELETE p",
                tid=tenant_id, sid=skill_id,
            )

    def delete_publication(self, tenant_id: str, source_ref: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (p:Publication {tenant_id:$tid, source_ref:$src}) DETACH DELETE p",
                tid=tenant_id, src=source_ref,
            )

    def append_skill_version(self, version: SkillVersion) -> None:
        # A version is a node attached to its skill, not merged: every capture
        # is a new row, never an update to a previous one (history is
        # append-only). The node is created unconditionally and the
        # HAS_VERSION edge attached only when the skill still exists: the
        # contract captures a version by tenant/skill id alone (skill_versions,
        # get_skill_version) without ever traversing the edge, so a MATCH that
        # gated the CREATE on the skill being present would silently drop the
        # row for a skill row that has not been upserted yet - the edge is
        # only what the delete cascade follows.
        with self._driver.session() as session:
            session.run(
                "CREATE (v:SkillVersion {"
                "id: $id, skill_id: $skill_id, tenant_id: $tenant_id, at: $at, "
                "revision: $revision, cause: $cause, actor_person_id: $actor, "
                "detail: $detail, group_id: $group_id, "
                "parts_json: $parts_json, metadata_json: $metadata_json, "
                "rules_json: $rules_json}) "
                "WITH v "
                "OPTIONAL MATCH (s:Skill {id: $skill_id}) "
                "FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | "
                "  MERGE (s)-[:HAS_VERSION]->(v))",
                id=version.id, skill_id=version.skill_id, tenant_id=version.tenant_id,
                at=version.at, revision=version.revision, cause=version.cause.value,
                actor=version.actor_person_id, detail=version.detail,
                group_id=version.group_id, parts_json=version.parts_json,
                metadata_json=version.metadata_json, rules_json=version.rules_json,
            )

    def skill_versions(self, tenant_id: str, skill_id: str, limit: int = 50,
                       before: datetime | None = None) -> list[SkillVersion]:
        # `before` is normalised to a plain UTC offset before it goes on the
        # wire, the same as `at` is normalised on the way back in
        # `_skill_version_from_node`: a datetime handed back from an earlier
        # `skill_versions` call round-trips with `pytz.UTC` as its tzinfo (the
        # driver's own `DateTime.to_native()`), and the driver packs that as a
        # zoned-by-id datetime rather than a fixed-offset one. Compared
        # against the fixed-offset `at` already stored on the node, `<`
        # silently answers wrong instead of raising - every row reads as
        # "before" a boundary that should have excluded it.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (v:SkillVersion {tenant_id: $tenant_id, skill_id: $skill_id}) "
                "WHERE $before IS NULL OR v.at < $before "
                "RETURN v ORDER BY v.at DESC LIMIT $limit",
                tenant_id=tenant_id, skill_id=skill_id, limit=limit,
                before=None if before is None else before.astimezone(timezone.utc),
            )
            return [self._skill_version_from_node(r["v"]) for r in recs]

    def get_skill_version(self, version_id: str) -> SkillVersion | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (v:SkillVersion {id:$id}) RETURN v", id=version_id,
            ).single()
        if rec is None:
            return None
        return self._skill_version_from_node(rec["v"])

    def latest_skill_version(self, tenant_id: str, skill_id: str) -> SkillVersion | None:
        rows = self.skill_versions(tenant_id, skill_id, limit=1)
        return rows[0] if rows else None

    def recent_skill_changes(self, tenant_id: str, limit: int = 8) -> list[SkillChange]:
        with self._driver.session() as session:
            rows = session.run(
                "MATCH (v:SkillVersion {tenant_id: $tenant_id}) "
                "MATCH (s:Skill {id: v.skill_id, tenant_id: $tenant_id}) "
                "RETURN v.id AS id, v.skill_id AS skill_id, s.name AS skill_name, "
                "v.at AS at, v.cause AS cause, v.revision AS revision, "
                "v.actor_person_id AS actor_person_id, v.detail AS detail "
                "ORDER BY at DESC, id DESC LIMIT $limit",
                tenant_id=tenant_id, limit=max(0, limit),
            )
            return [SkillChange(id=r["id"], skill_id=r["skill_id"],
                                skill_name=r["skill_name"],
                                at=r["at"].to_native().astimezone(timezone.utc),
                                cause=SkillVersionCause(r["cause"]), revision=r["revision"],
                                actor_person_id=r["actor_person_id"], detail=r["detail"])
                    for r in rows]

    def trim_skill_versions(self, tenant_id: str, skill_id: str, keep: int) -> int:
        # The oldest row (the `created` snapshot) always survives, regardless
        # of `keep`: UNWIND then excludes it from the doomed set even when
        # keep=0 would otherwise take it. With nothing to trim the UNWIND
        # yields no rows at all, so the driver's .single() comes back None
        # rather than a row carrying a zero count.
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (v:SkillVersion {tenant_id: $tenant_id, skill_id: $skill_id}) "
                "WITH v ORDER BY v.at DESC "
                "WITH collect(v) AS rows "
                "WITH rows, rows[-1] AS oldest "
                "UNWIND rows[$keep..] AS doomed "
                "WITH doomed, oldest WHERE doomed <> oldest "
                "DETACH DELETE doomed "
                "RETURN count(doomed) AS removed",
                tenant_id=tenant_id, skill_id=skill_id, keep=keep,
            ).single()
        return int(rec["removed"]) if rec is not None else 0

    @staticmethod
    def _skill_version_from_node(n) -> SkillVersion:
        return SkillVersion(
            id=n["id"], skill_id=n["skill_id"], tenant_id=n["tenant_id"],
            at=n["at"].to_native().astimezone(timezone.utc), revision=n["revision"],
            cause=SkillVersionCause(n["cause"]),
            actor_person_id=n.get("actor_person_id"),
            detail=n.get("detail"), group_id=n.get("group_id"),
            parts_json=n.get("parts_json", "[]"),
            metadata_json=n.get("metadata_json", "{}"),
            rules_json=n.get("rules_json", "[]"),
        )

    def record_usage_event(self, event: UsageEvent) -> None:
        # MERGE on id, so a client that retries a call it already logged does
        # not inflate the count the ranking policy reads.
        with self._driver.session() as session:
            session.run(
                "MERGE (u:UsageEvent {id:$id}) "
                "SET u:Entity, u.skill_id=$sid, u.tenant_id=$tid, u.timestamp=$ts, "
                "u.source_runtime=$runtime, u.principal_id=$pid, u.resource=$resource",
                id=event.id, sid=event.skill_id, tid=event.tenant_id,
                ts=event.timestamp, runtime=event.source_runtime.value,
                pid=event.principal_id, resource=event.resource,
            )

    def usage_events(self, tenant_id: str, skill_id: str | None = None,
                     since: datetime | None = None) -> list[UsageEvent]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (u:UsageEvent {tenant_id:$tid}) "
                "WHERE ($sid IS NULL OR u.skill_id=$sid) "
                "AND ($since IS NULL OR u.timestamp >= $since) "
                "RETURN u ORDER BY u.timestamp",
                tid=tenant_id, sid=skill_id, since=since,
            )
            return [self._usage_event_from_node(r["u"]) for r in recs]

    @staticmethod
    def _usage_event_from_node(n) -> UsageEvent:
        return UsageEvent(
            id=n["id"], skill_id=n["skill_id"], tenant_id=n["tenant_id"],
            timestamp=n["timestamp"].to_native(),
            source_runtime=SourceRuntime(n["source_runtime"]),
            principal_id=n.get("principal_id"), resource=n.get("resource"),
        )

    def set_publish_block(self, tenant_id: str, reasons: list[str], source: str) -> None:
        # One flag per tenant: MERGE on tenant_id so a re-raise replaces it.
        with self._driver.session() as session:
            session.run(
                "MERGE (b:PublishBlock {tenant_id:$tid}) "
                "SET b:Entity, b.reasons=$reasons, b.source=$source, b.set_at=$at",
                tid=tenant_id, reasons=list(reasons), source=source,
                at=datetime.now(timezone.utc),
            )

    def clear_publish_block(self, tenant_id: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (b:PublishBlock {tenant_id:$tid}) DETACH DELETE b", tid=tenant_id,
            )

    def get_publish_block(self, tenant_id: str) -> PublishBlock | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (b:PublishBlock {tenant_id:$tid}) RETURN b", tid=tenant_id,
            ).single()
            if rec is None:
                return None
            b = rec["b"]
            return PublishBlock(
                tenant_id=b["tenant_id"], reasons=list(b["reasons"]),
                source=b["source"], set_at=b["set_at"].to_native(),
            )

    @staticmethod
    def _publication_from_node(n) -> Publication:
        return Publication(
            id=n["id"], skill_id=n["skill_id"], source_ref=n["source_ref"],
            content_hash=n["content_hash"],
            published_at=n["published_at"].to_native(), tenant_id=n["tenant_id"],
        )

    def observe_rule_in(self, rule_id: str, transaction_id: str, source_ref: str) -> None:
        """Add each distinct imported source once, preserving other evidence.

        The workspace lock serialises with manual read/modify/write decisions;
        the rule lock makes the observation's source test and increment atomic.
        Inherited corroboration is not necessarily represented by lineage, so
        rebuilding the total from edges would discard legitimate evidence.
        """
        def _tx(tx):
            tx.run(
                "MATCH (r:Rule {id:$rid}), (t:Transaction {id:$tid}) "
                "WHERE r.tenant_id=t.tenant_id "
                "MERGE (w:WorkflowLock {tenant_id:r.tenant_id}) "
                "SET w.locked=true REMOVE w.locked "
                "WITH r,t "
                "SET r._observation_lock=true REMOVE r._observation_lock "
                "WITH r,t, NOT EXISTS { MATCH (r)-[seen:OBSERVED_IN]->() "
                "WHERE seen.source_ref=$src } AS new_source "
                "MERGE (r)-[o:OBSERVED_IN]->(t) "
                "SET o.last_seen_at=$now, o.source_ref=$src, "
                "r.corroboration_count=coalesce(r.corroboration_count,0) "
                "+ CASE WHEN new_source THEN 1 ELSE 0 END",
                rid=rule_id, tid=transaction_id, src=source_ref,
                now=datetime.now(timezone.utc),
            )

        with self._driver.session() as session:
            session.execute_write(_tx)

    def examples_for_section(self, section_id: str) -> list[Example]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Section {id:$sid})-[:HAS_EXAMPLE]->(e:Example) "
                "RETURN e ORDER BY coalesce(e.order, 2147483647)",
                sid=section_id,
            )
            return [self._example_from_node(r["e"]) for r in recs]

    def upsert_cross_skill_edge(
        self, edge_type: EdgeType, from_rule_id: str, to_rule_id: str,
        confidence: float, kind: str | None = None,
    ) -> None:
        # Edge type can't be parameterised in Cypher; build the statement.
        cypher = (
            f"MATCH (a:Rule {{id:$frm}}), (b:Rule {{id:$to}}) "
            f"MERGE (a)-[r:{edge_type.value}]->(b) "
            f"SET r.confidence=$conf"
        )
        if kind is not None:
            cypher += ", r.kind=$kind"
        with self._driver.session() as session:
            session.run(cypher, frm=from_rule_id, to=to_rule_id, conf=confidence, kind=kind)

    def cross_skill_neighbours(self, rule_id: str) -> list[tuple[EdgeType, str, float]]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (r:Rule {id:$rid})-[rel]->(o:Rule) "
                "WHERE type(rel) IN ['EQUIVALENT_TO', 'RELATES_TO', 'CROSS_SKILL_DIVERGENCE'] "
                "RETURN type(rel) AS et, o.id AS to_id, rel.confidence AS conf",
                rid=rule_id,
            )
            return [(EdgeType(r["et"]), r["to_id"], float(r["conf"] or 0.0)) for r in recs]


def _now() -> datetime:
    return datetime.now(timezone.utc)
