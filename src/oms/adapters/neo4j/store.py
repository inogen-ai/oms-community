import json
import logging
from datetime import datetime, timezone

from neo4j import Driver
from neo4j.graph import Node

from oms.adapters.neo4j import schema
from oms.adapters.neo4j.fulltext import fulltext_terms
from oms.domain.identity import SkillRef
from oms.domain.custody import effective_example_parent
from oms.domain.relationships import EDGE_LABELS
from oms.domain.models import (
    Rule, Skill, Transaction, Edge, Learning, Constraint, Example, Tag,
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
from oms.ports.graph_store import TRANSACTION_CONTEXT_FIELDS, RuleContext, SectionMutabilityError

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
        assignment = ("ON CREATE SET" if create_only else
                      "ON CREATE SET n.tenant_id=$tenant_id WITH n "
                      "WHERE n.tenant_id=$tenant_id SET")
        with self._driver.session() as session:
            row = session.run(
                "MERGE (n:Rule {id: $id}) "
                f"{assignment} n:Entity, n.body=$body, n.tenant_id=$tenant_id, n.status=$status, "
                "n.corroboration_count=$cc, n.reference_only=$ref, n.polarity=$pol, "
                "n.plane=$plane, n.created_at=$created_at "
                "WITH n WHERE n.tenant_id=$tenant_id RETURN n.id AS id",
                id=rule.id, body=rule.body, tenant_id=rule.tenant_id,
                status=rule.status.value, cc=rule.corroboration_count,
                ref=rule.reference_only, pol=rule.polarity.value,
                plane=rule.plane.value,
                created_at=rule.created_at,
            ).single()
            if row is None:
                raise ValueError("Rule ID belongs to another tenant")

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
            row = session.run(
                "MERGE (n:Transaction {id:$id}) ON CREATE SET n.tenant_id=$tenant_id "
                "WITH n WHERE n.tenant_id=$tenant_id "
                "SET n:Entity, n.signal_type=$st, n.source_runtime=$sr, "
                "n.sanitised_payload_ref=$ref, n.timestamp=$ts, n.tenant_id=$tenant_id, "
                "n.source_ref=$source_ref, n.skill_hint=$skill_hint, "
                "n.principal_id=$principal_id, n.source_agent_id=$source_agent_id, "
                "n.summary=$summary, n.held_reason=$held_reason, "
                "n.person_id=$person_id, n.assurance=$assurance, "
                "n.admitted=$admitted, n.signal_confidence=$signal_confidence, "
                "n.repo=$repo, n.scope_reviewed=$scope_reviewed, n.licence_hold=$licence_hold, "
                "n.session_summary=$session_summary, n.project_name=$project_name, "
                "n.reuse_case=$reuse_case, n.learning_evidence=$learning_evidence, "
                "n.workflow_state=$workflow_state, n.workflow_decision=$workflow_decision, "
                "n.workflow_rule_id=$workflow_rule_id, n.workflow_safety_digest=$workflow_safety_digest "
                "RETURN n.id AS id",
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
                session_summary=transaction.session_summary,
                project_name=transaction.project_name,
                reuse_case=transaction.reuse_case,
                learning_evidence=transaction.learning_evidence,
                workflow_state=transaction.workflow_state,
                workflow_decision=transaction.workflow_decision,
                workflow_rule_id=transaction.workflow_rule_id,
                workflow_safety_digest=transaction.workflow_safety_digest,
            ).single()
            if row is None:
                raise ValueError("Transaction ID belongs to another tenant")

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
            # Absent on rows written before session context existed, which
            # reads as "not provided", never as empty context. Read back here
            # because workflow code writes whole rows: a field missed on read
            # would be erased by the next write.
            session_summary=n.get("session_summary"),
            project_name=n.get("project_name"),
            reuse_case=n.get("reuse_case"),
            learning_evidence=n.get("learning_evidence"),
            workflow_state=n.get("workflow_state"),
            workflow_decision=n.get("workflow_decision"),
            workflow_rule_id=n.get("workflow_rule_id"),
            workflow_safety_digest=n.get("workflow_safety_digest"),
        )

    def upsert_skill(self, skill: Skill) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (n:Skill {storage_key:$storage_key}) SET n.id=$id "
                "SET n:Entity, n.name=$name, n.description=$desc, n.domain=$domain, "
                "n.tenant_id=$tenant_id, n.status=$status, n.curated=$curated, "
                "n.origin=$origin, n.repo=$repo, n.publish_enabled=$publish_enabled, "
                "n.import_source_ref=$import_source_ref, n.import_name=$import_name, "
                "n.declared_license=$declared_license, n.document_mode=$document_mode",
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
                declared_license=skill.declared_license, document_mode=skill.document_mode,
             storage_key=SkillRef(skill.tenant_id, skill.id).storage_key, scope_tenant=skill.tenant_id)

    def get_skill(self, skill_id: str, *, tenant_id: str) -> Skill | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (n:Skill {id:$id, tenant_id:$scope_tenant}) RETURN n", id=skill_id, scope_tenant=tenant_id).single()
            if rec is None:
                return None
            return self._skill_from_node(rec["n"])

    def delete_skill(self, skill_id: str, *, tenant_id: str) -> None:
        """Remove this skill's custody while preserving nodes with other owners."""
        def write(tx):
            params = {"skill": skill_id, "tenant": tenant_id}
            tx.run("MATCH (sk:Skill {id:$skill,tenant_id:$tenant})-[:HAS_SECTION]->"
                   "(s:Section {tenant_id:$tenant})-[:CONTAINS_BLOCK]->(b:ContentBlock {tenant_id:$tenant}) "
                   "WHERE NOT EXISTS { MATCH (other:Section)-[:CONTAINS_BLOCK]->(b) "
                   "WHERE other.skill_id <> $skill OR other.tenant_id <> $tenant } "
                   "DETACH DELETE b", **params).consume()
            tx.run("MATCH (sk:Skill {id:$skill,tenant_id:$tenant}) "
                   "OPTIONAL MATCH (sk)-[:HAS_SECTION]->(s:Section {tenant_id:$tenant}) "
                   "WITH sk,collect(s) AS sections WITH sections+[sk] AS owners UNWIND owners AS owner "
                   "MATCH (owner)-[:HAS_EXAMPLE]->(e:Example {tenant_id:$tenant}) "
                   "WHERE NOT EXISTS { MATCH (other)-[:HAS_EXAMPLE]->(e) WHERE NOT other IN owners } "
                   "DETACH DELETE e", **params).consume()
            tx.run("MATCH (sk:Skill {id:$skill,tenant_id:$tenant})-[:HAS_ARTEFACT]->"
                   "(a:Artefact {tenant_id:$tenant}) "
                   "WHERE NOT EXISTS { MATCH (other:Skill)-->(a) WHERE other <> sk } "
                   "DETACH DELETE a", **params).consume()
            tx.run("MATCH (v:SkillVersion {skill_id:$skill,tenant_id:$tenant}) DETACH DELETE v", **params).consume()
            tx.run("MATCH (sk:Skill {id:$skill,tenant_id:$tenant}) "
                   "OPTIONAL MATCH (sk)-[:HAS_SECTION]->(s:Section {tenant_id:$tenant}) "
                   "DETACH DELETE sk,s", **params).consume()
        with self._driver.session() as session:
            session.execute_write(write)

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
                     import_source_ref=n.get("import_source_ref"), import_name=n.get("import_name"),
                     declared_license=n.get("declared_license"), document_mode=n.get("document_mode"))

    # Rank 0 is the node an explorer key names: any core node by its id except
    # tags and file occurrences, which have qualified keys (graph_view.graph_key).
    # Rank 1 accepts the plain ids those two were opened by before.
    _GRAPH_ROOTS = (
        "CALL { MATCH (n {id:$id}) WHERE n.tenant_id=$tenant AND NOT n:Tag AND NOT n:Artefact "
        "AND any(label IN labels(n) WHERE label IN $labels) RETURN n, 0 AS rank "
        "UNION MATCH (n:Tag {id:$tag}) WHERE EXISTS { MATCH (n)--(owner {tenant_id:$tenant}) } RETURN n, 0 AS rank "
        "UNION MATCH (n:Artefact {storage_key:$occurrence}) WHERE n.tenant_id=$tenant RETURN n, 0 AS rank "
        "UNION MATCH (n {id:$id}) WHERE (n:Artefact AND n.tenant_id=$tenant) "
        "OR (n:Tag AND EXISTS { MATCH (n)--(owner {tenant_id:$tenant}) }) RETURN n, 1 AS rank } "
        "RETURN elementId(n) AS element, properties(n) AS props, labels(n) AS kinds, rank ORDER BY rank LIMIT 2")

    @classmethod
    def _graph_root(cls, tx, node_id, tenant_id):
        """The one node an explorer key names, or None when absent or ambiguous."""
        from oms.adapters.graph_view import GRAPH_LABELS, parse_graph_key
        qualified = parse_graph_key(node_id) or ("", ())
        tag = qualified[1][0] if qualified[0] == "Tag" else None
        occurrence = json.dumps([tenant_id, *qualified[1]], ensure_ascii=False, separators=(",", ":")) \
            if qualified[0] == "Artefact" else None
        rows = list(tx.run(cls._GRAPH_ROOTS, id=node_id, tag=tag, occurrence=occurrence,
                           tenant=tenant_id, labels=list(GRAPH_LABELS)))
        if not rows or (len(rows) > 1 and rows[1]["rank"] == rows[0]["rank"]):
            return None
        return rows[0]

    @staticmethod
    def _graph_record(row):
        from oms.adapters.graph_view import GRAPH_LABELS, graph_record
        return graph_record(row["props"], next(k for k in GRAPH_LABELS if k in row["kinds"]))

    def graph_node(self, node_id, tenant_id):
        with self._driver.session() as session:
            root = session.execute_read(self._graph_root, node_id, tenant_id)
            return None if root is None else self._graph_record(root)

    def graph_neighbours(self, node_id, tenant_id, *, offset=0, limit=100):
        from oms.adapters.graph_view import GRAPH_LABELS, graph_key
        match = ("MATCH (n)-[r]-(other) WHERE elementId(n)=$element "
            "AND any(label IN labels(other) WHERE label IN $labels) "
            "AND (other.tenant_id=$tenant OR (other:Tag AND other.tenant_id IS NULL)) ")
        with self._driver.session() as session:
            # Count and page within one read transaction so the cursor describes
            # one consistent neighbourhood, independent of graph search limits.
            # Relationships are read between nodes, not ids, so nodes sharing
            # an id never lend each other their neighbours.
            def read(tx):
                root = self._graph_root(tx, node_id, tenant_id)
                if root is None:
                    return {"nodes": [], "edges": [], "total": 0, "next_offset": None}
                anchor = self._graph_record(root)["id"]
                params = {"element": root["element"], "tenant": tenant_id, "labels": list(GRAPH_LABELS)}
                # Pages follow the explorer key, as the memory adapter's do; the key's
                # escaping is computed here, so only the chosen page reads its properties.
                keys = list(tx.run(match + "RETURN DISTINCT elementId(other) AS element, "
                    "other {.id, .owner_skill_id, .occurrence_path} AS props, labels(other) AS kinds", **params))
                def order(row):
                    label = next(k for k in GRAPH_LABELS if k in row["kinds"])
                    return graph_key(row["props"], label), label, row["element"]
                total = len(keys)
                page = [row["element"] for row in sorted(keys, key=order)[offset:offset + limit]]
                found = {row["element"]: row for row in tx.run(match + "AND elementId(other) IN $page "
                    "WITH other, collect(DISTINCT {outgoing:startNode(r)=n, type:type(r)}) AS edges "
                    "RETURN elementId(other) AS element, properties(other) AS props, labels(other) AS kinds, edges",
                    page=page, **params)}
                rows = [found[element] for element in page]
                nodes = [self._graph_record(row) for row in rows]
                return {"nodes": nodes,
                        "edges": [{"source": anchor if edge["outgoing"] else node["id"],
                                   "target": node["id"] if edge["outgoing"] else anchor, "type": edge["type"]}
                                  for node, row in zip(nodes, rows) for edge in row["edges"]],
                        "total": total, "next_offset": offset + limit if offset + limit < total else None}
            return session.execute_read(read)

    def upsert_tag(self, tag_id: str, name: str) -> None:
        with self._driver.session() as session:
            session.run("MERGE (n:Tag {id:$id}) SET n:Entity, n.name=$name", id=tag_id, name=name)

    def tag_name(self, tag_id: str) -> str | None:
        with self._driver.session() as session:
            row = session.run("MATCH (n:Tag {id:$id}) RETURN n.name AS name", id=tag_id).single()
            return row["name"] if row else None

    def attach_edge(self, edge: Edge, *, tenant_id: str) -> None:
        def write(tx):
            endpoints = self._edge_endpoints(tx, edge, tenant_id)
            if endpoints is None:
                raise KeyError("Relationship endpoints are missing or outside the tenant")
            tx.run("MATCH (a),(b) WHERE elementId(a)=$start AND elementId(b)=$end "
                   f"MERGE (a)-[r:{edge.type.value}]->(b) SET r += $props",
                   start=endpoints[0], end=endpoints[1], props=edge.properties).consume()
        with self._driver.session() as session:
            session.execute_write(write)

    @staticmethod
    def _edge_endpoints(tx, edge: Edge, tenant_id: str):
        rows = list(tx.run(
            "MATCH (a:Entity {id:$start}),(b:Entity {id:$end}) "
            "WHERE (a.tenant_id=$tenant OR (a:Tag AND a.tenant_id IS NULL)) "
            "AND (b.tenant_id=$tenant OR (b:Tag AND b.tenant_id IS NULL)) "
            "AND any(pair IN $pairs WHERE pair[0] IN labels(a) AND pair[1] IN labels(b)) "
            "RETURN DISTINCT elementId(a) AS start,elementId(b) AS end",
            start=edge.from_id, end=edge.to_id, tenant=tenant_id,
            pairs=[list(pair) for pair in EDGE_LABELS[edge.type]]))
        if len(rows) > 1:
            raise ValueError("Relationship endpoints are ambiguous")
        return (rows[0]["start"], rows[0]["end"]) if rows else None

    def detach_edge(self, edge: Edge, *, tenant_id: str) -> None:
        def write(tx):
            endpoints = self._edge_endpoints(tx, edge, tenant_id)
            if endpoints is None:
                return
            tx.run("MATCH (a)-[r:" + edge.type.value + "]->(b) "
                   "WHERE elementId(a)=$start AND elementId(b)=$end DELETE r",
                   start=endpoints[0], end=endpoints[1]).consume()
        with self._driver.session() as session:
            session.execute_write(write)

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

    def tag_records_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Tag]:
        with self._driver.session() as session:
            rows = session.run("MATCH (:Skill {id:$id,tenant_id:$tenant})-[:TAGGED_WITH]->(tag:Tag) "
                               "RETURN DISTINCT tag.id AS id, tag.name AS name ORDER BY id",
                               id=skill_id, tenant=tenant_id)
            return [Tag(id=row["id"], name=row["name"]) for row in rows]

    def tags_for_skill(self, skill_id: str, *, tenant_id: str) -> list[str]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Skill {id:$sid, tenant_id:$scope_tenant})-[:TAGGED_WITH]->(t:Tag) "
                "RETURN DISTINCT t.name AS name ORDER BY name",
                sid=skill_id,
             scope_tenant=tenant_id)
            return [r["name"] for r in recs]

    def rules_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Rule]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:Rule {tenant_id:$scope_tenant})-[:BELONGS_TO]->(:Skill {id:$sid, tenant_id:$scope_tenant}) RETURN n ORDER BY n.id",
                sid=skill_id,
             scope_tenant=tenant_id)
            return [self._rule_from_node(r["n"]) for r in recs]

    def skills_for_rule(self, rule_id: str, *, tenant_id: str) -> list[Skill]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Rule {id:$rid, tenant_id:$scope_tenant})-[:BELONGS_TO]->(s:Skill {tenant_id:$scope_tenant}) "
                "RETURN DISTINCT s ORDER BY s.id",
                rid=rule_id,
             scope_tenant=tenant_id)
            return [self._skill_from_node(r["s"]) for r in recs]

    def skills_by_rule(self, rule_ids: list[str], *, tenant_id: str) -> dict[str, list[Skill]]:
        if not rule_ids:
            return {}
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (r:Rule {tenant_id:$scope_tenant})-[:BELONGS_TO]->(s:Skill {tenant_id:$scope_tenant}) WHERE r.id IN $ids "
                "RETURN r.id AS rid, s ORDER BY r.id, s.id",
                ids=list(rule_ids),
             scope_tenant=tenant_id)
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

    def rule_context(self, rule_ids: list[str], *, tenant_id: str) -> dict[str, RuleContext]:
        if not rule_ids:
            return {}
        with self._driver.session() as session:
            recs = session.run(
                # One query for the whole page. OPTIONAL on both legs so a rule
                # with no skill, or none whose lineage names a person, still
                # comes back - absent means "no such rule", which is a
                # different answer the caller is entitled to.
                "MATCH (r:Rule {tenant_id:$scope_tenant}) WHERE r.id IN $ids "
                "OPTIONAL MATCH (r)-[:BELONGS_TO]->(s:Skill {tenant_id:$scope_tenant}) "
                "WITH r, collect(DISTINCT s.name) AS names, "
                "     collect(DISTINCT s.domain) AS domains "
                "OPTIONAL MATCH (r)-[:DERIVED_FROM]->(t:Transaction {tenant_id:$scope_tenant}) "
                "WHERE t.person_id IS NOT NULL "
                # The correction it was BORN from, not the latest one to
                # corroborate it: oldest wins.
                "WITH r, names, domains, t ORDER BY t.timestamp "
                "WITH r, names, domains, collect(t.person_id)[0] AS contributor "
                "RETURN r.id AS rid, names, domains, contributor",
                ids=list(rule_ids),
             scope_tenant=tenant_id)
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

    def clear_transaction_context(self, transaction_ids: list[str], tenant_id: str) -> int:
        wanted = sorted(set(transaction_ids))
        if not wanted:
            return 0
        # Setting a property to null removes it, which is how a row written
        # before the context existed already reads: "not provided". The
        # property names come from the constant, never from a caller. Counted
        # before the SET, so a row that carried none of them is not counted.
        carried = " OR ".join(f"t.{name} IS NOT NULL" for name in TRANSACTION_CONTEXT_FIELDS)
        cleared = ", ".join(f"t.{name} = null" for name in TRANSACTION_CONTEXT_FIELDS)
        with self._driver.session() as session:
            record = session.run(
                "MATCH (t:Transaction) WHERE t.id IN $ids AND t.tenant_id = $tid "
                f"WITH t, ({carried}) AS carried SET {cleared} "
                "RETURN count(CASE WHEN carried THEN 1 END) AS cleared",
                ids=wanted, tid=tenant_id,
            ).single()
        return record["cleared"] if record is not None else 0

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
                 "AND ($skill IS NULL OR (n)-[:BELONGS_TO]->(:Skill {id: $skill, tenant_id:$scope_tenant}))")
        params = {"tid": tenant_id,
                  "status": None if status is None else status.value,
                  "q": (query or None), "skill": skill_id,
                  "limit": limit, "offset": offset}
        with self._driver.session() as session:
            total = session.run(
                f"MATCH (n:Rule {{tenant_id:$scope_tenant}}) WHERE {where} RETURN count(n) AS n", **params
            , scope_tenant=tenant_id).single()["n"]
            recs = session.run(
                # The id is the final sort key and it is load-bearing: 5,393 of
                # acme's rules were written by one bulk import and share a
                # created_at to the second, so created_at alone is not a total
                # order and a page boundary would repeat or skip rows.
                f"MATCH (n:Rule {{tenant_id:$scope_tenant}}) WHERE {where} "
                "RETURN n ORDER BY n.created_at DESC, n.id "
                "SKIP $offset LIMIT $limit",
                **params,
             scope_tenant=tenant_id)
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

    def upsert_example(self, example: Example, *, tenant_id: str) -> None:
        if example.tenant_id != tenant_id:
            raise ValueError("Example tenant differs from its owner")
        parents = [effective_example_parent(example)]
        key = SkillRef(tenant_id, example.id).storage_key
        def write(tx):
            if parents:
                label, parent_id = parents[0]
                self._require_node(tx, label, parent_id, tenant_id)
            tx.run("MERGE (e:Example {storage_key:$key}) "
                   "SET e:Entity,e.id=$id,e.tenant_id=$tenant,e.body=$body,e.kind=$kind,"
                   "e.parent_section_id=$section,e.parent_rule_id=$rule,e.parent_skill_id=$skill,"
                   "e.name=$name,e.original_label=$original,e.source_ref=$source,e.order=$order,"
                   "e.created_at=coalesce(e.created_at,$created) "
                   "WITH e OPTIONAL MATCH (e)-[old:HAS_EXAMPLE|ILLUSTRATES]-() DELETE old",
                   key=key,id=example.id,tenant=tenant_id,body=example.body,kind=example.kind.value,
                   section=example.parent_section_id,rule=example.parent_rule_id,skill=example.parent_skill_id,
                   name=example.name,original=example.original_label,source=example.source_ref,
                   order=example.order,created=example.created_at).consume()
            if parents:
                tx.run(f"MATCH (p:{label} {{id:$parent,tenant_id:$tenant}}),"
                       "(e:Example {storage_key:$key}) MERGE (p)-[:HAS_EXAMPLE]->(e) "
                       + ("MERGE (e)-[:ILLUSTRATES]->(p)" if label != "Section" else ""),
                       parent=parent_id,tenant=tenant_id,key=key).consume()
        with self._driver.session() as session:
            session.execute_write(write)

    @staticmethod
    def _require_node(tx, label: str, identifier: str, tenant_id: str) -> None:
        row = tx.run(f"MATCH (n:{label} {{id:$id,tenant_id:$tenant}}) RETURN count(n) AS n",
                     id=identifier,tenant=tenant_id).single()
        if row["n"] != 1:
            raise KeyError("Owned endpoint is absent or ambiguous")

    def attach_example(self, example: Example, *, tenant_id: str) -> None:
        self.upsert_example(example, tenant_id=tenant_id)

    def get_example(self, example_id: str, *, tenant_id: str) -> Example | None:
        with self._driver.session() as session:
            row = session.run("MATCH (e:Example {id:$id,tenant_id:$tenant}) RETURN e", id=example_id, tenant=tenant_id).single()
            return self._example_from_node(row["e"]) if row else None

    def examples_for_rule(self, rule_id: str, *, tenant_id: str) -> list[Example]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (e:Example {tenant_id:$scope_tenant})-[:ILLUSTRATES]->(:Rule {id:$rid, tenant_id:$scope_tenant}) RETURN e ORDER BY e.order IS NULL,e.order,e.id", rid=rule_id
            , scope_tenant=tenant_id)
            return [self._example_from_node(r["e"]) for r in recs]

    def examples_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Example]:
        # Examples are stored once, under their section; skill-level retrieval
        # traverses section membership. Directly attached examples (ILLUSTRATES)
        # are kept for rows written before single-parent storage.
        with self._driver.session() as session:
            recs = session.run(
                "CALL { MATCH (e:Example {tenant_id:$scope_tenant})-[:ILLUSTRATES]->(:Skill {id:$sid, tenant_id:$scope_tenant}) RETURN e "
                "UNION "
                "MATCH (:Skill {id:$sid, tenant_id:$scope_tenant})-[:HAS_SECTION]->(:Section {tenant_id:$scope_tenant})-[:HAS_EXAMPLE]->(e:Example {tenant_id:$scope_tenant}) "
                "RETURN e } RETURN e ORDER BY e.order IS NULL,e.order,e.id",
                sid=skill_id,
             scope_tenant=tenant_id)
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
            created_at=n["created_at"].to_native() if n.get("created_at") else _now(),
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
        def write(tx):
            self._require_node(tx, "Skill", section.skill_id, section.tenant_id)
            owner = tx.run("MATCH (s:Section {storage_key:$key}) RETURN s.skill_id AS owner",
                           key=SkillRef(section.tenant_id, section.id).storage_key).single()
            if owner is not None and owner["owner"] != section.skill_id:
                raise ValueError("Section identity belongs to another skill")
            tx.run(
                "MERGE (s:Section {storage_key:$storage_key}) SET s.id=$id "
                "SET s:Entity, s.skill_id=$skill_id, s.kind=$kind, s.heading=$heading, "
                "s.order=$order, s.mutability=$mut, s.tenant_id=$tid, "
                "s.created_at = coalesce(s.created_at, $created_at) "
                "WITH s "
                "MATCH (sk:Skill {id:$skill_id, tenant_id:$scope_tenant}) "
                "MERGE (sk)-[r:HAS_SECTION]->(s) "
                "SET r.order=$order",
                id=section.id, skill_id=section.skill_id, kind=section.kind.value,
                heading=section.heading, order=section.order,
                mut=section.mutability.value, tid=section.tenant_id,
                created_at=section.created_at,
             storage_key=SkillRef(section.tenant_id, section.id).storage_key, scope_tenant=section.tenant_id)
        with self._driver.session() as session:
            session.execute_write(write)

    def get_section(self, section_id: str, *, tenant_id: str) -> Section | None:
        with self._driver.session() as session:
            rec = session.run("MATCH (s:Section {id:$id, tenant_id:$scope_tenant}) RETURN s", id=section_id, scope_tenant=tenant_id).single()
        if rec is None:
            return None
        return self._section_from_node(rec["s"])

    def sections_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Section]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (sk:Skill {id:$sid, tenant_id:$scope_tenant})-[:HAS_SECTION]->(s:Section {tenant_id:$scope_tenant}) "
                "RETURN s ORDER BY s.order, s.id",
                sid=skill_id,
             scope_tenant=tenant_id)
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
                    order: int | None = None, group: str | None = None, *, tenant_id: str) -> None:
        if rule.tenant_id != tenant_id:
            raise ValueError("Rule tenant differs from section owner")
        sec = self.get_section(section_id, tenant_id=tenant_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.SYSTEM_AGGREGATED:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach rule"
            )
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant}), (r:Rule {id:$rid, tenant_id:$scope_tenant}) "
                "MERGE (s)-[rel:CONTAINS_RULE]->(r) "
                "SET rel.order = $order, rel.group = $group",
                sid=section_id, rid=rule.id, order=order, group=group,
             scope_tenant=tenant_id)

    def detach_rule(self, rule_id: str, section_id: str, *, tenant_id: str) -> None:
        with self._driver.session() as session:
            session.run("MATCH (:Section {id:$section,tenant_id:$tenant})-[r:CONTAINS_RULE]->(:Rule {id:$child,tenant_id:$tenant}) DELETE r", section=section_id, child=rule_id, tenant=tenant_id).consume()

    def detach_block(self, block_id: str, section_id: str, *, tenant_id: str) -> None:
        with self._driver.session() as session:
            session.run("MATCH (:Section {id:$section,tenant_id:$tenant})-[r:CONTAINS_BLOCK]->(:ContentBlock {id:$child,tenant_id:$tenant}) DELETE r", section=section_id, child=block_id, tenant=tenant_id).consume()

    def remove_example(self, example_id: str, *, tenant_id: str) -> None:
        with self._driver.session() as session:
            row = session.run("MATCH (e:Example {id:$id,tenant_id:$tenant}) OPTIONAL MATCH (p)-[:HAS_EXAMPLE]->(e) RETURN count(p) AS parents", id=example_id, tenant=tenant_id).single()
            if row and row["parents"] > 1:
                raise ValueError("Example has ambiguous owners")
            session.run("MATCH (e:Example {id:$id,tenant_id:$tenant}) DETACH DELETE e", id=example_id, tenant=tenant_id).consume()

    def remove_section(self, section_id: str, *, tenant_id: str) -> None:
        with self._driver.session() as session:
            row = session.run("MATCH (s:Section {id:$id,tenant_id:$tenant}) OPTIONAL MATCH (s)-[r]->() RETURN count(r) AS children", id=section_id, tenant=tenant_id).single()
            if row and row["children"]:
                raise ValueError("Section still owns content")
            session.run("MATCH (s:Section {id:$id,tenant_id:$tenant}) DETACH DELETE s", id=section_id, tenant=tenant_id).consume()

    def inherit_placements(self, retired_rule_id: str, successor: Rule, *, tenant_id: str) -> None:
        if successor.tenant_id != tenant_id:
            raise ValueError("Successor tenant differs from placement owner")
        # Copy every CONTAINS_RULE membership of the retired rule (with its order
        # and group) onto the successor, so a superseding rule renders in the
        # retired rule's place. CONTAINS_RULE only lives on system-aggregated
        # sections, so no mutability guard is needed.
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section {tenant_id:$scope_tenant})-[old:CONTAINS_RULE]->(:Rule {id:$rid, tenant_id:$scope_tenant}) "
                "MATCH (succ:Rule {id:$sid, tenant_id:$scope_tenant}) "
                "MERGE (s)-[new:CONTAINS_RULE]->(succ) "
                "SET new.order = old.order, new.group = old.group",
                rid=retired_rule_id, sid=successor.id,
             scope_tenant=tenant_id)

    def attach_block(self, block: ContentBlock, section_id: str, *, tenant_id: str) -> None:
        if block.tenant_id != tenant_id:
            raise ValueError("Block tenant differs from section owner")
        sec = self.get_section(section_id, tenant_id=tenant_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach block"
            )
        with self._driver.session() as session:
            session.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant}), (b:ContentBlock {id:$bid, tenant_id:$scope_tenant}) "
                "MERGE (s)-[:CONTAINS_BLOCK]->(b)",
                sid=section_id, bid=block.id,
             scope_tenant=tenant_id)

    def rules_for_section(self, section_id: str, *, tenant_id: str) -> list[Rule]:
        # Authorially placed rules first, in source order; unplaced (learned)
        # rules after, by corroboration.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant})-[rel:CONTAINS_RULE]->(r:Rule {tenant_id:$scope_tenant}) "
                "RETURN r ORDER BY rel.order IS NULL, "
                "CASE WHEN rel.order IS NULL THEN -r.corroboration_count ELSE rel.order END, r.id",
                sid=section_id,
             scope_tenant=tenant_id)
            return [self._rule_from_node(r["r"]) for r in recs]

    def rule_placements_for_section(self, section_id: str, *, tenant_id: str) -> list[RulePlacement]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant})-[rel:CONTAINS_RULE]->(r:Rule {tenant_id:$scope_tenant}) "
                "RETURN r.id AS rid, rel.order AS ord, rel.group AS grp "
                "ORDER BY rel.order IS NULL, rel.order, r.id",
                sid=section_id,
             scope_tenant=tenant_id)
            return [RulePlacement(rule_id=r["rid"], order=r["ord"], group=r["grp"]) for r in recs]

    def blocks_for_section(self, section_id: str, *, tenant_id: str, include_inactive: bool = False) -> list[ContentBlock]:
        # Active blocks only; blocks predating the status property (null) read as
        # active for backward compatibility.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant})-[:CONTAINS_BLOCK]->(b:ContentBlock {tenant_id:$scope_tenant}) "
                "WHERE $include_inactive OR coalesce(b.status, 'active') = 'active' RETURN b ORDER BY b.id",
                sid=section_id, include_inactive=include_inactive,
             scope_tenant=tenant_id)
            return [self._block_from_node(r["b"]) for r in recs]

    def blocks_revised_for_rule(self, rule_id: str, *, tenant_id: str) -> list[ContentBlock]:
        # Rule and block share a transaction: the rule DERIVED_FROM it when it
        # was created or corroborated, the block DERIVED_FROM it when a
        # revision rewrote it for that rule. Active blocks only, with the
        # same null-as-active reading as `blocks_for_section`.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (r:Rule {id:$rid, tenant_id:$scope_tenant})-[:DERIVED_FROM]->(t:Transaction {tenant_id:$scope_tenant})"
                "<-[:DERIVED_FROM]-(b:ContentBlock {tenant_id:$scope_tenant}) "
                "WHERE coalesce(b.status, 'active') = 'active' "
                "RETURN DISTINCT b ORDER BY b.id",
                rid=rule_id,
             scope_tenant=tenant_id)
            return [self._block_from_node(r["b"]) for r in recs]

    def get_content_block(self, block_id: str, *, tenant_id: str) -> ContentBlock | None:
        # No status filter, deliberately: a review item can outlive the block it
        # names, and the reviewer needs the text the verdict was about even once
        # a later revision has superseded it.
        with self._driver.session() as session:
            rec = session.run("MATCH (b:ContentBlock {id:$id, tenant_id:$scope_tenant}) RETURN b",
                              id=block_id, scope_tenant=tenant_id).single()
        if rec is None:
            return None
        return self._block_from_node(rec["b"])

    def content_owners(self, rule_ids: list[str], block_ids: list[str], *,
                       tenant_id: str) -> dict[tuple[str, str], frozenset[str]]:
        owners = {("rule", rid): set() for rid in rule_ids} | {("block", bid): set() for bid in block_ids}
        with self._driver.session() as session:
            for row in session.run(
                    "UNWIND $rules AS rid MATCH (r:Rule {id:rid, tenant_id:$tenant}) "
                    "OPTIONAL MATCH (r)-[:BELONGS_TO]->(s:Skill {tenant_id:$tenant}) "
                    "OPTIONAL MATCH (p:Skill {tenant_id:$tenant})-[:HAS_SECTION]->"
                    "(:Section {tenant_id:$tenant})-[:CONTAINS_RULE]->(r) "
                    "RETURN rid, collect(DISTINCT s.id) + collect(DISTINCT p.id) AS skills",
                    rules=list(rule_ids), tenant=tenant_id):
                owners["rule", row["rid"]].update(row["skills"])
            for row in session.run(
                    "UNWIND $blocks AS bid MATCH (p:Skill {tenant_id:$tenant})-[:HAS_SECTION]->"
                    "(:Section {tenant_id:$tenant})-[:CONTAINS_BLOCK]->(:ContentBlock {id:bid, tenant_id:$tenant}) "
                    "RETURN bid, collect(DISTINCT p.id) AS skills", blocks=list(block_ids), tenant=tenant_id):
                owners["block", row["bid"]].update(row["skills"])
        return {entity: frozenset(skills) for entity, skills in owners.items()}

    def section_for_block(self, block_id: str, *, tenant_id: str) -> Section | None:
        # The CONTAINS_BLOCK edge run backwards. A superseded block stays
        # attached for history, so this resolves for those too.
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (s:Section {tenant_id:$scope_tenant})-[:CONTAINS_BLOCK]->(:ContentBlock {id:$id, tenant_id:$scope_tenant}) "
                "RETURN s ORDER BY s.id LIMIT 1",
                id=block_id,
             scope_tenant=tenant_id).single()
        if rec is None:
            return None
        return self._section_from_node(rec["s"])

    def sections_with_multiple_active_blocks(self, tenant_id: str) -> list[str]:
        # Custody invariant (Section 7.3): count this tenant's CONTAINS_BLOCK
        # targets that read as active (null status is active for legacy blocks),
        # grouped by section; any section with more than one is a violation.
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (s:Section {tenant_id:$tid})-[:CONTAINS_BLOCK]->(b:ContentBlock {tenant_id:$scope_tenant}) "
                "WHERE coalesce(b.status, 'active') = 'active' "
                "WITH s, count(b) AS active_blocks "
                "WHERE active_blocks > 1 "
                "RETURN s.id AS sid",
                tid=tenant_id,
             scope_tenant=tenant_id)
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
             scope_tenant=tenant_id)
            return [r["bid"] for r in recs]

    def supersede_block(
        self, old_block_id: str, new_block: ContentBlock, section_id: str,
        transaction_id: str | None = None, *, tenant_id: str) -> None:
        # Validate mutability up front (reads), then perform every write in a
        # single transaction: a crash mid-way must not leave the section with
        # two ACTIVE blocks (the custody invariant the publish gate checks).
        sec = self.get_section(section_id, tenant_id=tenant_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach block"
            )
        if new_block.tenant_id != tenant_id:
            raise ValueError("Replacement block tenant differs from section owner")
        new_block.status = BlockStatus.ACTIVE

        def _tx(tx):
            attached = tx.run("MATCH (:Section {id:$section,tenant_id:$tenant})-[:CONTAINS_BLOCK]->"
                              "(b:ContentBlock {id:$block,tenant_id:$tenant}) RETURN b.id AS id",
                              section=section_id,block=old_block_id,tenant=tenant_id).single()
            if attached is None:
                raise KeyError("Predecessor block is not attached to this section")
            if transaction_id is not None:
                self._require_node(tx, "Transaction", transaction_id, tenant_id)
            self._run_upsert_content_block(tx, new_block)
            tx.run(
                "MATCH (s:Section {id:$sid, tenant_id:$scope_tenant}), (b:ContentBlock {id:$bid, tenant_id:$scope_tenant}) "
                "MERGE (s)-[:CONTAINS_BLOCK]->(b)",
                sid=section_id, bid=new_block.id,
             scope_tenant=tenant_id)
            # Mark the predecessor superseded; it stays attached for history.
            tx.run(
                "MATCH (b:ContentBlock {id:$bid, tenant_id:$scope_tenant}) SET b.status=$status",
                bid=old_block_id, status=BlockStatus.SUPERSEDED.value,
             scope_tenant=tenant_id)
            tx.run(
                "MATCH (new:ContentBlock {id:$nid, tenant_id:$scope_tenant}), (old:ContentBlock {id:$oid, tenant_id:$scope_tenant}) "
                "MERGE (new)-[:SUPERSEDES]->(old)",
                nid=new_block.id, oid=old_block_id,
             scope_tenant=tenant_id)
            if transaction_id is not None:
                tx.run(
                    "MATCH (new:ContentBlock {id:$nid, tenant_id:$scope_tenant}), (t:Transaction {id:$tid, tenant_id:$scope_tenant}) "
                    "MERGE (new)-[:DERIVED_FROM]->(t)",
                    nid=new_block.id, tid=transaction_id,
                 scope_tenant=tenant_id)

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
            "MERGE (b:ContentBlock {storage_key:$storage_key}) SET b.id=$id "
            "SET b:Entity, b.content_ref=$cref, b.kind=$kind, b.tenant_id=$tid, "
            "b.source_ref=$src, b.body=$body, b.status=$status, "
            "b.created_at = coalesce(b.created_at, $created_at)",
            id=block.id, cref=block.content_ref, kind=block.kind.value,
            tid=block.tenant_id, src=block.source_ref, body=block.body,
            status=(block.status or BlockStatus.ACTIVE).value,
            created_at=block.created_at,
         storage_key=SkillRef(block.tenant_id, block.id).storage_key, scope_tenant=block.tenant_id)

    def upsert_artefact(self, artefact: Artefact, skill_id: str, path: str, *, tenant_id: str) -> None:
        if artefact.tenant_id != tenant_id:
            raise ValueError("Artefact tenant differs from its owner")
        key = json.dumps([tenant_id,skill_id,path], ensure_ascii=False, separators=(",", ":"))
        def write(tx):
            self._require_node(tx, "Skill", skill_id, tenant_id)
            tx.run("MATCH (s:Skill {id:$skill,tenant_id:$tenant}) "
                   "MERGE (a:Artefact:ArtefactOccurrence {storage_key:$key}) "
                   "SET a:Entity,a.id=$id,a.content_ref=$ref,a.kind=$kind,a.name=$name,a.size=$size,"
                   "a.tenant_id=$tenant,a.source_ref=$source,a.owner_skill_id=$skill,a.occurrence_path=$path,"
                   "a.created_at=coalesce(a.created_at,$created),a.mode=$mode "
                   "WITH s,a OPTIONAL MATCH (s)-[old:HAS_ARTEFACT {path:$path}]->(other) "
                   "WHERE other <> a DELETE old WITH DISTINCT s,a "
                   "MERGE (s)-[r:HAS_ARTEFACT {path:$path}]->(a)",
                   skill=skill_id,tenant=tenant_id,key=key,id=artefact.id,ref=artefact.content_ref,
                   kind=artefact.kind.value,name=artefact.name,size=artefact.size,source=artefact.source_ref,
                   path=path,created=artefact.created_at,mode=artefact.mode).consume()
        with self._driver.session() as session:
            session.execute_write(write)

    def artefacts_for_skill(self, skill_id: str, *, tenant_id: str) -> list[tuple[Artefact, str]]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (sk:Skill {id:$sid, tenant_id:$scope_tenant})-[r:HAS_ARTEFACT]->(a:Artefact) "
                "RETURN a, r.path AS path ORDER BY path",
                sid=skill_id,
             scope_tenant=tenant_id)
            return [(self._artefact_from_node(rec["a"]), rec["path"]) for rec in recs]

    def _artefact_from_node(self, n) -> Artefact:
        return Artefact(
            id=n["id"], content_ref=n["content_ref"], kind=ArtefactKind(n["kind"]),
            name=n["name"], size=n["size"], tenant_id=n["tenant_id"],
            source_ref=n["source_ref"],
            mode=n.get("mode"),
            created_at=n["created_at"].to_native() if n.get("created_at") else _now(),
        )

    def remove_artefact(self, skill_id: str, path: str, *, tenant_id: str) -> None:
        key = json.dumps([tenant_id, skill_id, path], ensure_ascii=False, separators=(",", ":"))
        with self._driver.session() as session:
            session.run("MATCH (s:Skill {id:$skill,tenant_id:$tenant}) "
                        "MATCH (a:Artefact {storage_key:$key}) "
                        "OPTIONAL MATCH (s)-[r:HAS_ARTEFACT|HAS_REFERENCE]->(a) DELETE r "
                        "WITH DISTINCT a WHERE NOT (a)--() DELETE a",
                        skill=skill_id, tenant=tenant_id, key=key).consume()

    def upsert_publication(self, publication: Publication) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (p:Publication {id:$id}) "
                "SET p:Entity, p.skill_id=$skill_id, p.source_ref=$src, p.content_hash=$hash, "
                "p.published_at=$pa, p.tenant_id=$tid, p.mode=$mode, "
                "p.manifest_json=$manifest,p.policy_version=$policy",
                id=publication.id, skill_id=publication.skill_id,
                src=publication.source_ref, hash=publication.content_hash,
                pa=publication.published_at, tid=publication.tenant_id,
                mode=publication.mode, manifest=publication.manifest_json, policy=publication.policy_version,
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
                "id: $id, storage_key:$storage_key, skill_id: $skill_id, tenant_id: $tenant_id, at: $at, "
                "revision: $revision, cause: $cause, actor_person_id: $actor, "
                "detail: $detail, group_id: $group_id, "
                "parts_json: $parts_json, metadata_json: $metadata_json, "
                "rules_json: $rules_json, files_json:$files_json, source_operation_id:$source_operation, "
                "source_origin_id:$source_origin, source_revision:$source_revision}) "
                "WITH v "
                "OPTIONAL MATCH (s:Skill {id: $skill_id, tenant_id:$scope_tenant}) "
                "FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | "
                "  MERGE (s)-[:HAS_VERSION]->(v))",
                id=version.id, skill_id=version.skill_id, tenant_id=version.tenant_id,
                at=version.at, revision=version.revision, cause=version.cause.value,
                actor=version.actor_person_id, detail=version.detail,
                group_id=version.group_id, parts_json=version.parts_json,
                metadata_json=version.metadata_json, rules_json=version.rules_json,
                files_json=version.files_json, source_operation=version.source_operation_id,
                source_origin=version.source_origin_id, source_revision=version.source_revision,
             storage_key=SkillRef(version.tenant_id,version.id).storage_key, scope_tenant=version.tenant_id)

    def skill_versions(self, tenant_id: str, skill_id: str, limit: int = 50,
                       before: datetime | None = None, *, before_id: str | None = None) -> list[SkillVersion]:
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
                "WHERE $before IS NULL OR v.at < $before OR (v.at = $before AND v.id < $before_id) "
                "RETURN v ORDER BY v.at DESC, v.id DESC LIMIT $limit",
                tenant_id=tenant_id, skill_id=skill_id, limit=limit,
                before=None if before is None else before.astimezone(timezone.utc),
                before_id=before_id,
            )
            return [self._skill_version_from_node(r["v"]) for r in recs]

    def get_skill_version(self, version_id: str, *, tenant_id: str) -> SkillVersion | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (v:SkillVersion {id:$id,tenant_id:$scope_tenant}) RETURN v", id=version_id,
             scope_tenant=tenant_id).single()
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
                "WITH v ORDER BY v.at DESC, v.id DESC "
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
            files_json=n.get("files_json"), source_operation_id=n.get("source_operation_id"),
            source_origin_id=n.get("source_origin_id"), source_revision=n.get("source_revision"),
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
            mode=n.get("mode"), manifest_json=n.get("manifest_json"), policy_version=n.get("policy_version"),
        )

    def observe_rule_in(self, rule_id: str, transaction_id: str, source_ref: str) -> None:
        """Add each distinct imported source once, preserving other evidence.

        The workspace lock serialises with manual read/modify/write decisions;
        the rule lock makes the observation's source test and increment atomic.
        Inherited corroboration is not necessarily represented by lineage, so
        rebuilding the total from edges would discard legitimate evidence.
        """
        def _tx(tx):
            row = tx.run("MATCH (r:Rule {id:$id}) RETURN r.tenant_id AS tenant", id=rule_id).single()
            if row is None:
                raise KeyError(rule_id)
            self._require_node(tx, "Transaction", transaction_id, row["tenant"])
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

    def examples_for_section(self, section_id: str, *, tenant_id: str) -> list[Example]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (:Section {id:$sid, tenant_id:$scope_tenant})-[:HAS_EXAMPLE]->(e:Example {tenant_id:$scope_tenant}) "
                "RETURN e ORDER BY e.order IS NULL,e.order,e.id",
                sid=section_id,
             scope_tenant=tenant_id)
            return [self._example_from_node(r["e"]) for r in recs]

    def upsert_cross_skill_edge(
        self, edge_type: EdgeType, from_rule_id: str, to_rule_id: str,
        confidence: float, kind: str | None = None,
    ) -> None:
        # Edge type can't be parameterised in Cypher; build the statement.
        cypher = (
            f"MATCH (a:Rule {{id:$frm}}), (b:Rule {{id:$to}}) "
            "WHERE a.tenant_id=b.tenant_id "
            f"MERGE (a)-[r:{edge_type.value}]->(b) "
            f"SET r.confidence=$conf"
        )
        if kind is not None:
            cypher += ", r.kind=$kind"
        with self._driver.session() as session:
            row = session.run(cypher + " RETURN a.id AS id", frm=from_rule_id, to=to_rule_id,
                              conf=confidence, kind=kind).single()
            if row is None:
                raise ValueError("Cross-skill relationship requires existing same-tenant rules")

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
