"use client";
import { useEffect } from "react";
import { legacySourcesTarget } from "@/lib/sources";

// Sources lives under Skills now; old links keep working.
export default function SourcesPage() {
  useEffect(() => { window.location.replace(legacySourcesTarget(window.location.search)); }, []);
  return <p role="status">Loading sources…</p>;
}
