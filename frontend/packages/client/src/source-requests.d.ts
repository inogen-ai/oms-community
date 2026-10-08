import type { SourceRequest, SourceByteRequest, SourceRequestContext, SourceOperation, SourceDiscovery } from "./sources.js";
/** Opaque identities only. authSession must change on login or authentication changes; never use a credential. */
export interface SourceRequestScope { server: string; workspace: string; authSession: string }
export interface SourceRequestStorage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }
export interface PendingSourceRequest {
  readonly key: string; readonly path: string; readonly method: "POST" | "DELETE" | "PUT";
  readonly digest: string; readonly body: string | null; readonly requires_body: boolean; readonly operation_id: string | null;
}
/** A further mutation an edition retains for recovery. The pathname test sees the path without its query; `body` guards the parsed JSON. */
export interface SourceMutationRoute {
  method: "POST" | "PUT" | "DELETE";
  pathname: (pathname: string) => boolean;
  body?: (parsed: unknown) => boolean;
}
export class StaleSourceResponseError extends Error { constructor() }
export interface SourceRequests {
  request: SourceRequest;
  requestBytes: SourceByteRequest;
  readonly persistent: boolean;
  pending(): Promise<readonly PendingSourceRequest[]>;
  recover(key: string, init?: RequestInit): Promise<SourceOperation | SourceDiscovery | undefined>;
  setScope(scope: SourceRequestScope | null): void;
  /** Invalidate pending generations and responses after retargeting or a relevant settings change. */
  clear(): void;
  /** Fence this view and detach its observer without discarding shared recovery records. */
  close(): void;
}
/** The injected transport must supply fresh authentication for both initiating requests and recovery polls. */
export function createSourceRequests(options: { request: SourceRequest; requestBytes?: SourceByteRequest; context?: SourceRequestContext; scope: SourceRequestScope | null; storage?: SourceRequestStorage | null;
  /** Called asynchronously after pending state changes, restoration or invalidation. Observer errors do not affect requests. */
  onChange?: () => void;
  /** Called asynchronously once if a peer makes this manager obsolete. Local resets and close do not call it; observer errors are isolated. */
  onInvalidated?: () => void;
  /** Edition routes retained beside the built-in ones. Path hygiene and tenant-only query keys still apply. */
  mutations?: ReadonlyArray<SourceMutationRoute>;
}): SourceRequests;
