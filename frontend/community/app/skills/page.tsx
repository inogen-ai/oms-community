import { Suspense } from "react";
import Skills from "@/components/Skills";
export default function Page() { return <Suspense fallback={<p role="status">Loading…</p>}><Skills /></Suspense>; }
