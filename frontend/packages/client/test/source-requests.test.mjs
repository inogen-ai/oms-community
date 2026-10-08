import test from "node:test";
import assert from "node:assert/strict";
import * as module from "../src/index.js";
const scope = { server: "https://oms.example.test", workspace: "tenant-1", authSession: "session-1" };
const path = "/api/skill-updates/u/apply";
const body = { skill_id: "s", fingerprint: { format_version: 1, content_generation: 1, binding_generation: 1, update_generation: 0, policy_version: "p", policy_digest: "policy-state", local_digest: "l", candidate_digest: "u" }, removal_consents: [] };
const init = (key = "original-key", data = body) => ({ method: "POST", headers: { "Idempotency-Key": key, Authorization: "Bearer private-fixture" }, body: JSON.stringify(data) });
const storage = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key), values };
};
function create(options) {
  assert.equal(typeof module.createSourceRequests, "function");
  return module.createSourceRequests({ scope, ...options });
}

test("staged local approval survives reload with its exact target and no uploaded bytes", async () => {
  const store = storage();
  const payload = { upload_id: "upload-1", selections: [{ package_path: "skills/expenses", target_skill_id: "expenses" }] };
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  const client = module.createSourcesClient({ request: lost.request });
  await assert.rejects(client.installLocal(payload, "local-key"), /Lost/);
  let received;
  const resumed = create({ storage: store, request: async (url, options) => {
    received = [url, JSON.parse(options.body), new Headers(options.headers).get("Idempotency-Key")];
    return { operation_id: "local-op", state: "awaiting_review", committed: false, outcomes: [] };
  } });
  await resumed.recover("local-key");
  assert.deepEqual(received, ["/api/skill-local-imports", payload, "local-key"]);
  assert.equal((await resumed.pending()).length, 0);
});

test("an edition route is retained and recovered like the built-in routes, behind its own body guard", async () => {
  const store = storage();
  const url = "/api/example-approvals/approval-1/approve?tenant=acme";
  const mutations = [{ method: "POST", pathname: value => /^\/api\/example-approvals\/[^/?.]+\/approve$/.test(value),
    body: parsed => parsed !== null && typeof parsed === "object" && Object.keys(parsed).length === 1 && typeof parsed.approval_id === "string" }];
  const payload = { approval_id: "approval-preview" };
  const lost = create({ storage: store, mutations, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request(url, init("approve-key", payload)), /Lost/);
  assert.deepEqual((await lost.pending()).map(row => [row.path, row.requires_body]), [[url, false]]);
  const operation = { operation_id: "batch-op", state: "complete", committed: true, outcomes: [] };
  let received;
  const resumed = create({ storage: store, mutations, request: async (path, options) => {
    received = [path, JSON.parse(options.body), new Headers(options.headers).get("Idempotency-Key")];
    return { approval: { id: "approval-1", status: "approved" }, operation };
  } });
  assert.deepEqual(await resumed.recover("approve-key"), operation);
  assert.deepEqual(received, [url, payload, "approve-key"]);
  assert.equal((await resumed.pending()).length, 0);
  // A body the guard refuses is never stored; the request is retained by key only.
  const wrong = create({ storage: storage(), mutations, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(wrong.request(url, init("wrong-key", { approval_id: "a", extra: true })), /Lost/);
  assert.deepEqual((await wrong.pending()).map(row => [row.body, row.requires_body]), [[null, true]]);
  // Without registration, or with other hygiene failures, the route is not retained.
  await assert.rejects(create({ storage: storage(), request: async () => ({}) }).request(url, init("none-key", payload)), /Only source mutations/);
  const registered = create({ storage: storage(), mutations, request: async () => ({}) });
  for (const bad of ["/api/example-approvals/a/approve?other=1", "/api/example-approvals/a/approve#x", "/api/example-approvals/%2e%2e/approve", "/api/example-approvals/a/other"]) {
    await assert.rejects(registered.request(bad, init("bad-key", payload)), /Only source mutations/);
  }
  // A registered method does not widen the others.
  await assert.rejects(registered.request("/api/example-approvals/a/approve", { ...init("put-key", payload), method: "PUT" }), /Only source mutations/);
});

test("reload after a lost first response reuses the original body and key", async () => {
  const store = storage();
  const first = create({ storage: store, request: async () => { throw new TypeError("Lost connection"); } });
  await assert.rejects(first.request(path, init()), /Lost connection/);
  assert.equal((await first.pending()).length, 1);
  assert.ok(![...store.values.values()].join("").includes("private-fixture"));
  let seen;
  const restored = create({ storage: store, request: async (url, options) => {
    seen = { url, options };
    return { operation_id: "op", state: "complete", committed: true, outcomes: [] };
  } });
  await restored.recover("original-key");
  assert.equal(seen.url, path);
  assert.equal(new Headers(seen.options.headers).get("Idempotency-Key"), "original-key");
  assert.deepEqual(JSON.parse(seen.options.body), body);
  assert.equal((await restored.pending()).length, 0);
});

test("known operation recovery polls without replaying a mutation", async () => {
  const store = storage();
  const first = create({ storage: store, request: async () => ({ operation_id: "op/1", state: "planning", committed: false, outcomes: [] }) });
  await first.request(path, init());
  let seen;
  const restored = create({ storage: store, request: async (url, options) => {
    seen = { url, options };
    return { operation_id: "op/1", state: "awaiting_review", committed: false, outcomes: [] };
  } });
  const response = await restored.recover("original-key");
  assert.equal(seen.url, "/api/skill-source-operations/op%2F1");
  assert.equal(seen.options?.method ?? "GET", "GET");
  assert.equal(response.state, "awaiting_review");
  assert.equal((await restored.pending()).length, 0);
});

test("merged prose and discovery URLs remain unstored but exact caller resubmission can recover", async () => {
  for (const [url, data] of [["/api/skill-updates/u/draft", { ...body, choices: [{ part_id: "p", choice: "merged_text", merged_text: "private-merged-prose", part_fingerprint: "f" }] }], ["/api/skill-source-discoveries", { url: "https://user:private-password@github.com/org/repo" }]]) {
    const store = storage();
    const first = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
    await assert.rejects(first.request(url, init("key", data)));
    const serialized = [...store.values.values()].join("");
    assert.ok(!serialized.includes("private-merged-prose"));
    assert.ok(!serialized.includes("private-password"));
    const restored = create({ storage: store, request: async (url, options) => {
      if (options?.method === "GET") throw Object.assign(new Error("Not found"), { status: 404 });
      return { operation_id: "op", state: "complete", committed: true, outcomes: [] };
    } });
    await assert.rejects(restored.recover("key"), /original request body/i);
    await assert.rejects(restored.recover("key", init("key", {})), /identity/i);
    await restored.recover("key", init("key", data));
    assert.equal((await restored.pending()).length, 0);
  }
});

test("private draft recovery looks up the caller's receipt without persisting or resending prose", async () => {
  const store = storage();
  const draft = { ...body, choices: [{ part_id: "p", choice: "merged_text", merged_text: "private wording", part_fingerprint: "f" }] };
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request("/api/skill-updates/u/draft", init("draft-key", draft)));
  let seen;
  const restored = create({ storage: store, request: async (url, options) => {
    seen = [url, options.method, options.body];
    return { operation_id: "accepted", state: "awaiting_review", committed: false, outcomes: [] };
  } });
  await restored.recover("draft-key");
  assert.deepEqual(seen, ["/api/skill-source-operations?request_key=draft-key", "GET", undefined]);
  assert.equal((await restored.pending()).length, 0);
  assert.ok(![...store.values.values()].join("").includes("private wording"));
});

test("GitHub install retains only the discovery selection body for exact reload recovery", async () => {
  const store = storage();
  const payload = { discovery_id: "d", selections: [{ package_path: "", local_name: "Expenses", domain: "finance" }] };
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request("/api/skill-source-installations", init("install-key", payload)));
  let seen;
  const resumed = create({ storage: store, request: async (url, options) => {
    seen = JSON.parse(options.body);
    return { operation_id: "installed", state: "complete", committed: true, outcomes: [] };
  } });
  await resumed.recover("install-key");
  assert.deepEqual(seen, payload);
});

test("server, workspace, authentication changes and logout discard pending state and late responses", async () => {
  for (const replacement of [null, { ...scope, server: "https://other.test" }, { ...scope, workspace: "tenant-2" }, { ...scope, authSession: "session-2" }]) {
    const store = storage();
    let finish;
    let started;
    const ready = new Promise(resolve => { started = resolve; });
    const requests = create({ storage: store, request: () => { started(); return new Promise(resolve => { finish = resolve; }); } });
    const flight = requests.request(path, init());
    await ready;
    requests.setScope(replacement);
    finish({ operation_id: "old", state: "planning", committed: false });
    await assert.rejects(flight, error => error.name === "StaleSourceResponseError");
    assert.equal((await requests.pending()).length, 0);
    if (replacement === null) await assert.rejects(requests.request(path, init()), /session/i);
  }
});

test("restoration validates scope and safe payloads before exposing pending state", async () => {
  const store = storage();
  const first = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(first.request(path, init()));
  const mismatch = create({ storage: store, scope: { ...scope, authSession: "new-session" }, request: async () => {} });
  assert.equal((await mismatch.pending()).length, 0);
  await assert.rejects(first.request(path, init("second")));
  for (const [key, raw] of store.values) {
    const saved = JSON.parse(raw);
    saved.requests[0].body = JSON.stringify({ ...body, merged_text: "private" });
    store.setItem(key, JSON.stringify(saved));
  }
  const tampered = create({ storage: store, request: async () => { assert.fail("Must not send tampered data"); } });
  assert.equal((await tampered.pending()).length, 0);
});

test("terminal refusal ends recovery and explicit retry requires a new identity", async () => {
  const store = storage();
  const seen = [];
  const requests = create({ storage: store, request: async (_, options) => {
    seen.push(new Headers(options.headers).get("Idempotency-Key"));
    return { operation_id: "op", state: "blocked", committed: false, outcomes: [] };
  } });
  await requests.request(path, init());
  assert.equal((await requests.pending()).length, 0);
  await assert.rejects(requests.recover("original-key"), /pending/i);
  await requests.request(path, init("explicit-new-key"));
  assert.deepEqual(seen, ["original-key", "explicit-new-key"]);
});

test("same key with a changed body is refused locally and read responses are fenced too", async () => {
  const requests = create({ storage: storage(), request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(requests.request(path, init()));
  await assert.rejects(requests.request(path, init("original-key", { ...body, skill_id: "other" })), /identity/i);
  let finish;
  const reader = create({ storage: storage(), request: () => new Promise(resolve => { finish = resolve; }) });
  const flight = reader.request("/api/skill-sources");
  await new Promise(resolve => setImmediate(resolve));
  reader.clear();
  finish({ items: [], next_cursor: null });
  await assert.rejects(flight, error => error.name === "StaleSourceResponseError");
});

test("recovery refreshes edition context for operation polling and guards byte responses", async () => {
  const store = storage();
  const calls = [];
  const context = async (url, options) => ({ path: `${url}?tenant_id=tenant-1`, init: { ...options, headers: { ...Object.fromEntries(new Headers(options?.headers)), Authorization: "Bearer fresh" } } });
  const first = create({ storage: store, context, request: async () => ({ operation_id: "op", state: "planning", committed: false }) });
  await first.request(path, init());
  let finish;
  const restored = create({ storage: store, context, request: async (url, options) => { calls.push({ url, options }); return { operation_id: "op", state: "complete", committed: true, outcomes: [] }; },
    requestBytes: async (url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve; }); } });
  await restored.recover("original-key");
  assert.equal(calls[0].url, "/api/skill-source-operations/op?tenant_id=tenant-1");
  assert.equal(new Headers(calls[0].options.headers).get("Authorization"), "Bearer fresh");
  const download = restored.requestBytes("/api/skill-updates/u/files?path=file&preview=false");
  await new Promise(resolve => setImmediate(resolve));
  restored.setScope(null);
  finish({ bytes: new Uint8Array([1]) });
  await assert.rejects(download, error => error.name === "StaleSourceResponseError");
});

test("obsolete async context cannot start a request under the new session", async () => {
  let finish;
  const requests = create({ storage: storage(), context: () => new Promise(resolve => { finish = resolve; }), request: async () => assert.fail("obsolete context reached network") });
  const flight = requests.request("/api/skill-sources");
  await new Promise(resolve => setImmediate(resolve));
  requests.setScope(null);
  finish({ path: "/api/skill-sources" });
  await assert.rejects(flight, error => error.name === "StaleSourceResponseError");
});

test("unavailable session storage retains in-memory recovery and reports its limitation", async () => {
  const requests = create({ storage: { getItem() { throw new Error("Disabled"); }, setItem() { throw new Error("Disabled"); }, removeItem() { throw new Error("Disabled"); } }, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(requests.request(path, init()));
  assert.equal(requests.persistent, false);
  assert.equal((await requests.pending()).length, 1);
});

test("malformed or foreign stored envelopes cannot disable the current session", async () => {
  for (const value of [null, [], 3, "value", JSON.parse('{"__proto__":{}}'), { version: 1, scope, requests: [null] }, { version: 1, scope, requests: [], credentials: "not-allowed" }]) {
    const store = storage();
    store.setItem("oms.source-requests.v1", JSON.stringify(value));
    const requests = create({ storage: store, request: async () => ({ items: [], next_cursor: null }) });
    assert.deepEqual(await requests.pending(), []);
    assert.deepEqual(await requests.request("/api/skill-sources"), { items: [], next_cursor: null });
    assert.equal(store.values.size, 0);
  }
});

test("uploaded bytes cannot enter session recovery storage", async () => {
  const store = storage();
  const requests = create({ storage: store, request: async () => assert.fail("Uploads must be server staged") });
  await assert.rejects(requests.request("/api/skill-source-installations", { ...init(), body: new Blob(["private bytes"]) }), /Stage uploaded bytes/);
  assert.equal(store.values.size, 0);
});

test("only the most recent same-key attempt may update pending operation state", async () => {
  const finish = [];
  const requests = create({ storage: storage(), request: () => new Promise(resolve => { finish.push(resolve); }) });
  const first = requests.request(path, init());
  while (finish.length < 1) await new Promise(resolve => setImmediate(resolve));
  const second = requests.request(path, init());
  while (finish.length < 2) await new Promise(resolve => setImmediate(resolve));
  finish[1]({ operation_id: "op", state: "complete", committed: true });
  await second;
  finish[0]({ operation_id: "op", state: "planning", committed: false });
  await assert.rejects(first, error => error.name === "StaleSourceResponseError");
  assert.deepEqual(await requests.pending(), []);
});


test("relocation recovery stores only a digest and looks up the same receipt after reload", async () => {
  const store = storage();
  const data = { destination_url: "https://private-fixture@github.com/org/repo", expected_source_generation: 2, expected_generations: [] };
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request("/api/skill-sources/s/relocate", init("relocate-key", data)), /Lost/);
  assert.ok(![...store.values.values()].join("").includes("private-fixture"));
  const [pending] = await lost.pending();
  assert.equal(pending.body, null); assert.equal(pending.requires_body, true);
  let seen;
  const resumed = create({ storage: store, request: async (url, options) => {
    seen = [url, options.method, options.body];
    return { operation_id: "relocated", source_id: "s", state: "complete", committed: true, outcomes: [] };
  } });
  await resumed.recover("relocate-key");
  assert.deepEqual(seen, ["/api/skill-source-operations?request_key=relocate-key", "GET", undefined]);
});


test("profile grants retain exact safe IDs and PUT key across reload, but reject unrelated PUTs", async () => {
  const store = storage(), payload = { users: ["person-1"], teams: ["team-1"] };
  const options = { ...init("grant-key", payload), method: "PUT" };
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request("/api/skill-source-profiles/profile-1/grants", options), /Lost/);
  const [pending] = await lost.pending(); assert.equal(pending.body, JSON.stringify(payload));
  let seen;
  const resumed = create({ storage: store, request: async (url, request) => {
    seen = [url, request.method, new Headers(request.headers).get("Idempotency-Key"), JSON.parse(request.body)];
    return { operation_id: "granted", state: "complete", committed: true, outcomes: [] };
  } });
  await resumed.recover("grant-key");
  assert.deepEqual(seen, ["/api/skill-source-profiles/profile-1/grants", "PUT", "grant-key", payload]);
  await assert.rejects(resumed.request("/api/settings", options), /Only source mutations/);
  await assert.rejects(lost.request("/api/skill-source-profiles/profile-1/grants", { ...options, headers: { "Idempotency-Key": "unsafe" }, body: JSON.stringify({ users: ["Bearer private-fixture"], teams: [] }) }));
  assert.equal((await lost.pending()).find(row => row.key === "unsafe").body, null);
});

test("a refused mutation resolves its terminal receipt and permits corrected input with a fresh key", async () => {
  const failure = new module.HttpError("package already owned", 409, "package_already_owned", { operation_id: "install-op" });
  const calls = [], store = storage();
  let credential = "first-fixture";
  const requests = create({ storage: store,
    context: (url, options) => {
      const headers = new Headers(options.headers); headers.set("Authorization", credential);
      return { path: `${url}?tenant_id=tenant-1`, init: { ...options, headers } };
    },
    request: async (url, options) => {
      calls.push([url, options.method, options.body, new Headers(options.headers).get("Authorization"), new Headers(options.headers).get("Idempotency-Key")]);
      if (calls.length === 1) { credential = "fresh-fixture"; throw failure; }
      return { operation_id: "install-op", state: "failed", committed: false,
        outcomes: [{ skill_id: "s", state: "failed", update_id: null, code: "package_already_owned" }] };
    },
  });
  await assert.rejects(requests.request(path, init()), error => error === failure);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].slice(0, 4), ["/api/skill-source-operations/install-op?tenant_id=tenant-1", "GET", undefined, "fresh-fixture"]);
  assert.deepEqual(await requests.pending(), []);
  assert.equal(store.values.size, 0);
  await requests.request(path, init("corrected-key", { ...body, skill_id: "different" }));
  assert.equal(calls[2][4], "corrected-key");
});

test("one inconclusive receipt lookup keeps the original error and pending request", async () => {
  const failure = new module.HttpError("package already owned", 409, "package_already_owned", { operation_id: "op" });
  for (const receipt of [
    { operation_id: "op", state: "planning", committed: false, outcomes: [] },
    { operation_id: "different", state: "failed", committed: false, outcomes: [] },
    { operation_id: "op", state: "failed", outcomes: [] },
    { operation_id: "op", state: "failed", committed: false, outcomes: null },
    { operation_id: "op", state: "failed", committed: false, outcomes: [null] },
    undefined,
    new TypeError("Lost receipt response"),
    new module.HttpError("operation not found", 404, "operation_not_found"),
    new module.HttpError("forbidden", 403, "source_action_forbidden"),
    new module.HttpError("unavailable", 503, "unavailable", { operation_id: "op" }),
  ]) {
    const calls = [];
    const requests = create({ storage: storage(), request: async (url, options) => {
      calls.push(options.method);
      if (calls.length === 1) throw failure;
      if (receipt instanceof Error) throw receipt;
      return receipt;
    } });
    await assert.rejects(requests.request(path, init()), error => error === failure);
    assert.deepEqual(calls, ["POST", "GET"]);
    const pending = await requests.pending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].operation_id, "op");
    await assert.rejects(requests.request(path, init("original-key", { ...body, skill_id: "other" })), /identity/i);
  }
});

test("a failed recovery read cannot discard the original mutation identity", async () => {
  for (const status of [403, 404]) {
    let calls = 0;
    const failure = new module.HttpError("receipt unavailable", status, "operation_not_found");
    const requests = create({ storage: storage(), request: async () => {
      if (++calls === 1) return { operation_id: "op", state: "planning", committed: false, outcomes: [] };
      throw failure;
    } });
    await requests.request(path, init());
    await assert.rejects(requests.recover("original-key"), error => error === failure);
    assert.equal((await requests.pending()).length, 1);
    assert.equal(calls, 2);
  }
});

test("receipt resolution notifies pending-state observers without making their failures request failures", async () => {
  const store = storage(), snapshots = [];
  let requests;
  requests = create({ storage: store, onChange: () => {
    snapshots.push(requests.pending());
    throw new Error("Observer failed");
  }, request: async (_, options) => {
    if (options.method === "POST") throw new module.HttpError("conflict", 409, "conflict", { operation_id: "op" });
    return { operation_id: "op", state: "failed", committed: false, outcomes: [] };
  } });
  await assert.rejects(requests.request(path, init()), /conflict/);
  const seen = await Promise.all(snapshots);
  assert.ok(seen.some(rows => rows.length === 1));
  assert.equal(seen.at(-1).length, 0);
  const beforeClear = snapshots.length;
  requests.clear();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(snapshots.length > beforeClear);
});

test("restoration and scope changes notify observers of recoverable state", async () => {
  const store = storage();
  const lost = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(lost.request(path, init()));
  let changes = 0;
  const resumed = create({ storage: store, onChange: () => { changes++; }, request: async () => {} });
  assert.equal((await resumed.pending()).length, 1);
  assert.ok(changes > 0);
  const restoredChanges = changes;
  resumed.setScope(null);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(changes > restoredChanges);
});

test("late receipt resolution is fenced after a session change or a newer same-key attempt", async () => {
  for (const replacement of ["session", "attempt"]) {
    let finish, started, calls = 0;
    const lookupStarted = new Promise(resolve => { started = resolve; });
    const requests = create({ storage: storage(), request: async (_, options) => {
      if (++calls === 1) throw new module.HttpError("conflict", 409, "conflict", { operation_id: "op" });
      if (options.method === "GET") return new Promise(resolve => { finish = resolve; started(); });
      return { operation_id: "op", state: "planning", committed: false, outcomes: [] };
    } });
    const first = requests.request(path, init()).catch(error => error);
    await Promise.race([lookupStarted, first]);
    assert.equal(typeof finish, "function");
    if (replacement === "session") requests.setScope({ ...scope, authSession: "replacement" });
    else await requests.request(path, init());
    finish({ operation_id: "op", state: "failed", committed: false, outcomes: [] });
    assert.ok(await first instanceof module.StaleSourceResponseError);
    assert.equal((await requests.pending()).length, replacement === "session" ? 0 : 1);
  }
});

test("an unresponsive receipt lookup returns the original refusal without losing recovery", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const failure = new module.HttpError("conflict", 409, "conflict", { operation_id: "op" });
  let signal, started;
  const lookupStarted = new Promise(resolve => { started = resolve; });
  const requests = create({ storage: storage(), request: async (_, options) => {
    if (options.method === "POST") throw failure;
    signal = options.signal;
    started();
    return new Promise(() => {});
  } });
  const flight = requests.request(path, init()).catch(error => error);
  await Promise.race([lookupStarted, flight]);
  assert.ok(signal);
  t.mock.timers.tick(6000);
  assert.equal(await flight, failure);
  assert.equal(signal.aborted, true);
  assert.equal((await requests.pending()).length, 1);
});

test("expired receipt context cannot start a late lookup", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const failure = new module.HttpError("conflict", 409, "conflict", { operation_id: "op" });
  let finish, started, lookups = 0;
  const lookupStarted = new Promise(resolve => { started = resolve; });
  const requests = create({ storage: storage(), context: (url, options) => {
    if (options.method !== "GET") return { path: url, init: options };
    return new Promise(resolve => { finish = () => resolve({ path: url, init: options }); started(); });
  }, request: async (_, options) => {
    if (options.method === "POST") throw failure;
    lookups++;
    return { operation_id: "op", state: "failed", committed: false, outcomes: [] };
  } });
  const flight = requests.request(path, init()).catch(error => error);
  await Promise.race([lookupStarted, flight]);
  assert.equal(typeof finish, "function");
  t.mock.timers.tick(6000);
  assert.equal(await flight, failure);
  finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(lookups, 0);
  assert.equal((await requests.pending()).length, 1);
});

test("a confirmed committed receipt returns the authoritative result after a mutation error", async () => {
  const committed = { operation_id: "op", state: "complete", committed: true,
    outcomes: [{ skill_id: "s", state: "applied", update_id: null, code: null }] };
  let writes = 0;
  const requests = create({ storage: storage(), request: async (_, options) => {
    if (options.method === "POST") { writes++; throw new module.HttpError("response failed", 500, "source_request_failed", { operation_id: "op" }); }
    return committed;
  } });
  assert.deepEqual(await requests.request(path, init()), committed);
  assert.deepEqual(await requests.pending(), []);
  assert.equal(writes, 1);
});

test("known-operation recovery retains identity when a receipt is malformed or belongs to another operation", async () => {
  for (const receipt of [
    { operation_id: "other-op", state: "complete", committed: true, outcomes: [] },
    { operation_id: "op", state: "complete", outcomes: [] },
    { operation_id: "op", state: "complete", committed: true, outcomes: [null] },
    { operation_id: "op", state: "planning", committed: true, outcomes: [] },
    undefined,
  ]) {
    const requests = create({ storage: storage(), request: async (_, options) => options.method === "POST"
      ? { operation_id: "op", state: "planning", committed: false, outcomes: [] } : receipt });
    await requests.request(path, init());
    await assert.rejects(requests.recover("original-key"), /invalid.*receipt/i);
    assert.equal((await requests.pending()).length, 1);
    assert.equal((await requests.pending())[0].operation_id, "op");
  }
});

test("key-reuse rejection cannot promote or recover a different payload's committed receipt", async () => {
  const committed = { operation_id: "prior-op", state: "complete", committed: true,
    outcomes: [{ skill_id: "s", state: "applied", update_id: null, code: null }] };
  const rejection = new module.HttpError("idempotency key reused", 409, "idempotency_key_reused", { operation_id: "prior-op" });
  let writes = 0, lookups = 0;
  const requests = create({ storage: storage(), request: async (_, options) => {
    if (options.method === "GET") { lookups++; return committed; }
    if (new Headers(options.headers).get("Idempotency-Key") === "unrelated") throw new TypeError("Lost other request");
    if (++writes === 1) return committed;
    throw rejection;
  } });
  await requests.request(path, init());
  await assert.rejects(requests.request(path, init("unrelated")), /Lost other request/);
  await assert.rejects(requests.request(path, init("original-key", { ...body, skill_id: "different" })), error => error === rejection);
  assert.deepEqual((await requests.pending()).map(row => row.key), ["unrelated"]);
  await assert.rejects(requests.recover("original-key"), /No pending source request/);
  assert.equal(lookups, 0);
  assert.equal(writes, 2);
});

test("simultaneous managers share pending records, order same-key responses and release closed observers", async () => {
  const store = storage(), events = [0, 0], finish = [];
  const first = create({ storage: store, onChange: () => events[0]++, request: async () => { throw new TypeError("Lost"); } });
  const second = create({ storage: store, onChange: () => events[1]++, request: () => new Promise(resolve => finish.push(resolve)) });
  await Promise.all([first.pending(), second.pending()]);
  await assert.rejects(first.request(path, init("lost")), /Lost/);
  const pending = second.request(path, init("other"));
  while (!finish.length) await new Promise(resolve => setImmediate(resolve));
  finish[0]({ operation_id: "other", state: "complete", committed: true, outcomes: [] });
  await pending;
  assert.deepEqual((await second.pending()).map(row => row.key), ["lost"]);
  first.close();
  await new Promise(resolve => setImmediate(resolve));
  const before = events[0];
  const recovered = second.recover("lost");
  while (finish.length < 2) await new Promise(resolve => setImmediate(resolve));
  finish[1]({ operation_id: "recovered", state: "complete", committed: true, outcomes: [] });
  await recovered;
  assert.equal(events[0], before);
  assert.ok(events[1] > 0);
  assert.deepEqual(await second.pending(), []);
  await assert.rejects(first.request(path, init("closed")), error => error.name === "StaleSourceResponseError");
  second.close();
});

test("scope changes invalidate peer managers and cannot be undone by their late responses", async () => {
  const store = storage(); let finish;
  const first = create({ storage: store, request: () => new Promise(resolve => { finish = resolve; }) });
  const second = create({ storage: store, request: async () => assert.fail("Obsolete peer sent request") });
  const flight = first.request(path, init());
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  const rejected = assert.rejects(flight, error => error.name === "StaleSourceResponseError");
  second.setScope(null);
  finish({ operation_id: "old", state: "planning", committed: false, outcomes: [] });
  await rejected;
  assert.deepEqual(await first.pending(), []);
  assert.equal(store.values.size, 0);
  await assert.rejects(first.request(path, init("late")), /session|obsolete/);
});

test("same-key responses are ordered across managers without sharing authority context", async () => {
  const store = storage(), finish = [], contexts = [];
  const make = name => create({ storage: store, context: (url, options) => { contexts.push(name); return { path: url, init: options }; }, request: () => new Promise(resolve => finish.push(resolve)) });
  const first = make("first"), second = make("second");
  const flight = first.request(path, init());
  while (!finish.length) await new Promise(resolve => setImmediate(resolve));
  const rejected = assert.rejects(flight, error => error.name === "StaleSourceResponseError");
  assert.equal((await second.pending())[0]?.key, "original-key");
  const retry = second.recover("original-key");
  while (finish.length < 2) await new Promise(resolve => setImmediate(resolve));
  finish[1]({ operation_id: "op", state: "complete", committed: true, outcomes: [] });
  await retry;
  finish[0]({ operation_id: "op", state: "planning", committed: false, outcomes: [] });
  await rejected;
  assert.deepEqual(contexts, ["first", "second"]);
  assert.deepEqual(await first.pending(), []);
  first.close(); second.close();
});

test("closing one view fences its late response while a peer can recover the original intent", async () => {
  const store = storage(); let finish;
  const first = create({ storage: store, request: () => new Promise(resolve => { finish = resolve; }) });
  const second = create({ storage: store, request: async () => ({ operation_id: "op", state: "complete", committed: true, outcomes: [] }) });
  const flight = first.request(path, init());
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  first.close();
  await second.recover("original-key");
  finish({ operation_id: "late", state: "planning", committed: false, outcomes: [] });
  await assert.rejects(flight, error => error.name === "StaleSourceResponseError");
  assert.deepEqual(await second.pending(), []);
  second.close();
});

test("obsolete managers cannot erase the replacement scope's pending records", async () => {
  const store = storage(), lost = async () => { throw new TypeError("Lost"); };
  const first = create({ storage: store, request: lost });
  await assert.rejects(first.request(path, init("old")));
  const second = create({ storage: store, request: lost, scope: { ...scope, authSession: "new" } });
  await assert.rejects(second.request(path, init("new")));
  first.setScope(null); first.clear(); first.close();
  second.close();
  const restored = create({ storage: store, request: lost, scope: { ...scope, authSession: "new" } });
  assert.deepEqual((await restored.pending()).map(row => row.key), ["new"]);
  restored.close();
});

test("private receipt reads share the same key ordering as an exact original-input retry", async () => {
  const store = storage(), draft = { ...body, choices: [{ part_id: "p", merged_text: "Private" }] };
  const first = create({ storage: store, request: async () => { throw new TypeError("Lost"); } });
  await assert.rejects(first.request("/api/skill-updates/u/draft", init("draft", draft)));
  let finish;
  const reader = create({ storage: store, request: () => new Promise(resolve => { finish = resolve; }) });
  const lookup = reader.recover("draft");
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  const retry = create({ storage: store, request: async () => ({ operation_id: "new", state: "planning", committed: false, outcomes: [] }) });
  await retry.recover("draft", { body: JSON.stringify(draft) });
  finish({ operation_id: "old", state: "complete", committed: true, outcomes: [] });
  await assert.rejects(lookup, error => error.name === "StaleSourceResponseError");
  assert.equal((await retry.pending())[0].operation_id, "new");
  first.close(); reader.close(); retry.close();
});

test("unreserved throttling does not leave a phantom operation after browser restoration", async () => {
  const store = storage(), calls = [];
  const transport = async (url, options) => {
    calls.push([url, options?.method, new Headers(options?.headers).get("Idempotency-Key")]);
    if (calls.length === 2) throw new module.HttpError("Check throttled", 429, "check_throttled", { operation_id: null, retry_after_seconds: 60 });
    return { operation_id: "check-" + calls.length, state: "complete", committed: false, outcomes: [] };
  };
  const first = create({ storage: store, request: transport });
  const check = "/api/skill-sources/s/check";
  const data = { expected_source_generation: 1, skill_ids: ["s"] };
  await first.request(check, init("first-check", data));
  await assert.rejects(first.request(check, init("throttled-check", data)), error => error.retry_after_seconds === 60);
  assert.deepEqual(await first.pending(), []);
  first.close();
  const restored = create({ storage: store, request: transport });
  assert.deepEqual(await restored.pending(), []);
  await restored.request(check, init("throttled-check", data));
  assert.deepEqual(calls.map(row => row[1]), ["POST", "POST", "POST"]);
  assert.deepEqual(calls.map(row => row[2]), ["first-check", "throttled-check", "throttled-check"]);
  restored.close();
});

test("shared invalidation notifies obsolete owners once but not the manager that resets its own scope", async () => {
  const store = storage(), notices = [];
  const first = create({ storage: store, request: async () => {}, onInvalidated: () => notices.push("first") });
  const peer = create({ storage: store, request: async () => {}, onInvalidated: () => { notices.push("peer"); throw new Error("Observer failed"); } });
  await Promise.all([first.pending(), peer.pending()]);
  first.clear();
  await Promise.resolve();
  assert.deepEqual(notices, ["peer"]);
  await assert.rejects(peer.request("/api/skill-sources"), error => error.name === "StaleSourceResponseError");
  first.clear(); peer.setScope(null);
  await Promise.resolve();
  assert.deepEqual(notices, ["peer"]);
  await first.request("/api/skill-sources");
  first.close(); peer.close();
});

test("closing an owner suppresses queued invalidation and leaves live peers usable", async () => {
  const store = storage(), notices = [];
  const first = create({ storage: store, request: async () => { throw new TypeError("Lost"); }, onInvalidated: () => notices.push("first") });
  const peer = create({ storage: store, request: async () => ({ operation_id: "op", state: "complete", committed: true, outcomes: [] }), onInvalidated: () => notices.push("peer") });
  await assert.rejects(first.request(path, init("pending")), /Lost/);
  first.close();
  await peer.recover("pending");
  assert.deepEqual(notices, []);
  const retired = create({ storage: store, request: async () => {}, onInvalidated: () => notices.push("retired") });
  peer.setScope(null);
  retired.close();
  await Promise.resolve();
  assert.deepEqual(notices, []);
  peer.close();
});
