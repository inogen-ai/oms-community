interface OriginUpdate { update_id: string; plan: { origin: { kind: "github" | "local" } } }
export function sourceUpdateScope<T extends OriginUpdate>(updates: readonly T[], options?: { origin?: "github" | "local"; selectedId?: string }): {
  shown: T[]; elsewhere: T | undefined; intro: string | null; empty: string;
};
