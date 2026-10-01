# Contribution context, policy and mode

This is the contract for what an agent can attach to a contribution, how a
composition can refuse new contributions, what each transport answers, and
how an installation chooses whether its agents ask before sharing. Community
itself injects no policy, publishes with session learnings on, and installs
in automatic mode unless told otherwise.

## Session context fields

A contribution can carry four optional fields that describe the work around
it. The limits are in characters.

| Field | Limit | Meaning |
| --- | --- | --- |
| `session_summary` | 400 | One or two factual sentences stating the objective of the current piece of work, not a completion report. |
| `project_name` | 160 | A human-readable project name, mainly for work outside a repository. A name, never a path. |
| `reuse_case` | 400 | The future task this would help and what a future agent should do differently. |
| `learning_evidence` | 600 | What the agent observed that supports the lesson and its scope. Never secrets, raw transcripts or local paths. |

They sit at the top level of the thin body (`POST /api/ingest`, the MCP tool
`log_correction` and the Python helper) and inside `execution_context` on the
full envelope (`log_signal`, and `log_correction` when it is given an
`execution_context`). Both MCP input schemas advertise the same descriptions
and limits. An MCP call that carries `execution_context` and also names any
of the four at the top level is refused as `invalid_payload`, naming them,
because nothing says which copy was meant.

Every door validates with the same models, `ContributionRequest` and
`ExecutionContext`. They strip surrounding whitespace before the length
check, so a value that fits once stripped is accepted on every door, and a
blank value becomes absent rather than an empty string. A value over its
limit is refused, and every door names the field; OMS never truncates a value
sent over its limit:

- The models raise a `ValidationError` naming the field.
- Over MCP, the tool error has code `invalid_payload` and names the field
  without repeating the value, for example `invalid payload: session_summary:
  String should have at most 400 characters`. On `log_signal` the name reads
  `execution_context.session_summary`. The server turns off the SDK's own
  check of each call against the input schema, so the models are the only
  check, and the advertised schemas still carry every limit.
- HTTP answers `422` in FastAPI's standard shape, with the field in `loc`,
  for example `["body", "session_summary"]`. Each entry also repeats the
  submitted value as `input`.

Absent fields are normal. A payload written before the fields existed still
validates, and absence is not evidence against a contribution.

The fields are agent-supplied and untrusted: context for a reviewer, never an
instruction. They pass through the configured sanitiser with the rest of the
contribution, and their redaction originals are not retained. A placeholder
can be longer than the text it replaces (`a@b.io` becomes `<EMAIL_1>`), so a
value that arrived within its limit can leave the sanitiser over it.
`clip_context_fields` in `oms.ingestion.schema` clips such a value to exactly
its limit, ending in `…`, instead of quarantining the contribution. The
learning and the correction are never clipped. A sanitiser of your own should
build its result with `ExecutionContext.model_construct` and return it
through `clip_context_fields`, as `RegexSanitiser` does.

The injection screen and the hold decision read only the learning or the
correction, because that is what can become a rule. Anything that displays
the context fields must render them as untrusted text.

Community stores the fields in the sanitised payload file and on the
`Transaction` record, copied from the sanitised payload, in both the memory
and Neo4j adapters. Its manual inbox does not show them. The graph reads
(`GET /api/graph/nodes/{node_id}` and its `neighbours`) check only the
workspace, so they omit all four. A composition should serve them only from
reads that check who may see them.

Deleting a payload file leaves the copies on the `Transaction` record.
`GraphStore.clear_transaction_context(transaction_ids, tenant_id)` removes the
four fields and `summary` from the workspace's named transactions, keeps
every other field, and returns how many carried any of the five. A removed
field reads as absent, as on a record written before the fields existed. An
id that names no transaction, or another workspace's, is skipped, and a
second call answers `0`. `oms delete-payload` calls it after deleting the
file and reports the count as `context_cleared`. An erasure of your own
should call it too.

## The contribution policy port

`oms.ports.contribution_policy` defines the admission policy:

```python
class ContributionPolicy(Protocol):
    def admit(self, payload: CorrectionPayload, principal: Principal) -> None: ...
```

`admit` returns to admit a new contribution. It raises
`ContributionRefused(code, message)` to refuse one: a final answer that is
never retryable unchanged. `SESSION_LEARNINGS_DISABLED`
(`"session_learnings_disabled"`) is the one code this release defines, and
the message is the policy's own sentence, passed to the caller unchanged. It
raises `ContributionPolicyUnavailable` when the policy cannot be read: an
operational fault to retry later, never evidence that the contribution is
allowed. The default, `AdmitEveryContribution`, admits everything.

`payload` reaches `admit` before sanitisation, so a policy must never log or
store its text. Under `ContributionCoordinator`, `admit` runs inside the unit
of work that holds the workspace's lock, so a policy should only read: a write
through another connection would wait on that lock.

A composition injects its policy when it builds the ingestion service:

```python
ingestion = IngestionService(store, sanitiser, payloads, ..., policy=my_policy)
```

Only `None` selects the default, so a policy object that tests false is still
consulted. `in_transaction` copies the service with its policy, so
`ContributionCoordinator` and every transport built on it consult the same
one. The Community composition in `oms.community.app` passes none.

`IngestionService.ingest` works in this order:

1. The `ingest:write` scope and the workspace binding. A caller who may not
   write learns nothing about the policy.
2. The transaction id lookup. A stored id answers a retry from the stored
   transaction, or refuses a request that is not a retry of it, and never
   reaches the policy (see [idempotent retries](#idempotent-retries)).
3. `policy.admit`, with the payload as sent, before sanitisation.
4. Sanitisation, storage, screening and review.

A refusal therefore writes nothing: no transaction, payload file, quarantine
record, custody entry or review item. It reserves nothing either, so the same
transaction id is admitted as new work once the policy allows it.

Raise only these two exceptions. Anything else still fails the call and
admits nothing, but reaches the caller through the transport's ordinary error
handling: HTTP answers `500`, or `422` with the exception's text for a
`ValueError`, and the MCP SDK returns the exception's own text as the error.

## Refusals on each transport

| Transport | Refused | Policy unavailable |
| --- | --- | --- |
| MCP `log_correction` and `log_signal` | Tool error (`isError: true`) whose text is `{"error": <message>, "code": <code>, "retryable": false}` | Tool error: `{"error": "contribution policy is unavailable; nothing was recorded; retry later", "code": "unavailable", "retryable": true}` |
| HTTP `POST /api/ingest` | `403` with `{"detail": <message>, "reason": <code>, "retryable": false}` | `503` with `{"detail": "contribution policy is unavailable; nothing was recorded; retry later", "reason": "unavailable", "retryable": true}` |
| Python helper | Returns `{"status": "refused", "reason": <code>, "message": <message>, "retryable": false}` | Raises the `503` as `urllib.error.HTTPError` |

Neither outcome is an accepted receipt, so a client that reads only `isError`
or the status code still sees a failure. The unavailable sentence is fixed,
because the adapter's own exception may describe its infrastructure;
`POLICY_UNAVAILABLE` and `POLICY_UNAVAILABLE_MESSAGE` in the port hold the
code and the sentence for a transport of your own. A reused transaction id
is refused in the same three-key shape (see
[idempotent retries](#idempotent-retries)). Other MCP errors keep their two
keys, `error` and `code`; an argument that fails validation, on any tool
including the read tools, is `invalid_payload` in that shape. HTTP
also answers `403` for other refusals with `detail` alone, so branch on
`reason` and `retryable`, not on the status. `create_app` registers the HTTP
mapping for the whole application.

An accepted contribution answers `{"transaction_id": ..., "status":
"accepted", "state": ...}`, with `202` over HTTP. MCP leaves out `state` when
the transaction has none.

The bundled `oms_contribute.py` helper is generated at publish time:

```python
contribute_learning(correction, skill_hint=None, signal_type=None,
                    source_ref=None, repo=None, *, session_summary=None,
                    project_name=None, reuse_case=None,
                    learning_evidence=None, transaction_id=None) -> dict
```

With `signal_type="self_reflection"`, `correction` is sent as `learning`. A
keyword left as `None` is not sent, except `repo`, which the helper reads from
the `origin` remote of the current directory's checkout. It treats an error
response as a refusal when the JSON carries a non-empty string `reason` and
`"retryable": false`, either at the top level with the message in `detail`
or inside an `error` object with the message in `message`. Every other
failure raises as before, with its status, headers and body intact, so a
caller can still read which field was over its limit.

The command line takes the same context:

```sh
python oms_contribute.py '<correction>' ['<skill_hint>'] [--signal-type <type>]
    [--source-ref <ref>] [--session-summary <text>] [--project-name <name>]
    [--reuse-case <text>] [--evidence <text>] [--transaction-id <id>]
```

`--evidence` sets `learning_evidence`. The command prints the JSON answer and
exits `0` only on `"status": "accepted"`. A refusal also writes its message to
standard error and exits `1`. Any other failure exits `1`, and a flag without
a value is a usage error that exits `2` before anything is sent.

## Idempotent retries

On the thin body `transaction_id` is optional, and OMS assigns a UUID when it
is absent; the full envelope requires one. A caller that chooses its own id
should use a new UUID for each contribution: the thin body's schema
description says so, and so does the confirm-mode instruction. An id is 1 to
256 characters of letters, digits, `.`, `_`, `:` or `-`, starting with a
letter or digit. Over MCP a malformed id is an `invalid_payload` error naming
`transaction_id`. Over HTTP it is a `422` whose `detail` is the validation
text, which repeats the id.

An id already stored in the caller's workspace answers a retry with a
receipt for that transaction: the same `transaction_id`, `"status":
"accepted"`, and its current `state`, which may have moved on since the first
answer. The lookup runs before the policy and before sanitisation, so:

- a retry after an answer that never arrived records nothing twice;
- a retry of a transaction admitted before the policy changed still gets its
  receipt, and no new id is minted;
- nothing compares the retry's content with the original, so any change to
  the wording, the skill hint or the context needs a new id.

A request is a retry when it has the stored transaction's signal type and
comes from the same caller: the same person when both the stored transaction
and the request carry one (a person can hold a credential on each machine),
otherwise the same principal. A transaction stored without a principal is
answered as a retry, because there is nothing to compare. Any other request
with a stored id is refused, and nothing is written:

| Transport | Answer |
| --- | --- |
| MCP `log_correction` and `log_signal` | Tool error: `{"error": "transaction_id already names another contribution; nothing was recorded; send this one again with a new transaction_id", "code": "transaction_id_reused", "retryable": false}` |
| HTTP `POST /api/ingest` | `409` with `{"detail": <the same sentence>, "reason": "transaction_id_reused", "retryable": false}` |
| Python helper | Returns `{"status": "refused", "reason": "transaction_id_reused", "message": <the same sentence>, "retryable": false}` |

The refusal is final for that id, but not for the contribution, which can be
sent again under a new id. `IngestionService.ingest` raises
`TransactionIdReused`; `TRANSACTION_ID_REUSED` and
`TRANSACTION_ID_REUSED_MESSAGE` in `oms.ingestion.service` hold the code and
the sentence for a transport of your own.

Two limits follow from the check. Unbound callers share one principal, so
two of them cannot be told apart: a second unbound caller that sends an id
the first one used, for the same kind of signal, gets the first one's
receipt, and its own words are not stored. And any caller that may write can
learn whether a guessed id is stored in its workspace for another caller:
the refusal says so, and gives no state, person or content. A new UUID for
each contribution, as above, leaves nothing to guess.

An id stored in another workspace is refused as a tenant mismatch (`403`
over HTTP, code `tenant_mismatch` over MCP). A refused or unavailable
outcome stores nothing under the id.

## Instruction variants

With a contribution endpoint configured, every published root instruction
file ends in a contribution block. The block has four variants, one for each
combination of contribution mode and session learnings:

- `automatic` tells the agent to log a qualifying correction a person gives
  it in the same turn, before acknowledging it, and to approve the MCP tool
  once when prompted.
- `confirm` has the agent apply the correction to the current task, then ask
  "Should this apply just to this task, or would you like to suggest it for
  the team's `<skill>` guidance?", offering **Just this time** and **Review
  suggestion**. Only a preview the person approves is submitted, with a new
  UUID as its `transaction_id`, reused on a retry. Edited wording needs a new
  preview and a new id, and approving the tool once is not approval of later
  suggestions.
- With session learnings on, a `## Session learnings` section sets five tests
  a lesson must pass, asks for it as a rule in one imperative sentence, at
  most three per piece of work, and names the context fields to send. In
  confirm mode it adds "Show the person each proposed learning with its
  context; submit only those they approve."
- With session learnings off, that section is left out. It is the only text
  that asks an agent to reflect or to send `self_reflection`, and nothing
  above it changes.

`render_claude_md(constraints, skills, contribution_endpoint=None, *,
session_learnings=True, contribution_mode="automatic")` in
`oms.publish.render` renders one root file. An unknown mode raises
`ValueError`, with or without an endpoint. `Publisher(..., *,
session_learnings=True)` fixes the session-learnings choice for every file
one publish writes, and the caller resolves it for the workspace;
`Publisher.render_root(tenant_id, *, skills=None,
contribution_mode="automatic")` renders one variant with that choice.
Community's publisher never sets it, so Community publishes with session
learnings on.

`publish` writes each configured root file (by default `AGENTS.md` and
`CLAUDE.md`) in automatic mode and, when an endpoint is set, a confirm copy
beside it named by `confirm_variant_name`. The copy inserts `.confirm` before
the final suffix (`AGENTS.confirm.md`, `CLAUDE.confirm.md`), or appends it to
a name without one (`.cursorrules.confirm`). No harness loads these names by
itself; an installation in confirm mode names the file it wants. Building a
`Publisher` whose configured root files include another's confirm name raises
`ValueError`.

The copies are ledgered like the other root files, so withdrawing the
endpoint prunes them. Every root file in one publication comes from one read
of the constraints. The Cursor and Windsurf project rule files
(`.cursor/rules/oms.mdc` and `.windsurf/rules/oms.md`) always carry the
automatic variant. The confirm copies open with the same
`# Organisational Constraints` line, so a reader that finds root files by
that marker should skip any name that is the confirm name of another.

The MCP tool descriptions and the helper's docstring are the same in every
mode, and leave when to submit to the loaded instructions. Each tool
description ends "Follow your OMS instructions for when to submit; some
installations ask the person to confirm first." The helper opens "Send a
correction or learning that your OMS instructions say to share."

## Choosing a mode at install

`install.sh` and `install.ps1` choose one mode for the machine, from the
first of:

1. `OMS_CONTRIBUTION_MODE`, when it is set and not empty;
2. the mode this machine recorded on its last run, in
   `~/.oms/contribution-mode`;
3. `automatic`.

Only `automatic` and `confirm` are valid, compared case-sensitively.

```sh
OMS_CONTRIBUTION_MODE=confirm sh install.sh
```

On Windows, run `$env:OMS_CONTRIBUTION_MODE = 'confirm'` before
`install.ps1`. When the bundle takes contributions, its `README.md` explains
both modes to the person installing.

The mode is settled before anything is written. An invalid value, or a
record that is empty or unreadable, stops the install with
`OMS_CONTRIBUTION_MODE must be automatic or confirm`, changes nothing, and
shows both ways to re-run, confirm first. A damaged record is never read as
`automatic`. In confirm mode, a bundle without the confirm copy of the root
file the installer imports stops the install ("This bundle has no
confirm-mode instructions. Ask your OMS administrator to publish again.")
rather than installing the automatic file. Only choosing `automatic`
explicitly installs that. A bundle that takes no contributions is the
exception: its root file has no `# Contributing learnings` block, so it says
the same in both modes and publish writes no confirm copy. The installer
installs that file, says "This bundle takes no contributions, so both modes
install the same instructions.", and keeps confirm mode, so the refresh after
a publication that takes contributions installs the confirm copy.

A run that passes these checks records its mode in
`~/.oms/contribution-mode`, one word on one line, before it links any skills.
The scheduled refresh re-runs the installer without setting the variable, so
the machine keeps its mode until someone chooses again. A machine with no
record counts as having been automatic, like every installation made before
the mode existed. At its end, once every tool step is done, the run records
the mode the tools were given in `~/.oms/contribution-mode-installed`. A
change of mode is measured against that second record, so a run that stops
part way leaves the change for the next run to see. A machine installed
before the second record existed counts as having been given the mode it
chose. The run prints
`Contribution mode: <mode>`; the mode never goes into `status.md`, where
empty means healthy.

Every tool configured in one run gets the same variant:

| Tool | What it receives |
| --- | --- |
| Claude Code | An import of the chosen root file in its user `CLAUDE.md` |
| Codex, Windsurf and Antigravity | A copy of the chosen root file between OMS markers in their global instructions |
| Cursor | `~/.oms/cursor-user-rules.md`, staged for pasting into Cursor Settings > Rules |

In confirm mode the installer also writes Codex's own per-tool approval into
the OMS block of `~/.codex/config.toml`:

```toml
[mcp_servers.oms.tools.log_correction]
approval_mode = "prompt"
[mcp_servers.oms.tools.log_signal]
approval_mode = "prompt"
```

Automatic mode writes neither table. The block is rewritten on every run, so
choosing automatic again removes them. A table the user wrote for either
tool is left alone and decides for that tool. If `config.toml` already has
its own `[mcp_servers.oms]`, the installer leaves it alone and says that
confirm mode set no per-tool approval there. Codex documents `approval_mode`
without defining what each value does, so this setting is configured, not
qualified with a live client. The other tools' MCP registration is the same
in both modes.

A tool the run could not bring up to date is never left holding the other
mode's instructions without a word:

- Windsurf caps `global_rules.md` at 6000 characters, and the confirm copy is
  several hundred bytes longer than the automatic file. If the chosen file is
  over the cap after a change of mode, the installer removes the OMS block
  the last run left, keeps the person's own text, and reports Windsurf as not
  updated. In an unchanged mode the earlier block stays, as it always has.
- Cursor's rules are pasted by hand. After a change of mode, the installer
  asks the person to paste the staged file again, replacing the rules pasted
  before.
- A detected tool the installer skips because it cannot write where that
  tool's files go, and Windsurf in the case above, are listed on standard
  error as `Not updated: <tools>. The reasons are above.` and never counted
  in `Done. Configured: <tools>.` With nothing configured, the run ends
  `Done, with no agent tool fully configured. The reasons are above.`

### Variables for installer fragments

A composition that adds trusted fragments (`ShellInstallFragments` and
`PowerShellInstallFragments` in `oms.ports.publishing`) can read the settled
mode:

| POSIX sh | PowerShell | Value |
| --- | --- | --- |
| `OMS_CONTRIBUTION_MODE_RESOLVED` | `$OmsContributionMode` | `automatic` or `confirm`, already validated |
| `OMS_ROOT_FILE` | `$OmsRootFile` | The name, inside the bundle, of the root instruction file for that mode, known to exist |

Every fragment except `refresh_success` runs after the mode is settled.
`refresh_success` is text inside the generated refresh script: an unescaped
reference to either variable is fixed when the installer writes that script,
and an escaped one is unset when it runs. It needs neither, because the
refresh re-runs the installer, which settles the mode again.

`root_source` defaults to `$SRC/$OMS_ROOT_FILE` (`$SRC/$OmsRootFile` in
PowerShell, where `root_import` defaults to `@$SRC/$OmsRootFile`). A fragment
that replaces either must give the same mode's instructions, or the tools
installed in one run would disagree about whether to ask.

## What is guided and what is enforced

- Confirmation is instruction-guided and is not a security boundary. The
  confirm copy tells the agent to ask and to submit only an approved preview.
  Nothing in a request records a confirmation, and the server treats a
  contribution from a confirm-mode installation like any other.
- One MCP server answers every installation, automatic and confirm alike, so
  its tools declare no per-call approval requirement. Codex's
  `approval_mode = "prompt"` is the only client approval the installer
  writes. Where another client offers per-call approval, its settings decide,
  and a standing "always allow" choice or an automatic approval setting
  answers for the person.
- MCP approval covers MCP calls only. The Python helper and
  `POST /api/ingest` accept a contribution from any caller the deployment
  admits (in Community, which has no login, any client that can reach the
  API), with no client prompt. The confirm instructions cover those paths
  too; the server does not enforce them.
- Leaving the session learnings section out of the instructions does not stop
  learnings arriving. An agent can still hold a stale instruction file, and
  OMS cannot remove text already in an agent's context. A composition that
  turns session learnings off must also inject a policy that refuses
  `self_reflection` with `session_learnings_disabled`.
- A policy that refuses by signal type sees only the signal type the agent
  sent. The instructions scope corrections to what a person gives, and the
  helper tells a caller not to resend a refusal as a different signal, but
  nothing stops an agent relabelling its own lesson as a correction.
- An edition that needs enforced confirmation needs a control on the server
  side, such as a credential bound to the person or a policy. Instructions
  alone can only guide the agent.
