import type { ElementType, ReactNode } from "react";

export interface MobileNavigationItem { id: string; label: string; href?: string; count?: number }
export function MobileNavigation(props: {
  items: MobileNavigationItem[]; current?: string; onSelect?: (id: string) => void;
  linkComponent?: ElementType; label?: string; navigationLabel?: string;
  wideQuery?: string; className?: string;
}): ReactNode;
