import { Suspense } from "react";
import Sources from "@/components/Sources";

export default function SourcesPage() {
  return <Suspense fallback={<p role="status">Loading sources…</p>}><Sources /></Suspense>;
}
