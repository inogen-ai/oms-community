# Deterministic reference tests

The exported tests are executable reference cases for the local workflow and
its storage contracts. They use synthetic skills, corrections and source
archives, with explicit expected state and bytes. The release workflow runs
them against the installed `oms-core` candidate to check packaged behaviour.
To run the reference cases during local development:

```sh
uv sync --frozen --extra test
uv run pytest tests
```

Database cases use a disposable Neo4j. An explicit test database requires
`OMS_TEST_NEO4J_URI`, `OMS_TEST_NEO4J_PASSWORD` and
`OMS_TEST_NEO4J_DISPOSABLE=1`; otherwise the fixture starts its own container.
These tests erase their database between cases.

| Reference suite | Observable contract |
| --- | --- |
| `tests/community/test_critic_api.py` | HTTP surface, workspace and browser request boundaries, import/capture/review/history/publication behaviour |
| `tests/community/test_critic_workflow.py` | Atomic manual decisions, idempotent retries, concurrent conflicts, exact reinforcement, lineage, payload custody and safety holds |
| `tests/community/test_manual_amendment.py` | Atomic rule/prose amendments, correction evidence, shared-rule impact, stale drafts, rollback and explicit section placement in memory and Neo4j |
| `tests/community/test_critic_import_review.py` | Explicit three-way source review, preserved terminal audit, source retry behaviour, rule membership and constraint edit/retire/restore |
| `tests/community/test_critic_catalogue.py` | A denied workspace is refused before its publication gate or content is read |
| `tests/community/test_repository_views.py` | Narrow repository contracts and matching memory/Neo4j semantics for scoped reads, history and custody |
| `tests/schema/test_critic_schema.py` | Schema/edition ownership, repeated and concurrent migration, rollback and preservation of existing data |

The frontend's fixture journeys cover visible local tasks. Its
`npm run e2e:real` journey drives a freshly installed core wheel, imports a
synthetic archive, records manual choices, publishes once per active rule and
compares original reference/script bytes. Set `OMS_CORE_PYTHON` to that clean
environment's Python; the journey owns its temporary workspace and test ports.

These references establish deterministic correctness for the tested inputs.
They do not measure semantic judgement, unseen prompt-injection detection,
model quality, productivity gains or workload performance. Exact reinforcement
uses normalised wording; it does not establish semantic equivalence between
different statements. Any benchmark claim needs a named corpus, task,
measurement method, environment and reproducible result separate from these
conformance cases.

Release evidence records the candidate hashes, installed package versions and
suite results. Test counts and timings belong with that particular run, since
the cases change as the contracts evolve. See [release validation](releasing.md).
