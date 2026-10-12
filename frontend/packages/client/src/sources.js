import { StaleSourceResponseError } from "./source-requests.js";

const segment = (value) => {
  if (typeof value !== "string" || !value || value === "." || value === "..") throw new Error("A source resource ID is required.");
  return encodeURIComponent(value);
};
const query = (values = {}) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null) params.set(key, String(value));
  return params.size ? `?${params}` : "";
};
function relativePath(value, allowRoot = false) {
  if (allowRoot && value === "") return value;
  if (typeof value !== "string" || /[\\\x00-\x1f\x7f]/.test(value)
      || value.split("/").some(part => ["", ".", ".."].includes(part))) {
    throw new Error("A file must use a safe manifest-relative path.");
  }
  return value;
}

/** Edition admission is injected for every request, including byte downloads. */
export function createSourcesClient({ request, requestBytes, context = (path, init) => ({ path, init }) }) {
  let epoch = 0;
  async function call(path, init, bytes = false) {
    const captured = epoch;
    const current = () => { if (captured !== epoch) throw new StaleSourceResponseError(); };
    try {
      const scoped = await context(path, init);
      current();
      const result = await (bytes ? requestBytes : request)(scoped.path, scoped.init);
      current();
      return result;
    } catch (error) { current(); throw error; }
  }
  function send(path, body, key, method = "POST") {
    if (typeof key !== "string" || !key.trim() || /[\x00-\x20\x7f]/.test(key)) throw new Error("An idempotency key is required.");
    return call(path, { method, headers: { "Idempotency-Key": key, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  }
  const source = id => `/api/skill-sources/${segment(id)}`;
  const binding = id => `/api/skills/${segment(id)}/source-binding`;
  const update = id => `/api/skill-updates/${segment(id)}`;
  return {
    invalidate: () => { epoch++; },
    workspace: () => call("/api/skill-source-workspace"),
    discover: (body, key) => send("/api/skill-source-discoveries", body, key),
    discovery: (id, page) => call(`/api/skill-source-discoveries/${segment(id)}${query(page)}`),
    discardDiscovery: (id, key) => send(`/api/skill-source-discoveries/${segment(id)}`, undefined, key, "DELETE"),
    install: (body, key) => send("/api/skill-source-installations", body, key),
    createSource: (body, key) => send("/api/skill-sources", body, key),
    installLocal: (body, key) => send("/api/skill-local-imports", body, key),
    sources: page => call(`/api/skill-sources${query(page)}`),
    source: (id, page) => call(`${source(id)}${query(page)}`),
    check: (id, body, key) => send(`${source(id)}/check`, body, key),
    schedule: (id, body, key) => send(`${source(id)}/schedule`, body, key),
    relocateSource: (id, body, key) => send(`${source(id)}/relocate`, body, key),
    removeSource: (id, body, key) => send(`${source(id)}/remove`, body, key),
    binding: id => call(binding(id)),
    link: (id, body, key) => send(`${binding(id)}/link`, body, key),
    relink: (id, body, key) => send(`${binding(id)}/relink`, body, key),
    retarget: (id, body, key) => send(`${binding(id)}/retarget`, body, key),
    unlink: (id, body, key) => send(`${binding(id)}/unlink`, body, key),
    automation: (id, body, key) => send(`${binding(id)}/automation`, body, key),
    updates: page => call(`/api/skill-updates${query(page)}`),
    update: id => call(update(id)),
    saveDraft: (id, body, key) => send(`${update(id)}/draft`, body, key),
    recheck: (id, body, key) => send(`${update(id)}/recheck`, body, key),
    apply: (id, body, key) => send(`${update(id)}/apply`, body, key),
    skip: (id, body, key) => send(`${update(id)}/skip`, body, key),
    adopt: (id, body, key) => send(`${update(id)}/adopt`, body, key),
    undo: (id, body, key) => send(`${update(id)}/undo`, body, key),
    bulkApply: (body, key) => send("/api/skill-updates/bulk-apply", body, key),
    operation: id => call(`/api/skill-source-operations/${segment(id)}`),
    operationForKey: request_key => call(`/api/skill-source-operations${query({ request_key })}`),
    history: (id, page) => call(`/api/skills/${segment(id)}/source-history${query(page)}`),
    discoveryFile: (id, { package_path, path, preview = false }) => call(
      `/api/skill-source-discoveries/${segment(id)}/files${query({ package_path: relativePath(package_path, true), path: relativePath(path), preview })}`, undefined, !preview),
    updateFile: (id, { side, path, preview = false }) => {
      if (!["base", "local", "upstream"].includes(side)) throw new Error("Unknown source file side.");
      return call(`${update(id)}/files${query({ side, path: relativePath(path), preview })}`, undefined, !preview);
    },
  };
}

const acknowledgedKey = choice => [...(choice.acknowledged ?? [])].sort().join(",");
const sameChoice = (a, b) => a.part_id === b.part_id && a.part_fingerprint === b.part_fingerprint && a.choice === b.choice
  && (a.merged_text ?? null) === (b.merged_text ?? null) && acknowledgedKey(a) === acknowledgedKey(b);
// Order-free, as the server may return the saved choices in another order.
const sameChoices = (a, b) => a.length === b.length && a.every(choice => b.some(other => sameChoice(choice, other)));

/**
 * The review card's one primary action (spec D3). Saves the reviewer's choices when they differ
 * from the saved draft, reads the card back, and applies only when it is current, still holds
 * exactly the choices this reviewer saved (drafts are one shared record, ruling T6-2) and its
 * saved selection passed the required checks. Otherwise it stops and calls
 * `options.onStopped(reason)` with "unavailable" (the read-back failed), "stale", "changed"
 * (someone else saved other choices) or "held". A failed save is not a stop: the host's `run`
 * reports it with its recovery. Each request runs through `run`, so each keeps its own key.
 */
export async function applyReviewedChoices(client, run, updateId, body, { onStopped } = {}) {
  let fingerprint = body.fingerprint;
  if (body.save) {
    const draft = { skill_id: body.skill_id, fingerprint: body.fingerprint, choices: body.choices };
    const saved = await run(key => client.saveDraft(updateId, draft, key), JSON.stringify(["draft", updateId, draft]));
    if (!saved || saved.state === "failed") return null;
    let current = null;
    try { current = await client.update(updateId); } catch { /* Reported below as "unavailable". */ }
    const reason = !current ? "unavailable" : current.stale ? "stale" : !sameChoices(current.drafts ?? [], body.choices) ? "changed"
      : current.resolved_check?.state !== "passed" ? "held" : null;
    if (reason) { onStopped?.(reason); return null; }
    fingerprint = current.plan.fingerprint;
  }
  const apply = { skill_id: body.skill_id, fingerprint, removal_consents: body.removal_consents };
  return run(key => client.apply(updateId, apply, key), JSON.stringify(["apply", updateId, apply]));
}
