"use client";
import { useRef, useState } from "react";
import type { ReviewItem, Skill } from "@inogen/oms-client";
import { Badge, Button, CollectionPagination, EmptyState, ManualReviewCard, Notice, Panel, collectionPage, type ManualReviewDraft } from "@inogen/oms-ui-core";
import { ChevronDown } from "lucide-react";
import { Feedback, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import SourceUpdates from "./SourceUpdates";
import { CorrectionAutomationHint } from "./EditionInformation";

const PAGE_SIZE = 20;
const isHeld = (item: ReviewItem) => item.state === "held_safety" || Boolean(item.held_reason);

export default function Inbox() {
  const { api, capabilities } = useWorkspace();
  const heading = useRef<HTMLHeadingElement>(null);
  const resource = useResource(async () => capabilities.manual_learning
    ? { items: await api.review(), skills: await api.skills() } : { items: [], skills: [] }, [api, capabilities.manual_learning]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [skillFilter, setSkillFilter] = useState("");
  const [sort, setSort] = useState("safety");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, ManualReviewDraft>>({});
  const [resolvedIds, setResolvedIds] = useState<Set<string>>(() => new Set());
  const [decisionMessage, setDecisionMessage] = useState("");
  const skills = [...(resource.data?.skills ?? [])].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  const items = (resource.data?.items ?? []).filter(item => !resolvedIds.has(item.txn_id)).sort((a, b) => {
    if (sort === "safety") return Number(isHeld(b)) - Number(isHeld(a)) || a.txn_id.localeCompare(b.txn_id);
    if (sort === "text") return a.text.localeCompare(b.text) || a.txn_id.localeCompare(b.txn_id);
    return a.txn_id.localeCompare(b.txn_id);
  });
  const skillName = skills.find(skill => skill.id === skillFilter)?.name.toLocaleLowerCase();
  const filtered = items.filter(item => {
    const matchesStatus = !status || (status === "held" ? isHeld(item) : status === "exact" ? item.exact_matches.length > 0 && !isHeld(item) : status === "similar" ? Boolean(item.similar_matches?.length) && !isHeld(item) : !isHeld(item));
    const matchesSkill = !skillFilter || item.candidate_skill_ids.includes(skillFilter) || item.skill_suggestions?.some(s => s.id === skillFilter) || item.skill_hint === skillFilter || Boolean(skillName && item.skill_hint?.toLocaleLowerCase() === skillName);
    return matchesStatus && matchesSkill && `${item.text} ${item.source_ref || ""} ${item.txn_id} ${item.skill_hint || ""} ${item.repo || ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  });
  const result = collectionPage(filtered, page, PAGE_SIZE);
  const selected = items.find(item => item.txn_id === selectedId);
  const outsidePage = selected && !result.items.some(item => item.txn_id === selectedId);
  const visible = outsidePage ? [selected, ...result.items] : result.items;
  const filtering = Boolean(query || status || skillFilter);
  function browse(change: () => void) { if (!selectedId || !drafts[selectedId]) setSelectedId(null); change(); }
  function clearFilters() { browse(() => { setQuery(""); setStatus(""); setSkillFilter(""); setPage(1); }); }

  return <><PageHeading headingRef={heading} title="Review corrections" description="Open a correction, review its context, and decide how your skill should change." />
    {!capabilities.manual_learning ? <Notice>Manual correction review is unavailable on this server.</Notice> : <>
      <CorrectionAutomationHint onDismiss={() => heading.current?.focus()} />
      <ResourceStatus resource={resource} />
      {decisionMessage && <Notice kind="success">{decisionMessage}</Notice>}
      {resource.data && !items.length && <Panel><EmptyState title="You’re all caught up">Corrections submitted through your local HTTP or MCP endpoint will appear here.</EmptyState></Panel>}
      {(items.length > 1 || filtering) && <div className="collection-toolbar"><label className="collection-search">Find a correction<input type="search" value={query} placeholder="Search wording, repository or source" onChange={event => browse(() => { setQuery(event.target.value); setPage(1); })} /></label><label>Review status<select value={status} onChange={event => browse(() => { setStatus(event.target.value); setPage(1); })}><option value="">All pending</option><option value="review">Ready for manual review</option><option value="held">Safety holds</option><option value="exact">Exact matches available</option><option value="similar">Existing rule suggestions</option></select></label><label>Filter by skill<select value={skillFilter} onChange={event => browse(() => { setSkillFilter(event.target.value); setPage(1); })}><option value="">All skills</option>{skills.map(skill => <option key={skill.id} value={skill.id}>{skill.name}</option>)}</select></label><label className="collection-sort">Sort corrections<select value={sort} onChange={event => browse(() => { setSort(event.target.value); setPage(1); })}><option value="safety">Safety holds first</option><option value="id">Correction ID</option><option value="text">Correction wording</option></select></label></div>}
      {Boolean(items.length) && <p className="muted">{items.length} pending {items.length === 1 ? "correction" : "corrections"} · Open a card to review</p>}
      {filtering && <div className="collection-filter-summary"><p className="muted">{filtered.length} of {items.length} pending corrections match your filters.</p><Button variant="secondary" onClick={clearFilters}>Clear correction filters</Button></div>}
      {outsidePage && <Notice>The open correction is outside this page or its filters. Your draft is retained.</Notice>}
      {Boolean(items.length) && <div className="correction-cards" role="region" aria-label="Correction list">
        {!result.items.length && <EmptyState title="No corrections match your filters">Change the search or clear your filters to see pending work.</EmptyState>}
        {visible.map(item => {
          const open = item.txn_id === selectedId;
          const target = skills.find(skill => skill.id === item.candidate_skill_ids[0])?.name || item.skill_hint;
          const panelId = `correction-panel-${item.txn_id}`;
          return <article key={item.txn_id} className={`correction-card${open ? " is-open" : ""}`} data-correction-id={item.txn_id}>
            <button className="correction-card__summary" aria-expanded={open} aria-controls={panelId} aria-label={`Review correction: ${item.text}`} onClick={() => setSelectedId(open ? null : item.txn_id)}>
              <span className="correction-card__summary-text"><span className="correction-card__title">{item.text}</span><span className="correction-card__meta">{target || "Choose a skill during review"}{item.repo && <> · {item.repo}</>}{item.posted_at && <> · <time dateTime={item.posted_at}>{new Date(item.posted_at).toLocaleString()}</time></>}{drafts[item.txn_id] && <span className="collection-unsaved">Draft retained</span>}</span></span>
              <Badge>{isHeld(item) ? "Safety hold" : "Needs review"}</Badge><ChevronDown size={18} aria-hidden="true" />
            </button>
            {open && <div id={panelId} role="region" aria-label="Review selected correction"><ManualReviewCard key={`${item.txn_id}:${item.state}`} item={item} skills={skills} loadDocument={api.document}
              draft={drafts[item.txn_id] ?? { body: item.text, skill_ids: item.candidate_skill_ids.filter(id => skills.some(skill => skill.id === id)), rule_id: "" }}
              onDraftChange={draft => setDrafts(current => ({ ...current, [item.txn_id]: draft }))}
              onDecision={async decision => {
                await api.decide(item.txn_id, decision);
                setDrafts(current => { const next = { ...current }; delete next[item.txn_id]; return next; });
                if (decision.action !== "release_safety") { setResolvedIds(current => new Set([...current, item.txn_id])); setSelectedId(current => current === item.txn_id ? null : current); }
                setDecisionMessage(decision.action === "amend" ? "Guidance updated and correction resolved. Publish to update your agents." : decision.action === "create" ? "Rule created." : decision.action === "reinforce" ? "Existing rule reinforced." : decision.action === "reject" ? "Correction rejected." : "Safety hold released. Review the wording and target skills before applying it.");
                resource.refresh();
              }} /></div>}
          </article>;
        })}
        <CollectionPagination page={result.page} total={result.total} pageSize={PAGE_SIZE} label="Corrections" onPageChange={next => browse(() => setPage(next))} />
      </div>}
      <ContributionForm skills={resource.data?.skills ?? []} onSubmitted={resource.refresh} subdued={items.length > 0} />
    </>}
    {capabilities.github_skill_sources && <SourceUpdates />}
  </>;
}

function ContributionForm({ skills, onSubmitted, subdued }: { skills: Skill[]; onSubmitted: () => void; subdued: boolean }) {
  const { api } = useWorkspace();
  const action = useAction();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [hint, setHint] = useState("");
  // Retain this id after a network failure; retry completes the same contribution.
  const [transactionId, setTransactionId] = useState<string | null>(null);
  function submit() {
    const id = transactionId ?? crypto.randomUUID();
    setTransactionId(id);
    void action.run(() => api.contribute({ correction: text.trim(), transaction_id: id, ...(hint ? { skill_hint: hint } : {}), source_ref: "community:browser", signal_type: "explicit_correction" }), "Correction submitted for review.", () => {
      setText(""); setHint(""); setTransactionId(null); setOpen(false); onSubmitted();
    });
  }
  return <div className={`contribution-form${subdued ? " contribution-form--subdued" : ""}`}><Button variant="secondary" onClick={() => setOpen(!open)}>{open ? "Close correction form" : "Submit a correction"}</Button><Feedback action={action} />{open && <Panel title="Capture a correction"><form onSubmit={(event) => { event.preventDefault(); submit(); }}><label>Correction<textarea value={text} rows={4} required disabled={action.busy} onChange={(event) => { setText(event.target.value); setTransactionId(null); }} /></label><label>Skill hint<select value={hint} disabled={action.busy} onChange={(event) => { setHint(event.target.value); setTransactionId(null); }}><option value="">Choose during review</option>{skills.map((skill) => <option key={skill.id} value={skill.id}>{skill.name}</option>)}</select></label><p className="muted">Sanitisation and safety screening run before this becomes a manual review item.</p><Button type="submit" disabled={!text.trim() || action.busy}>Send to inbox</Button></form></Panel>}</div>;
}
