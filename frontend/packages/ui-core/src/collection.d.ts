import type { ReactNode } from "react";

export function collectionPage<T>(items: T[], requestedPage: number, pageSize: number): { items: T[]; page: number; pageCount: number; total: number; start: number; end: number };
export function CollectionPagination(props: { page: number; total: number; pageSize: number; onPageChange: (page: number) => void; label?: string; singularLabel?: string; disabled?: boolean }): ReactNode;
export function CollectionViewToggle(props: { value: "cards" | "list"; onChange: (value: "cards" | "list") => void; label?: string }): ReactNode;
export function CollectionList(props: { items: { id: string; title: string; description?: string; meta?: ReactNode; actions?: ReactNode }[]; selectedId?: string | null; onSelect: (id: string) => void; label?: string }): ReactNode;
export function CollectionMultiSelect(props: { items: { id: string; name: string }[]; selectedIds: string[]; onChange: (ids: string[]) => void; disabled?: boolean; searchLabel?: string }): ReactNode;
