const STORAGE_KEY = "oms.source-requests.v1";
const MAX_BYTES = 65536;
// A storage area holds one active authority scope. Views share records, never transports.
const coordinators = new WeakMap();
const text = value => typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\x00-\x1f\x7f]/.test(value);
const generation = value => Number.isSafeInteger(value) && value >= 0;
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const shape = (value, fields) => object(value) && Object.entries(value).every(([key, item]) => Object.hasOwn(fields, key) && fields[key](item) === true);
const list = check => value => Array.isArray(value) && value.length <= 10000 && value.every(check);
const terminalStates = ["complete", "awaiting_review", "blocked", "failed"];
function operationReceipt(value, expectedId = null) {
  return object(value) && text(value.operation_id) && (!expectedId || value.operation_id === expectedId)
    && typeof value.committed === "boolean" && (terminalStates.includes(value.state)
      || (!value.committed && ["fetching", "planning", "applying"].includes(value.state)))
    && Array.isArray(value.outcomes) && value.outcomes.every(row => object(row) && text(row.skill_id)
      && ["applied", "awaiting_review", "blocked", "failed", "unchanged"].includes(row.state)
      && (row.update_id === null || text(row.update_id)) && (row.code === null || text(row.code)));
}
const fingerprint = value => shape(value, {
  format_version: value => value === 1, content_generation: generation, binding_generation: generation,
  update_generation: generation, policy_version: text, local_digest: text, candidate_digest: text,
  policy_digest: value => value === null || text(value),
});
const guards = value => shape(value, { skill_id: text, content: generation, binding: generation });
const safeFields = {
  skill_id: text, fingerprint, removal_consents: list(text), source_id: text,
  expected_content_generation: generation, expected_binding_generation: generation,
  expected_source_generation: generation, skill_ids: list(text), expected_generations: list(guards),
  enabled: value => typeof value === "boolean", undo_id: text, policy_version: text,
};
// An edition may retain further mutations. They only widen the pathname match; path hygiene still applies.
const extraRoute = (extra, pathname, method) => extra.find(route => route.method === method && route.pathname(pathname) === true);
function mutationPath(path, method, extra = []) {
  if (typeof path !== "string") return false;
  const [pathname, search = ""] = path.split("?");
  if (/[#\\\x00-\x20\x7f]/.test(path) || /%(?:25|2e|5c)/i.test(pathname)) return false;
  if ([...new URLSearchParams(search).keys()].some(key => !["tenant", "tenant_id"].includes(key))) return false;
  const id = "[^/?.]+";
  if (method === "PUT" && new RegExp(`^/api/skill-source-profiles/${id}/grants$`).test(pathname)) return true;
  if (method === "DELETE" && new RegExp(`^/api/skill-source-discoveries/${id}$`).test(pathname)) return true;
  return !!extraRoute(extra, pathname, method) || method === "POST" && (pathname === "/api/skill-source-discoveries" || pathname === "/api/skill-source-installations"
    || pathname === "/api/skill-sources" || pathname === "/api/skill-local-imports"
    || pathname === "/api/skill-updates/bulk-apply"
    || new RegExp(`^/api/skill-sources/${id}/(check|schedule|remove|relocate)$`).test(pathname)
    || new RegExp(`^/api/skills/${id}/source-binding/(link|relink|retarget|unlink|automation)$`).test(pathname)
    || new RegExp(`^/api/skill-updates/${id}/(draft|recheck|apply|skip|adopt|undo)$`).test(pathname));
}
function safeBody(path, body, method, extra = []) {
  if (body === null) return true;
  if (typeof body !== "string" || body.length > 16384) return false;
  const pathname = path.split("?")[0];
  if (/discoveries|\/(draft|link|relink|retarget|relocate)$/.test(pathname)) return false;
  let parsed;
  try { parsed = JSON.parse(body); } catch { return false; }
  if (/^\/api\/skill-source-profiles\/[^/]+\/grants$/.test(pathname)) {
    const identifiers = value => Array.isArray(value) && value.length <= 1000 && value.every(item => typeof item === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(item));
    return object(parsed) && Object.keys(parsed).length === 2 && shape(parsed, { users: identifiers, teams: identifiers });
  }
  if (pathname === "/api/skill-source-installations") return shape(parsed, { discovery_id: text,
    selections: list(value => shape(value, { package_path: value => value === "" || text(value), local_name: text, domain: text })) });
  if (pathname === "/api/skill-sources") return shape(parsed, { discovery_id: text });
  if (pathname === "/api/skill-local-imports") return shape(parsed, { upload_id: text,
    selections: list(value => shape(value, { package_path: value => value === "" || text(value),
      target_skill_id: text, local_name: text, domain: text, removal_consents: list(text) })) });
  const route = extraRoute(extra, pathname, method);
  if (route?.body) return route.body(parsed) === true;
  return pathname.endsWith("/bulk-apply")
    ? shape(parsed, { updates: list(value => shape(value, { ...safeFields, update_id: text })) })
    : shape(parsed, safeFields);
}
// A queued approval answers with its receipt beside the decided record.
const receiptOf = value => object(value) && !("state" in value) && object(value.operation) ? value.operation : value;
function identity(scope) {
  if (scope === null) return null;
  if (!scope || !text(scope.server) || !text(scope.workspace) || !text(scope.authSession)) throw new Error("A server, workspace and authentication session identity are required.");
  const server = new URL(scope.server);
  if (!["http:", "https:"].includes(server.protocol) || server.username || server.password || server.search || server.hash) throw new Error("Invalid source request server identity.");
  return { server: server.href.replace(/\/+$/, ""), workspace: scope.workspace, authSession: scope.authSession };
}
async function digest(path, method, body) {
  const data = new TextEncoder().encode(JSON.stringify([path, method, body]));
  const hash = await globalThis.crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}
export class StaleSourceResponseError extends Error {
  constructor() { super("The source request belongs to an obsolete session or view."); this.name = "StaleSourceResponseError"; }
}

/** Supply a transport that refreshes edition credentials on every call, including recovery. */
export function createSourceRequests({ request, requestBytes, context = (path, init) => ({ path, init }), scope, storage, onChange, onInvalidated, mutations = [] }) {
  const extra = mutations.filter(route => ["POST", "PUT", "DELETE"].includes(route?.method) && typeof route.pathname === "function");
  let active = identity(scope);
  let epoch = 0, closed = false, invalidationNotified = false;
  if (storage === undefined) {
    try { storage = globalThis.sessionStorage; } catch { /* Retain in memory. */ }
  }
  const notify = () => {
    if (onChange || onInvalidated) queueMicrotask(() => {
      if (closed) return;
      // A local reset rejoins a current coordinator before observers run.
      if (state.obsolete && !invalidationNotified) {
        invalidationNotified = true;
        try { onInvalidated?.(); } catch { /* Observers cannot change request outcomes. */ }
      }
      if (!closed) { try { onChange?.(); } catch { /* Observers cannot change request outcomes. */ } }
    });
  };
  function invalidate(state) {
    if (state.obsolete) return;
    state.obsolete = true; state.records.clear(); state.attempts.clear();
    try { if (storage && coordinators.get(storage) === state) storage.removeItem(STORAGE_KEY); } catch { state.persistent = false; }
    state.observers.forEach(observer => observer());
  }
  async function restore(state) {
    const discard = () => {
      if (state.obsolete) return;
      try { storage?.removeItem(STORAGE_KEY); } catch { state.persistent = false; }
    };
    let saved;
    try {
      const raw = storage?.getItem(STORAGE_KEY);
      if (!raw) return;
      if (raw.length > MAX_BYTES) { discard(); return; }
      saved = JSON.parse(raw);
    } catch { state.persistent = false; discard(); return; }
    if (!state.scope || !shape(saved, { version: value => value === 1, scope: object, requests: Array.isArray })
        || saved.version !== 1 || JSON.stringify(saved.scope) !== JSON.stringify(state.scope)
        || !Array.isArray(saved.requests) || saved.requests.length > 40) { discard(); return; }
    const restored = new Map();
    for (const row of saved.requests) {
      if (!shape(row, { key: text, path: text, method: text, digest: value => /^[a-f0-9]{64}$/.test(value),
        body: value => value === null || typeof value === "string", requires_body: value => typeof value === "boolean",
        operation_id: value => value === null || text(value) })
        || !text(row.key) || !mutationPath(row.path, row.method, extra) || !/^[a-f0-9]{64}$/.test(row.digest)
        || typeof row.requires_body !== "boolean" || !(row.operation_id === null || text(row.operation_id))
        || (row.requires_body ? row.body !== null : !safeBody(row.path, row.body, row.method, extra))
        || (!row.requires_body && await digest(row.path, row.method, row.body) !== row.digest)
        || restored.has(row.key)) { discard(); return; }
      restored.set(row.key, row);
    }
    if (!state.obsolete) {
      restored.forEach((row, key) => state.records.set(key, row));
      state.observers.forEach(observer => observer());
    }
  }
  function join() {
    let state = active && storage && coordinators.get(storage);
    if (state && (state.obsolete || JSON.stringify(state.scope) !== JSON.stringify(active))) {
      if (!state.obsolete) invalidate(state);
      state = null;
    }
    if (!state) {
      state = { scope: active, records: new Map(), attempts: new Map(), observers: new Set(), obsolete: false, persistent: !!storage };
      if (storage && active) coordinators.set(storage, state);
      state.ready = active ? restore(state) : Promise.resolve();
    }
    state.observers.add(notify);
    void state.ready.then(() => { if (state.observers.has(notify)) notify(); });
    return state;
  }
  let state = join();
  let records = state.records, attempts = state.attempts, ready = state.ready;
  function leave() {
    state.observers.delete(notify);
    if (!state.observers.size) {
      state.obsolete = true;
      if (storage && coordinators.get(storage) === state) coordinators.delete(storage);
    }
  }
  function changed() { state.observers.forEach(observer => observer()); }
  function persist() {
    if (state.obsolete) throw new StaleSourceResponseError();
    const saved = records.size && active ? JSON.stringify({ version: 1, scope: active, requests: [...records.values()] }) : null;
    if (saved && saved.length > MAX_BYTES) throw new Error("Too many pending source requests. Resolve existing requests first.");
    try { if (saved) storage?.setItem(STORAGE_KEY, saved); else storage?.removeItem(STORAGE_KEY); }
    catch { state.persistent = false; }
    changed();
  }
  function assertCurrent(captured) {
    if (closed || captured !== epoch || state.obsolete) throw new StaleSourceResponseError();
    if (!active) throw new Error("No active source request session.");
  }
  function recordResult(key, response) {
    const row = records.get(key), result = receiptOf(response);
    if (!row) return;
    if (["fetching", "planning", "applying"].includes(result?.state)) {
      if (text(result.operation_id)) records.set(key, { ...row, operation_id: result.operation_id });
    } else if (["complete", "awaiting_review", "blocked", "failed"].includes(result?.state)
        || result === undefined || text(result?.discovery_id)) records.delete(key);
    persist();
  }
  async function perform(path, init, key, captured) {
    const attempt = Symbol();
    if (key) attempts.set(key, attempt);
    const current = () => {
      assertCurrent(captured);
      if (key && attempts.get(key) !== attempt) throw new StaleSourceResponseError();
    };
    const mutation = !["GET", "HEAD"].includes((init?.method ?? "GET").toUpperCase());
    try {
      const scoped = await context(path, init);
      current();
      const result = await request(scoped.path, scoped.init);
      current();
      if (key && !mutation && !operationReceipt(result, records.get(key)?.operation_id)) {
        throw new Error("The server returned an invalid source operation receipt. Retry recovery.");
      }
      if (key) recordResult(key, result);
      return result;
    } catch (error) {
      current();
      if (key && mutation && records.has(key)) {
        // This receipt belongs to another payload; the current mutation was rejected.
        if (error?.status === 409 && error.code === "idempotency_key_reused") records.delete(key);
        else if (text(error?.operation_id)) {
          records.set(key, { ...records.get(key), operation_id: error.operation_id });
          persist();
          const controller = new AbortController();
          let timer;
          try {
            const lookup = async () => {
              const scoped = await context(`/api/skill-source-operations/${encodeURIComponent(error.operation_id)}`,
                { ...init, method: "GET", body: undefined, signal: controller.signal });
              current();
              return request(scoped.path, scoped.init);
            };
            const receipt = await Promise.race([lookup(), new Promise((_, reject) => {
              timer = setTimeout(() => reject(new Error("Source receipt lookup timed out.")), 5000);
            })]);
            current();
            if (operationReceipt(receipt, error.operation_id) && terminalStates.includes(receipt.state)) {
              recordResult(key, receipt);
              if (receipt.committed) return receipt;
            }
          } catch { current(); /* An unavailable receipt does not prove the mutation failed. */ }
          finally { clearTimeout(timer); controller.abort(); }
        }
        else if (Number.isInteger(error?.status) && error.status >= 400 && error.status < 500) records.delete(key);
        persist();
      }
      throw error;
    } finally {
      if (key && attempts.get(key) === attempt) attempts.delete(key);
    }
  }
  async function send(path, init) {
    const captured = epoch;
    await ready;
    assertCurrent(captured);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" || method === "HEAD") return perform(path, init, null, captured);
    if (!mutationPath(path, method, extra)) throw new Error("Only source mutations support request recovery.");
    const key = new Headers(init?.headers).get("Idempotency-Key");
    if (!text(key) || /\s/.test(key)) throw new Error("An idempotency key is required.");
    const body = init?.body ?? null;
    if (body !== null && typeof body !== "string") throw new Error("Stage uploaded bytes on the server before retaining a source request.");
    const hash = await digest(path, method, body);
    assertCurrent(captured);
    const existing = records.get(key);
    if (existing && existing.digest !== hash) throw new Error("The original request identity does not match.");
    if (!existing && records.size >= 40) throw new Error("Too many pending source requests. Resolve existing requests first.");
    const safe = safeBody(path, body, method, extra);
    records.set(key, existing ?? { key, path, method, digest: hash, body: safe ? body : null, requires_body: !safe, operation_id: null });
    persist();
    return perform(path, init, key, captured);
  }
  return {
    request: send,
    requestBytes: async (path, init) => {
      const captured = epoch;
      assertCurrent(captured);
      if (!requestBytes) throw new Error("A byte transport is required.");
      try {
        const scoped = await context(path, init);
        assertCurrent(captured);
        const result = await requestBytes(scoped.path, scoped.init);
        assertCurrent(captured);
        return result;
      } catch (error) { assertCurrent(captured); throw error; }
    },
    get persistent() { return state.persistent; },
    pending: async () => { const captured = epoch; await ready; if (closed || captured !== epoch) throw new StaleSourceResponseError(); return [...records.values()].map(row => Object.freeze({ ...row })); },
    recover: async (key, init) => {
      const captured = epoch;
      await ready;
      assertCurrent(captured);
      const row = records.get(key);
      if (!row) throw new Error("No pending source request has this key.");
      if (row.operation_id) return perform(`/api/skill-source-operations/${encodeURIComponent(row.operation_id)}`, { ...init, method: "GET", body: undefined }, key, captured);
      if (row.requires_body && typeof init?.body !== "string") {
        try {
          const result = await perform(`/api/skill-source-operations?request_key=${encodeURIComponent(key)}`,
            { ...init, method: "GET", body: undefined }, key, captured);
          assertCurrent(captured);
          return result;
        } catch (error) {
          if (error?.status !== 404) throw error;
          throw new Error("No receipt is recorded yet. Retry the lookup or supply the exact original request body.");
        }
      }
      const headers = new Headers(init?.headers);
      headers.set("Idempotency-Key", key);
      const body = init?.body ?? row.body;
      if (body !== null) headers.set("Content-Type", "application/json");
      return receiptOf(await send(row.path, { ...init, method: row.method, headers, ...(body === null ? {} : { body }) }));
    },
    setScope: next => {
      const replacement = identity(next);
      if (JSON.stringify(active) === JSON.stringify(replacement)) return;
      if (closed || state.obsolete) return;
      epoch++; invalidate(state); leave(); active = replacement; state = join();
      records = state.records; attempts = state.attempts; ready = state.ready; changed();
    },
    clear: () => {
      if (closed || state.obsolete) return;
      epoch++; invalidate(state); leave(); state = join();
      records = state.records; attempts = state.attempts; ready = state.ready; changed();
    },
    close: () => { if (!closed) { closed = true; epoch++; leave(); } },
  };
}
