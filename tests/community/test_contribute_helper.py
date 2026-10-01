"""The bundled Python helper carries session context, a stable id and refusals.

`oms_contribute.py` is generated at publish time and runs on a contributor's
machine with only the standard library, so these tests render it, execute the
generated text and stub the one network call it makes. They pin what reaches
the ingest door and what the caller hears back: the session context and a
`transaction_id` travel only when given, a policy refusal comes back as an
answer the caller can branch on rather than as an exception, and every other
failure still raises as it did before.
"""
import ast
from collections.abc import Callable
import email.message
import inspect
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
from typing import Annotated
import urllib.error
import urllib.request
import uuid

from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest

from oms.community.app import build_memory
from oms.community.routes import LocalContributionRequest
from oms.ingestion.schema import CorrectionPayload, ExecutionContext
from oms.ports.contribution_policy import (
    POLICY_UNAVAILABLE, POLICY_UNAVAILABLE_MESSAGE, SESSION_LEARNINGS_DISABLED,
)
from oms.publish.render import render_contribute_tool
from oms.settings.core import CoreSettings
from oms.web.api import create_app

ENDPOINT = "http://localhost:8000/api/ingest"
GENERATED = render_contribute_tool(ENDPOINT)
RULE = "In this repo, run the worker's linter before its tests."
# The context an agent attaches to a session learning, one value per field.
CONTEXT = {
    "session_summary": ("Move the payment retries onto the queue worker and verify the "
                        "release build."),
    "project_name": "Payments platform",
    "reuse_case": "Future changes to the retry worker: lint with its own config first.",
    "learning_evidence": ("The root linter passed twice while the worker's own config "
                          "failed the build."),
}
# The keyword-only tail of the signature, in its published order.
NEW_KEYWORDS = ("session_summary", "project_name", "reuse_case", "learning_evidence",
                "transaction_id")
# The CLI flag for each new keyword.
FLAGS = {"--session-summary": "session_summary", "--project-name": "project_name",
         "--reuse-case": "reuse_case", "--evidence": "learning_evidence",
         "--transaction-id": "transaction_id"}
# Quoted exactly: the sentence both docstrings give, whichever contribution
# mode the installation uses.
SHARE = "Send a correction or learning that your OMS instructions say to share."
# What a policy says when it refuses. The wording belongs to whichever
# composition injects the policy; the helper passes it through unchanged.
REFUSAL = "Session learnings are not accepted here. Nothing was recorded. Do not retry it."
ACCEPTED = {"transaction_id": "t-1", "status": "accepted"}
REFUSED = {"status": "refused", "reason": SESSION_LEARNINGS_DISABLED, "message": REFUSAL,
           "retryable": False}
# The two shapes a refusal arrives in: inside an error envelope, and at the
# top level beside a `detail`, which is how this repository's ingest route
# sends it.
ENVELOPE_REFUSAL = {"error": {"code": 403, "message": REFUSAL,
                              "reason": SESSION_LEARNINGS_DISABLED, "retryable": False}}
TOP_LEVEL_REFUSAL = {"detail": REFUSAL, "reason": SESSION_LEARNINGS_DISABLED, "retryable": False}


class _Response:
    """What `urlopen` returns for a 2xx answer: `_post` reads it in a `with`."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _raw(body: dict | bytes) -> bytes:
    return body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")


def _http_error(status: int, body: dict | bytes) -> urllib.error.HTTPError:
    """A real HTTPError, as `urlopen` raises for a non-2xx answer, with a body."""
    headers = email.message.Message()
    headers["Content-Type"] = "text/html" if isinstance(body, bytes) else "application/json"
    return urllib.error.HTTPError(ENDPOINT, status, "stub", headers, io.BytesIO(_raw(body)))


def _stub(monkeypatch: pytest.MonkeyPatch,
          outcomes: list[dict | Exception] | None = None) -> list[dict]:
    """Replace the network with a queue of answers and record every body posted.

    Patched on the shared `urllib.request` module, which the generated helper
    imports like any other code, so it reaches a helper executed into a
    namespace and one run as a script alike. Once the queue is empty, every
    request is accepted."""
    bodies: list[dict] = []
    queue = list(outcomes or [])

    def urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        bodies.append(json.loads(request.data.decode("utf-8")))
        outcome = queue.pop(0) if queue else ACCEPTED
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return bodies


def _helper() -> dict:
    """The generated helper, executed the way an import would run it.

    `_repo()` reads the remote of whichever checkout the process stands in,
    which here is this repository's own. It is neutralised so that every body
    below holds only what the test sent."""
    namespace: dict = {"__file__": "/nonexistent/oms_contribute.py"}
    exec(compile(GENERATED, "oms_contribute.py", "exec"), namespace)  # noqa: S102
    namespace["_repo"] = lambda: None
    return namespace


class _NoCheckout:
    """What `git remote get-url origin` answers outside a checkout."""
    returncode = 128
    stdout = ""
    stderr = "fatal: not a git repository"


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
         outcomes: list[dict | Exception] | None = None
         ) -> tuple[list[dict], Callable[..., None]]:
    """Run the rendered file as a script, in process, through `runpy`.

    In process so the stubbed network sees the body the command line built.
    The script defines and calls its own `_repo()` in the same run, so it is
    neutralised at `subprocess.run`, the one call it makes. Returns the
    recorded bodies and a `run(*argv)` that executes the `__main__` block."""
    script = tmp_path / "oms_contribute.py"
    script.write_text(GENERATED, encoding="utf-8")
    bodies = _stub(monkeypatch, outcomes)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _NoCheckout())

    def run(*argv: str) -> None:
        monkeypatch.setattr(sys, "argv", [str(script), *argv])
        runpy.run_path(str(script), run_name="__main__")

    return bodies, run


def test_the_new_keywords_are_sent_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent stays absent, so a caller written before these keywords posts
    the body it always posted, and one keyword given alone travels alone."""
    helper = _helper()
    bodies = _stub(monkeypatch)

    helper["contribute_learning"](RULE)
    helper["contribute_learning"](RULE, signal_type="self_reflection",
                                  source_ref="session:abc", **CONTEXT)
    helper["contribute_learning"](RULE, signal_type="self_reflection",
                                  reuse_case=CONTEXT["reuse_case"])

    assert bodies == [
        {"correction": RULE},
        {"learning": RULE, "signal_type": "self_reflection", "source_ref": "session:abc",
         **CONTEXT},
        {"learning": RULE, "signal_type": "self_reflection",
         "reuse_case": CONTEXT["reuse_case"]},
    ]
    # Under the names the ingest route declares. It refuses a key it does not
    # know, so one misspelt name would cost the whole contribution.
    for body in bodies:
        LocalContributionRequest.model_validate(body)


def test_existing_positional_callers_keep_their_meaning(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper()
    bodies = _stub(monkeypatch)

    helper["contribute_learning"](RULE, "deployment-runbook", "self_reflection",
                                  "session:abc", "github.com/acme/payments")

    assert bodies == [{"learning": RULE, "skill_hint": "deployment-runbook",
                       "signal_type": "self_reflection", "source_ref": "session:abc",
                       "repo": "github.com/acme/payments"}]
    # The new keywords follow a bare `*`, so no positional call can reach one.
    parameters = list(inspect.signature(helper["contribute_learning"]).parameters.values())
    assert [p.name for p in parameters] == ["correction", "skill_hint", "signal_type",
                                            "source_ref", "repo", *NEW_KEYWORDS]
    assert all(p.kind is p.KEYWORD_ONLY for p in parameters if p.name in NEW_KEYWORDS)
    with pytest.raises(TypeError):
        helper["contribute_learning"](RULE, None, None, None, None, CONTEXT["session_summary"])


def test_a_transaction_id_is_forwarded_for_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an answer never arrives, the retry carries the same id, so the
    server can hand back the first receipt instead of recording the
    contribution twice. The helper never makes an id up: the server assigns
    one when none is given."""
    helper = _helper()
    lost = urllib.error.URLError(TimeoutError("timed out"))
    bodies = _stub(monkeypatch, [lost, ACCEPTED])

    with pytest.raises(urllib.error.URLError):
        helper["contribute_learning"](RULE, transaction_id="t-1")
    ack = helper["contribute_learning"](RULE, transaction_id="t-1")

    assert ack == ACCEPTED
    assert [body.get("transaction_id") for body in bodies] == ["t-1", "t-1"]


def test_a_refusal_in_an_error_envelope_is_returned_as_refused_not_raised(
        monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper()
    _stub(monkeypatch, [_http_error(403, ENVELOPE_REFUSAL)])

    assert helper["contribute_learning"](RULE, signal_type="self_reflection") == REFUSED


def test_a_refusal_at_the_top_level_is_returned_as_refused_not_raised(
        monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper()
    no_sentence = {"reason": SESSION_LEARNINGS_DISABLED, "retryable": False}
    _stub(monkeypatch, [_http_error(403, TOP_LEVEL_REFUSAL), _http_error(403, no_sentence)])

    assert helper["contribute_learning"](RULE, signal_type="self_reflection") == REFUSED
    # A refusal that arrives without a sentence is still a refusal, and its
    # reason stands in for the missing message.
    assert helper["contribute_learning"](RULE, signal_type="self_reflection") == {
        **REFUSED, "message": SESSION_LEARNINGS_DISABLED}


@pytest.mark.parametrize(("status", "body"), [
    # A fault the server asks to be retried later is not a refusal: the
    # caller keeps its existing way of handling an error and retrying.
    pytest.param(503, {"detail": POLICY_UNAVAILABLE_MESSAGE, "reason": POLICY_UNAVAILABLE,
                       "retryable": True}, id="policy-unavailable"),
    pytest.param(422, {"detail": [{"loc": ["body", "session_summary"], "type": "string_too_long",
                                   "msg": "String should have at most 400 characters"}]},
                 id="field-over-its-limit"),
    pytest.param(403, {"error": {"code": 403, "message": "unauthorised: no credential"}},
                 id="error-envelope-without-a-reason"),
    pytest.param(403, {"detail": "browser origin is not allowed"}, id="detail-without-a-reason"),
    pytest.param(502, b"<html><body>Bad gateway</body></html>", id="body-that-is-not-json"),
])
def test_an_unrelated_http_error_still_raises(monkeypatch: pytest.MonkeyPatch, status: int,
                                              body: dict | bytes) -> None:
    """Only a final refusal becomes an answer. Everything else raises as it
    did before the refusal check existed, with the same status, headers and
    body, so a caller can still read which field was over its limit."""
    helper = _helper()
    sent = _http_error(status, body)
    _stub(monkeypatch, [sent])

    with pytest.raises(urllib.error.HTTPError) as raised:
        helper["contribute_learning"](RULE, signal_type="self_reflection")

    assert (raised.value.code, raised.value.reason, raised.value.geturl()) == (
        status, "stub", ENDPOINT)
    assert raised.value.headers["Content-Type"] == sent.headers["Content-Type"]
    assert raised.value.read() == _raw(body)


def test_the_cli_exits_nonzero_on_a_refusal_and_prints_its_message(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    bodies, run = _cli(tmp_path, monkeypatch, [_http_error(403, ENVELOPE_REFUSAL)])

    with pytest.raises(SystemExit) as exited:
        run(RULE, "--signal-type", "self_reflection", "--source-ref", "session:abc")

    assert exited.value.code == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == REFUSED
    assert REFUSAL in err
    assert len(bodies) == 1


def test_the_cli_carries_the_context_and_the_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    bodies, run = _cli(tmp_path, monkeypatch)

    run(RULE, "deployment-runbook", "--signal-type", "self_reflection",
        "--source-ref", "session:abc",
        "--session-summary", CONTEXT["session_summary"],
        "--project-name", CONTEXT["project_name"],
        "--reuse-case", CONTEXT["reuse_case"],
        "--evidence", CONTEXT["learning_evidence"],
        "--transaction-id", "t-1")

    assert bodies == [{"learning": RULE, "skill_hint": "deployment-runbook",
                       "signal_type": "self_reflection", "source_ref": "session:abc",
                       **CONTEXT, "transaction_id": "t-1"}]
    assert json.loads(capsys.readouterr().out) == ACCEPTED


@pytest.mark.parametrize("flag", FLAGS)
def test_a_new_flag_without_a_value_is_a_usage_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        flag: str) -> None:
    """A dangling flag must not post a contribution missing what it asked to
    carry, and the usage line names every flag the command takes."""
    bodies, run = _cli(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exited:
        run(RULE, "--signal-type", "self_reflection", flag)

    assert exited.value.code == 2
    usage = capsys.readouterr().err
    assert usage.startswith("usage: oms_contribute.py")
    for known in FLAGS:
        assert known in usage
    assert bodies == []


def test_the_docstring_gives_no_immediate_log_direction() -> None:
    """The same helper ships to every installation, including one that asks
    the person before anything is shared. When to send belongs to the
    instructions, so the docstrings say only what the helper sends."""
    module = ast.parse(GENERATED)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                    and node.name == "contribute_learning")
    for docstring in (ast.get_docstring(module), ast.get_docstring(function)):
        assert SHARE in " ".join(docstring.split())
    for direction in ("when the user corrects you", "call contribute_learning(",
                      "in the same turn"):
        assert direction not in GENERATED.lower()


def test_each_context_keyword_is_documented_as_the_envelope_describes_it() -> None:
    """The helper's reader is told what the MCP input schema and the thin
    door tell theirs, limits included, from the one description."""
    function = next(node for node in ast.parse(GENERATED).body
                    if isinstance(node, ast.FunctionDef) and node.name == "contribute_learning")
    documented = " ".join(ast.get_docstring(function).split())
    for name in CONTEXT:
        assert f"{name}: {ExecutionContext.model_fields[name].description}" in documented
    assert "transaction_id:" in documented


def _documented_function() -> str:
    """`contribute_learning`'s docstring, as one line of single spaces."""
    function = next(node for node in ast.parse(GENERATED).body
                    if isinstance(node, ast.FunctionDef) and node.name == "contribute_learning")
    return " ".join(ast.get_docstring(function).split())


def test_the_cli_exits_zero_on_the_receipt_this_repositorys_ingest_route_sends(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI counts a contribution as accepted only when the answer says
    `"status": "accepted"`. This repository's ingest route once answered with
    the transaction and its state alone, so every contribution it accepted
    was reported as a failure and exited 1. Sent through the real route
    rather than a stubbed receipt, so the two cannot drift apart again."""
    data = tmp_path / "data"
    app = create_app(build_memory(data, tenant="acme"),
                     settings=CoreSettings(data_dir=data, tenant_id="acme"))
    _, run = _cli(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1:4317") as client:
        def urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
            answer = client.post("/api/ingest", content=request.data,
                                 headers={"Content-Type": "application/json"})
            assert answer.status_code == 202, answer.text
            return _Response(answer.json())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        try:
            run(RULE, "--transaction-id", "cli-receipt")
        except SystemExit as exited:
            pytest.fail(f"the CLI exited {exited.code} on an accepted contribution")

    ack = json.loads(capsys.readouterr().out)
    assert (ack["transaction_id"], ack["status"]) == ("cli-receipt", "accepted")


def test_the_transaction_id_entry_says_which_ids_are_accepted_and_when_to_mint_one() -> None:
    """OMS answers a repeated id with the first receipt without comparing
    what was sent, so reusing an id for anything but a retry of the same
    request loses the change in silence. An id another person used is
    refused, and the entry says what to do then. The accepted form is the
    stored one, checked here against the field that stores it."""
    documented = _documented_function()
    assert ('letters, digits, ".", "_", ":" or "-", starting with a letter or digit, '
            'up to 256 characters. Use a new UUID for each contribution.') in documented
    assert ("without comparing what was sent, so any change to what is sent (the wording, "
            "skill_hint, the context or the signal) needs a new id.") in documented
    assert ('refused with the reason "transaction_id_reused"; send the contribution again '
            "with a new id.") in documented

    stored = TypeAdapter(Annotated[str, CorrectionPayload.model_fields["transaction_id"]])
    for accepted in (str(uuid.uuid4()), "a" * 256, "session:abc_1.2-x"):
        stored.validate_python(accepted)
    for refused in ("a" * 257, "-leading-hyphen", ".leading-dot", "has space", "x/y"):
        with pytest.raises(ValidationError):
            stored.validate_python(refused)


@pytest.mark.parametrize("description", [
    pytest.param(None, id="missing"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("A path such as C:\\temp\\notes.", id="backslash"),
    pytest.param('Ends the docstring """ and runs the rest.', id="triple-quote"),
])
def test_a_field_description_that_would_break_the_helper_is_refused(
        monkeypatch: pytest.MonkeyPatch, description: str | None) -> None:
    """The context descriptions are copied into a docstring of generated
    source. A backslash there is read as an escape and a triple quote ends
    the docstring early, turning the rest of the text into code, so the
    renderer refuses to write either rather than ship a helper that fails
    on a contributor's machine."""
    field = ExecutionContext.model_fields["reuse_case"]
    monkeypatch.setattr(field, "description", description)

    with pytest.raises(ValueError, match="reuse_case"):
        render_contribute_tool(ENDPOINT)
