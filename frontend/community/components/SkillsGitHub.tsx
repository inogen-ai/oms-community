"use client";
import { useRouter } from "next/navigation";
import { Button, GitHubMark } from "@inogen/oms-ui-core";

/** The Skills page's "Add a GitHub source" button: it opens or closes the inline add panel. */
export function AddGitHubSourceButton({ open }: { open: boolean }) {
  const router = useRouter();
  return <Button variant="secondary" aria-expanded={open} onClick={() => router.replace(open ? "/skills/" : "/skills/?github=add", { scroll: false })}><GitHubMark size={15} />Add a GitHub source</Button>;
}
