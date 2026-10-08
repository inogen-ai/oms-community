import test from "node:test";
import assert from "node:assert/strict";
import * as clientModule from "../src/index.js";
const { createClient, HttpError } = clientModule;

test("a 202 response remains in progress", async () => {
  const client = createClient({ fetch: async () => Response.json({ operation_id: "op-1", state: "planning", committed: false }, { status: 202 }) });
  assert.deepEqual(await client.request("/api/skill-source-operations/op-1"), { operation_id: "op-1", state: "planning", committed: false });
});
test("byte transport preserves authentication and metadata without JSON decoding", async () => {
  const seen = [];
  const client = createClient({ requestPolicy: "browser", fetch: async (path, init) => {
    seen.push({ path, init });
    return new Response(new Uint8Array([0, 255, 13]), { headers: { "Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="fixture.bin"' } });
  } });
  assert.equal(typeof client.requestBytes, "function");
  const result = await client.requestBytes("/api/skill-updates/u/files?path=fixture.bin&preview=false", { headers: { Authorization: "Bearer fixture" } });
  assert.deepEqual([...result.bytes], [0, 255, 13]);
  assert.equal(result.content_disposition, 'attachment; filename="fixture.bin"');
  assert.equal(result.content_type, "application/octet-stream");
  assert.equal(new Headers(seen[0].init.headers).get("Authorization"), "Bearer fixture");
});
test("both transports reject URL escapes before fetch and retain local security policy", async () => {
  let calls = 0;
  const client = createClient({ fetch: async (_, init) => {
    calls++;
    assert.equal(init.credentials, "omit");
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    return new Response("bytes");
  } });
  assert.equal(typeof client.requestBytes, "function");
  for (const path of ["//evil.test/api/x", "/api/../private", "/api/%2e%2e/private", "/api/x\\y", "/api/x#fragment", "/api/%252e%252e/private", "/api/x\n"]) {
    await assert.rejects(client.requestBytes(path));
    await assert.rejects(client.request(path));
  }
  assert.equal(calls, 0);
  await client.requestBytes("/api/files?path=a%20b", { credentials: "include", redirect: "follow" });
  assert.equal(calls, 1);
});
test("structured retry and operation metadata coexist with legacy errors", async () => {
  for (const body of [{ code: "busy", message: "Try later", operation_id: "op-1", retry_after_seconds: 9 }, { error: { code: "busy", message: "Try later", operation_id: "op-1", retry_after_seconds: 9 } }, { detail: { code: "busy", message: "Try later", operation_id: "op-1", retry_after_seconds: 9 } }]) {
    const client = createClient({ fetch: async () => Response.json(body, { status: 429, headers: { "Retry-After": "12" } }) });
    await assert.rejects(client.request("/api/skill-sources"), error => {
      assert.ok(error instanceof HttpError);
      assert.equal(error.operation_id, "op-1");
      assert.equal(error.retry_after_seconds, 9);
      assert.equal(error.code, "busy");
      return true;
    });
  }
  const client = createClient({ fetch: async () => Response.json({ error: { code: 42, message: "Old error" } }, { status: 429, headers: { "Retry-After": "12" } }) });
  await assert.rejects(client.request("/api/skill-sources"), error => error.code === 42 && error.retry_after_seconds === 12 && error.operation_id === null);
});
test("source facade injects scoped path and headers for reads, mutations and bytes", async () => {
  assert.equal(typeof clientModule.createSourcesClient, "function");
  const calls = [];
  const transport = createClient({ requestPolicy: "browser", fetch: async (path, init) => {
    calls.push({ path, init });
    return path.includes("preview=false") ? new Response("file") : Response.json({ state: "awaiting_review", committed: false });
  } });
  const client = clientModule.createSourcesClient({ ...transport, context: async (path, init) => ({
    path: `${path}${path.includes("?") ? "&" : "?"}tenant_id=tenant-1`,
    init: { ...init, headers: { ...Object.fromEntries(new Headers(init?.headers)), Authorization: "Bearer fixture" } },
  }) });
  await client.sources({ cursor: "next + page", limit: 4 });
  const result = await client.apply("u/1", { skill_id: "s", fingerprint: {} }, "request-1");
  await client.updateFile("u/1", { side: "upstream", path: "docs/a b.txt" });
  assert.equal(result.state, "awaiting_review");
  assert.equal(result.committed, false);
  assert.equal(calls[0].path, "/api/skill-sources?cursor=next+%2B+page&limit=4&tenant_id=tenant-1");
  assert.equal(calls[1].path, "/api/skill-updates/u%2F1/apply?tenant_id=tenant-1");
  assert.equal(new Headers(calls[1].init.headers).get("Idempotency-Key"), "request-1");
  assert.equal(new Headers(calls[2].init.headers).get("Authorization"), "Bearer fixture");
  assert.match(calls[2].path, /side=upstream&path=docs%2Fa\+b.txt&preview=false/);
});
test("source file paths and initiating keys are checked before network access", async () => {
  assert.equal(typeof clientModule.createSourcesClient, "function");
  let calls = 0;
  const client = clientModule.createSourcesClient({ request: async () => { calls++; }, requestBytes: async () => { calls++; } });
  for (const path of ["../secret", "/absolute", "a/../secret", "a\\b", "a//b", "a/./b", "a\0b"]) {
    await assert.rejects(async () => client.updateFile("u", { side: "local", path }));
    await assert.rejects(async () => client.discoveryFile("d", { package_path: "", path }));
  }
  await assert.rejects(async () => client.apply("u", {}, ""));
  await assert.rejects(async () => client.updateFile("u", { side: "invalid", path: "file" }));
  assert.equal(calls, 0);
});

test("source facade invalidation rejects old context and delayed downloads", async () => {
  let finish;
  const client = clientModule.createSourcesClient({ request: async () => assert.fail("invalidated context reached network"), requestBytes: async () => {},
    context: () => new Promise(resolve => { finish = resolve; }) });
  const flight = client.sources();
  client.invalidate();
  finish({ path: "/api/skill-sources" });
  await assert.rejects(flight, error => error.name === "StaleSourceResponseError");
});

test("source routes preserve guarded bodies and caller keys across every mutation", async () => {
  const calls = [];
  const response = { operation_id: "op", state: "blocked", committed: false, outcomes: [{ skill_id: "s", state: "blocked", code: "stale", update_id: null }] };
  const transport = createClient({ fetch: async (url, init) => { calls.push({ url, init }); return Response.json(response); } });
  const client = clientModule.createSourcesClient(transport);
  const body = { expected_content_generation: 2, expected_binding_generation: 3 };
  const routes = [
    ["discover", "/api/skill-source-discoveries"], ["install", "/api/skill-source-installations"],
    ["check", "/api/skill-sources/id/check"], ["schedule", "/api/skill-sources/id/schedule"], ["removeSource", "/api/skill-sources/id/remove"], ["relocateSource", "/api/skill-sources/id/relocate"],
    ...["link", "relink", "retarget", "unlink", "automation"].map(name => [name, `/api/skills/id/source-binding/${name}`]),
    ["saveDraft", "/api/skill-updates/id/draft"],
    ...["recheck", "apply", "skip", "adopt", "undo"].map(name => [name, `/api/skill-updates/id/${name}`]),
    ["bulkApply", "/api/skill-updates/bulk-apply"],
  ];
  for (const [method, url] of routes) {
    const noId = ["discover", "install", "bulkApply"].includes(method);
    const result = await client[method](...(noId ? [body, method] : ["id", body, method]));
    assert.equal(result.state, "blocked");
    assert.equal(result.committed, false);
    const call = calls.at(-1);
    assert.equal(call.url, url);
    assert.equal(call.init.method, "POST");
    assert.equal(new Headers(call.init.headers).get("Idempotency-Key"), method);
    assert.deepEqual(JSON.parse(call.init.body), body);
  }
});

test("read routes preserve empty pages, binding absence, previews and staging deletion", async () => {
  const calls = [];
  const preview = { path: "SKILL.md", side: "discovery", size: 90000, binary: false, text: "bounded", truncated: true, byte_limit: 65536, line_limit: 1000, media_type: "text/plain" };
  const client = clientModule.createSourcesClient(createClient({ fetch: async (url, init) => {
    calls.push({ url, init });
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (url.includes("preview=true")) return Response.json(preview);
    return Response.json({ items: [], next_cursor: null });
  } }));
  for (const [method, expected] of [["discovery", "/api/skill-source-discoveries/id"], ["source", "/api/skill-sources/id"], ["binding", "/api/skills/id/source-binding"], ["update", "/api/skill-updates/id"], ["operation", "/api/skill-source-operations/id"], ["history", "/api/skills/id/source-history"]]) {
    assert.deepEqual(await client[method]("id"), { items: [], next_cursor: null });
    assert.equal(calls.at(-1).url, expected);
  }
  await client.updates({ limit: 5 });
  assert.equal(calls.at(-1).url, "/api/skill-updates?limit=5");
  assert.deepEqual(await client.discoveryFile("id", { package_path: "", path: "SKILL.md", preview: true }), preview);
  assert.equal(calls.at(-1).url, "/api/skill-source-discoveries/id/files?package_path=&path=SKILL.md&preview=true");
  assert.equal(await client.discardDiscovery("id", "discard-key"), undefined);
  assert.equal(calls.at(-1).init.method, "DELETE");
  assert.equal(calls.at(-1).init.body, undefined);
});

test("safe encoded legacy identifiers remain usable through both transports", async () => {
  const calls = [];
  const client = createClient({ fetch: async url => { calls.push(url); return Response.json({}); } });
  await client.skill("100% verified");
  await client.requestBytes("/api/files/a%2Eb%25file");
  assert.deepEqual(calls, ["/api/skills/100%25%20verified", "/api/files/a%2Eb%25file"]);
});
