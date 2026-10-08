import { HttpError, StaleSourceResponseError, createSourceRequests, createSourcesClient, type CommunityClient, type SourceRequestContext, type SourceRequests, type SourcesClient, type SourcePage } from "@inogen/oms-client";

export interface CommunitySources { client: SourcesClient; requests: SourceRequests; scope: string }

export const communitySourceContext: SourceRequestContext = (path, init) => {
  const url = new URL(path, "https://community.invalid");
  const headers = new Headers(init?.headers);
  if (url.searchParams.has("tenant_id") || url.searchParams.has("tenant") || headers.has("X-Tenant-ID") || headers.has("tenant_id")) {
    throw new Error("Community source requests cannot override the local workspace.");
  }
  if (typeof init?.body === "string") {
    const body: unknown = JSON.parse(init.body);
    if (body && typeof body === "object" && ("tenant_id" in body || "tenant" in body)) throw new Error("Community source requests cannot override the local workspace.");
  }
  return { path, init };
};

function verifiedWorkspace(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error("The server did not provide a valid source workspace identity.");
  }
  return value;
}

export function createCommunitySources(api: CommunityClient, baseUrl: string, workspaceValue: unknown, onWorkspaceChanged: () => void): CommunitySources {
  const workspace = verifiedWorkspace(workspaceValue);
  const probe = createSourcesClient({ ...api, context: communitySourceContext });
  let obsolete = false;
  const workspaceChanged = () => {
    if (!obsolete) { obsolete = true; onWorkspaceChanged(); }
  };
  async function guarded<T>(send: () => Promise<T>): Promise<T> {
    try { return await send(); }
    catch (failure) {
      if (failure instanceof HttpError && failure.status === 409 && failure.code === "workspace_changed") workspaceChanged();
      throw failure;
    }
  }
  const context: SourceRequestContext = async (path, init) => {
    if (obsolete) throw new StaleSourceResponseError();
    const scoped = await communitySourceContext(path, init);
    if (obsolete) throw new StaleSourceResponseError();
    const headers = new Headers(scoped.init?.headers);
    if (new URL(path, "https://community.invalid").pathname === "/api/skill-source-workspace") headers.delete("X-Source-Workspace");
    else headers.set("X-Source-Workspace", workspace);
    return { ...scoped, init: { ...scoped.init, headers } };
  };
  const server = new URL(baseUrl || window.location.origin).href.replace(/\/+$/, "");
  let authSession = crypto.randomUUID();
  try {
    const saved = sessionStorage.getItem("oms-community-source-session");
    if (saved) {
      const value: unknown = JSON.parse(saved);
      if (value && typeof value === "object" && "server" in value && "id" in value && value.server === server && typeof value.id === "string") authSession = value.id;
    }
    sessionStorage.setItem("oms-community-source-session", JSON.stringify({ server, id: authSession }));
  } catch { /* In-memory request identity still works when browser storage is unavailable. */ }
  const retained = createSourceRequests({
    request: <T>(path: string, init?: RequestInit) => guarded(() => api.request<T>(path, init)),
    requestBytes: (path, init) => guarded(() => api.requestBytes(path, init)), context,
    scope: { server, workspace, authSession },
  });
  const requests: SourceRequests = {
    ...retained,
    get persistent() { return retained.persistent; },
    recover: async (key, init) => {
      if (obsolete) throw new StaleSourceResponseError();
      const current = await probe.workspace();
      if (verifiedWorkspace(current?.workspace_id) !== workspace) {
        workspaceChanged();
        throw new StaleSourceResponseError();
      }
      return retained.recover(key, init);
    },
  };
  return { client: createSourcesClient(requests), requests, scope: `${server}:${workspace}:${authSession}` };
}

export async function sourcePages<T>(load: (cursor?: string) => Promise<SourcePage<T>>): Promise<T[]> {
  const items: T[] = [], seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await load(cursor);
    items.push(...page.items);
    cursor = page.next_cursor ?? undefined;
    if (cursor && seen.has(cursor)) throw new Error("The server repeated a source page. Refresh before continuing.");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

export function saveSourceDownload(bytes: Uint8Array, path: string) {
  const url = URL.createObjectURL(new Blob([new Uint8Array(bytes).buffer], { type: "application/octet-stream" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = (path.split("/").at(-1) || "source-file").replace(/[\\\x00-\x1f\x7f]/g, "_");
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
