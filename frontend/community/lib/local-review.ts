import type { Constraint } from "@inogen/oms-client";

export interface LocalConstraint extends Constraint {
  source: "console" | "import";
}

export interface ImportReviewItem {
  id: string;
  kind: "block_revision" | "removal" | "injection" | "polarity_conflict" | "skill_update";
  subject_id: string;
  other_id?: string | null;
  reason: string;
  proposed_body?: string | null;
  skill_id?: string | null;
}

export const importDecisionLabels: Record<Exclude<ImportReviewItem["kind"], "skill_update">, { title: string; accept: string; reject: string }> = {
  block_revision: { title: "Source text changed", accept: "Apply source text", reject: "Keep current text" },
  removal: { title: "A rule is missing from the source", accept: "Retire missing rule", reject: "Keep rule" },
  injection: { title: "A rule needs a safety decision", accept: "Approve screened rule", reject: "Retire rule" },
  polarity_conflict: { title: "Imported guidance may conflict", accept: "Keep earlier rule as well", reject: "Retire earlier rule" },
};
