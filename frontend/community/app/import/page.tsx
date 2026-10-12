import { Suspense } from "react";
import Import from "@/components/Import";
export default function Page() { return <Suspense fallback={<p role="status">Loading…</p>}><Import /></Suspense>; }
