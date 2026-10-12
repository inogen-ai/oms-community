import type { ReactNode } from "react";
export function SourceChangeConfirmation(props: {
  action: "remove" | "relocate"; destination?: string; skills: readonly { id: string; name: string }[];
  busy?: boolean; onConfirm: () => void; onCancel: () => void;
}): ReactNode;
