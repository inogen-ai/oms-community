from datetime import datetime, timezone

from neo4j import Driver

from oms.domain.models import ReviewItem
from oms.domain.types import Verdict


def _item_from_node(n) -> ReviewItem:
    return ReviewItem(
        id=n["id"], kind=n["kind"], subject_id=n["subject_id"],
        other_id=n["other_id"], verdict=Verdict(n["verdict"]),
        reason=n["reason"], tenant_id=n["tenant_id"], resolved=n["resolved"],
        proposed_body=n.get("proposed_body"), rule_id=n.get("rule_id"),
        transaction_id=n.get("transaction_id"),
        resolution=n.get("resolution"),
        # Items predating these properties read as None / "normal".
        candidate_skill_ids=n.get("candidate_skill_ids"),
        # Absent on every item written before this field existed, which is the
        # correct reading: no group.
        group_id=n.get("group_id"),
        # Absent on every kind but proposed_edit, and on an edit held before
        # this field existed. None reads as "cannot tell which text this was
        # written against", which the approval refuses rather than guesses at.
        base_revision=n.get("base_revision"),
        priority=n.get("priority", "normal"),
        created_at=n["created_at"].to_native(),
        decided_by=n.get("decided_by"),
        # Resolved before the field existed, or resolved through the
        # break-glass: both read as None, and the pair of them together is
        # what says which. Neo4j hands back its own DateTime, so convert as
        # created_at does rather than letting two shapes of the same field
        # reach a caller.
        decided_at=(n["decided_at"].to_native()
                    if n.get("decided_at") is not None else None),
    )


class Neo4jReviewQueue:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def enqueue(self, item: ReviewItem) -> None:
        self._enqueue(item, create_only=False)

    def enqueue_once(self, item: ReviewItem) -> None:
        self._enqueue(item, create_only=True)

    def _enqueue(self, item: ReviewItem, *, create_only: bool) -> None:
        assignment = "ON CREATE SET" if create_only else "SET"
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:ReviewItem {{id:$id}}) {assignment} n.kind=$kind, n.subject_id=$subject, "
                "n.other_id=$other, n.verdict=$verdict, n.reason=$reason, "
                "n.tenant_id=$tid, n.resolved=$resolved, n.created_at=$created, "
                "n.proposed_body=$proposed, n.rule_id=$rule, n.transaction_id=$txn, "
                "n.resolution=$resolution, n.candidate_skill_ids=$candidates, "
                "n.priority=$priority, n.group_id=$group, "
                "n.base_revision=$base_revision",
                id=item.id, kind=item.kind, subject=item.subject_id, other=item.other_id,
                verdict=item.verdict.value, reason=item.reason, tid=item.tenant_id,
                resolved=item.resolved, created=item.created_at,
                proposed=item.proposed_body, rule=item.rule_id, txn=item.transaction_id,
                resolution=item.resolution, candidates=item.candidate_skill_ids,
                priority=item.priority, group=item.group_id,
                base_revision=item.base_revision,
            )

    def pending(self, tenant_id: str) -> list[ReviewItem]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:ReviewItem {tenant_id:$tid, resolved:false}) RETURN n", tid=tenant_id
            )
            return [_item_from_node(r["n"]) for r in recs]

    def get(self, item_id: str) -> ReviewItem | None:
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (n:ReviewItem {id:$id}) RETURN n", id=item_id
            ).single()
            return _item_from_node(rec["n"]) if rec is not None else None

    def resolve(self, item_id: str, resolution: str,
                decided_by: str | None = None) -> ReviewItem:
        # Conditional update, so that two reviewers deciding the same item at
        # the same moment cannot both be told they won: exactly one sees a row
        # come back and the loser gets the ValueError the port promises,
        # instead of a second verdict overwriting a first that has already been
        # recorded and acted on. The attribution is set in the same statement
        # for the same reason: a second write would be a second chance to lose
        # the race, and a resolved item with no decider is exactly what that
        # field exists to stop happening.
        #
        # `SET n._claim = true REMOVE n._claim` before the guard is what makes
        # that true, and the plain `MATCH ... WHERE n.resolved = false SET ...`
        # this used to carry is NOT enough. The comment here used to assert
        # that the WHERE clause alone made concurrent resolves safe. It was
        # then measured against a real Neo4j, and it does not: eight concurrent
        # resolves of one pending item returned more than one winner in 15 of
        # 20 trials, and as many as five winners in one of them. Neo4j takes
        # the exclusive lock on the node when the SET fires, not when the WHERE
        # is evaluated, so every caller tests `resolved` against the state it
        # read before any of them wrote, and each one that wakes up afterwards
        # writes over a decision it never saw.
        #
        # Writing a property and removing it again in the same statement takes
        # that lock up front. Once it is held, the `WITH n WHERE` that follows
        # reads the committed value rather than the pre-race one, so a loser
        # matches nothing, returns no row, and is refused. The property is gone
        # by the time the statement ends, so nothing about the stored item
        # changes.
        #
        # The same hole was found and fixed first in `Neo4jSkillUploadStore`,
        # which had copied this method's shorter form. There it cost a second
        # `SkillImporter.apply()` over one archive; here it costs a re-graded
        # verdict, which is smaller but is still a decision an audit trail
        # says was made once.
        with self._driver.session() as session:
            rec = session.run(
                "MATCH (n:ReviewItem {id:$id}) "
                "SET n._claim = true REMOVE n._claim "
                "WITH n WHERE n.resolved = false "
                "SET n.resolved=true, n.resolution=$resolution, "
                "n.decided_by=$decided_by, n.decided_at=$decided_at RETURN n",
                id=item_id, resolution=resolution, decided_by=decided_by,
                decided_at=datetime.now(timezone.utc),
            ).single()
        if rec is not None:
            return _item_from_node(rec["n"])
        if self.get(item_id) is None:
            raise KeyError(item_id)
        raise ValueError(f"item {item_id} already resolved")

    def history(self, tenant_id: str, since: datetime | None = None,
                subject_id: str | None = None) -> list[ReviewItem]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (n:ReviewItem {tenant_id:$tid, resolved:true}) "
                "WHERE ($subject IS NULL OR n.subject_id=$subject) "
                "AND ($since IS NULL OR n.decided_at >= $since) "
                # DESC with nulls last: Cypher orders null FIRST on DESC, which
                # would put every pre-`decided_at` decision at the top of the
                # feed. The coalesce sorts them to the bottom instead, matching
                # the in-memory adapter.
                "RETURN n ORDER BY "
                "coalesce(n.decided_at, datetime('1970-01-01T00:00:00Z')) DESC",
                tid=tenant_id, subject=subject_id, since=since,
            )
            return [_item_from_node(r["n"]) for r in recs]
