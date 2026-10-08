"use client";
import { useEffect, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { legacySourcesTarget } from "@/lib/sources";
import type { Skill, SkillDeletion, DocumentPart } from "@inogen/oms-client";
import SkillDetail from "./SkillDetail";
import GitHubImport from "./GitHubImport";
import { AddGitHubSourceButton } from "./SkillsGitHub";
import { Badge, Button, CollectionList, CollectionPagination, CollectionViewToggle, EmptyState, GitHubMark, Markdown, Notice, Panel, SkillMarkdown, collectionPage, sourceDisplayUrl } from "@inogen/oms-ui-core";
import { Feedback, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import SkillImport from "./SkillImport";
import { BookOpen, Plus, Upload, Volume2, VolumeX } from "lucide-react";

export interface DetailsDraft { name: string; description: string; domain: string }
export interface SkillDraft { details?: DetailsDraft; sections?: Record<string, string>; revision?: string; parts?: DocumentPart[] }
export type UpdateDraft = (update: (draft: SkillDraft) => SkillDraft) => void;
const PAGE_SIZE = 24;
const hasDraft = (draft?: SkillDraft) => Boolean(draft?.details || Object.keys(draft?.sections ?? {}).length);

export default function Skills() {
  const { api, capabilities, sourceRevision } = useWorkspace();
  const resource = useResource(() => api.skills(), [api, sourceRevision]);
  const router = useRouter();
  const search = useSearchParams();
  const github = search.get("github");
  const githubView = github === "add" ? github : null;
  const [selected, setSelected] = useState<string | null>(null);
  const [deleted, setDeleted] = useState<SkillDeletion | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("");
  const [sort, setSort] = useState("name");
  const [page, setPage] = useState(1);
  const [chosenView, setChosenView] = useState<"cards" | "list" | null>(null);
  const [createDraft, setCreateDraft] = useState<DetailsDraft>({ name: "", description: "", domain: "general" });
  const [drafts, setDrafts] = useState<Record<string, SkillDraft>>({});
  const action = useAction();
  useEffect(() => { setSelected(new URLSearchParams(window.location.search).get("skill")); }, []);
  useEffect(() => { if (githubView) setSelected(null); }, [githubView, search.toString()]);
  // The tracked-repositories view moved to the Import page; an old link must not dead-end here.
  useEffect(() => { if (github === "sources") router.replace(legacySourcesTarget(search.toString())); }, [github, router, search]);
  const skills = resource.data ?? [];
  const domains = [...new Set(skills.map((skill) => skill.domain || "general"))].sort();
  const filtered = skills.filter((skill) => (!domain || (skill.domain || "general") === domain) && `${skill.name} ${skill.description ?? ""} ${skill.domain ?? ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).sort((left, right) => {
    const names = left.name.localeCompare(right.name) || left.id.localeCompare(right.id);
    if (sort === "name-desc") return -names;
    if (sort === "domain") return (left.domain || "general").localeCompare(right.domain || "general") || names;
    return names;
  });
  const result = collectionPage(filtered, page, PAGE_SIZE);
  const view = chosenView ?? (skills.length > PAGE_SIZE ? "list" : "cards");
  const dirtyCount = Object.values(drafts).filter(hasDraft).length;
  useEffect(() => { if (page !== result.page) setPage(result.page); }, [page, result.page]);
  function updateDraft(id: string, update: (draft: SkillDraft) => SkillDraft) {
    setDrafts((current) => ({ ...current, [id]: update(current[id] ?? {}) }));
  }
  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void action.run(() => api.createSkill({ name: createDraft.name.trim(), description: createDraft.description, domain: createDraft.domain || "general" }), "Skill created.", () => { setDeleted(null); setCreating(false); setCreateDraft({ name: "", description: "", domain: "general" }); resource.refresh(); });
  }
  function afterDelete(result: SkillDeletion) {
    resource.setData(current => current?.filter(skill => skill.id !== result.deleted) ?? null);
    if (domain && !skills.some(skill => skill.id !== result.deleted && (skill.domain || "general") === domain)) setDomain("");
    setDrafts(current => { const next = { ...current }; delete next[result.deleted]; return next; });
    setSelected(null); setDeleted(result); resource.refresh();
    const url = new URL(window.location.href);
    url.searchParams.delete("skill");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  function muteButton(skill: Skill) {
    const muted = skill.publish_enabled === false;
    return <Button variant="secondary" disabled={action.busy} aria-label={`${muted ? "Unmute" : "Mute"} ${skill.name}`} onClick={() => void action.run(() => api.updateSkill(skill.id, { publish_enabled: muted }), muted ? "Skill included in the next publication." : "Skill muted. Publish to remove it from your agents.", resource.refresh)}>{muted ? <Volume2 size={14} /> : <VolumeX size={14} />}{muted ? "Unmute" : "Mute"}</Button>;
  }
  return <><div hidden={Boolean(selected)}>
    <PageHeading title="Your skills library"><div className="actions"><Button variant="secondary" onClick={() => { setCreating(!creating); setImporting(false); }} aria-expanded={creating}><Plus size={15} aria-hidden="true" />Create skill</Button><Button onClick={() => { setImporting(!importing); setCreating(false); }} aria-expanded={importing} aria-controls="library-import"><Upload size={15} aria-hidden="true" />Upload skills</Button>{capabilities.github_skill_sources && <AddGitHubSourceButton open={githubView === "add"} />}</div></PageHeading><Feedback action={action} />
    {github && !capabilities.github_skill_sources && <Notice>Source tracking is unavailable on this server.</Notice>}
    {githubView === "add" && capabilities.github_skill_sources && <Panel title="Add a GitHub source"><GitHubImport discoveryId={search.get("discovery")} onInstalled={() => { setDeleted(null); resource.refresh(); }} /><div className="oms-panel-footer"><Button variant="secondary" onClick={() => router.replace("/skills/", { scroll: false })}>Close</Button></div></Panel>}
    {deleted && <Notice kind="success">“{deleted.name}” deleted. <a href="/publish/">Publish updates</a> to remove it from your agents.</Notice>}
    <div hidden={!importing}><Panel title="Upload skills" id="library-import"><SkillImport onImported={() => { setDeleted(null); resource.refresh(); }} /></Panel></div>
    {creating && <Panel title="Create a skill"><form onSubmit={create} className="form-grid"><label>Name<input name="name" required maxLength={128} value={createDraft.name} disabled={action.busy} onChange={(event) => setCreateDraft({ ...createDraft, name: event.target.value })} autoFocus /></label><label>Description<textarea name="description" rows={2} value={createDraft.description} disabled={action.busy} onChange={(event) => setCreateDraft({ ...createDraft, description: event.target.value })} /></label><label>Domain<input name="domain" value={createDraft.domain} disabled={action.busy} onChange={(event) => setCreateDraft({ ...createDraft, domain: event.target.value })} required /></label><div className="actions"><Button type="submit" disabled={action.busy}>Save new skill</Button><Button variant="secondary" onClick={() => setCreating(false)}>Close form</Button></div></form></Panel>}
    {dirtyCount > 0 && <p className="muted" role="status">Unsaved edits in {dirtyCount} {dirtyCount === 1 ? "skill" : "skills"} stay available while you browse this library.</p>}
    <Panel className="collection-library"><div className="collection-toolbar"><label className="collection-search">Find a skill<input type="search" placeholder="Search your library" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} /></label><label>Filter by domain<select value={domain} onChange={(event) => { setDomain(event.target.value); setPage(1); }}><option value="">All domains</option>{domains.map((name) => <option key={name} value={name}>{name}</option>)}</select></label><label className="collection-sort">Sort skills<select value={sort} onChange={(event) => { setSort(event.target.value); setPage(1); }}><option value="name">Name A–Z</option><option value="name-desc">Name Z–A</option><option value="domain">Domain, then name</option></select></label><CollectionViewToggle value={view} onChange={setChosenView} label="Skills view" /></div><ResourceStatus resource={resource} />
      {(query || domain) && <div className="collection-filter-summary"><p className="muted">{filtered.length} of {skills.length} skills match your filters.</p><Button variant="secondary" onClick={() => { setQuery(""); setDomain(""); setPage(1); }}>Clear skill filters</Button></div>}
      {!resource.loading && !resource.error && filtered.length === 0 && <EmptyState title={skills.length ? "No skills match your filters" : "Your next useful skill starts here"}>{skills.length ? "Change the search or clear your filters to see the library." : "Import an existing skill tree or create a skill above."}</EmptyState>}
      {view === "cards" ? <div className="skill-grid">{result.items.map((skill) => <article className={`skill-card${skill.source ? " skill-card--linked" : ""}`} key={skill.id}>{skill.source && <span className="skill-card__source"><GitHubMark title={`GitHub: ${sourceDisplayUrl(skill.source.canonical_url)}`} /></span>}<button className="skill-card__open" onClick={() => setSelected(skill.id)}><span className="skill-card__heading"><span className="skill-icon" aria-hidden="true"><BookOpen size={18} /></span><h2>{skill.name}</h2></span><p>{skill.description || "Open this skill to inspect its guidance."}</p></button><div className="skill-card__footer"><Badge>{skill.domain || "general"}</Badge>{skill.publish_enabled === false && <Badge>Muted</Badge>}{muteButton(skill)}</div>{hasDraft(drafts[skill.id]) && <span className="collection-unsaved">Unsaved edits</span>}</article>)}</div> : <CollectionList label="Skills" items={result.items.map((skill) => ({ id: skill.id, title: skill.name, description: skill.description || "Open this skill to inspect its guidance.", actions: muteButton(skill), meta: <><Badge>{skill.domain || "general"}</Badge>{skill.source && <GitHubMark title={`GitHub: ${sourceDisplayUrl(skill.source.canonical_url)}`} />}{skill.publish_enabled === false && <Badge>Muted</Badge>}{hasDraft(drafts[skill.id]) && <span className="collection-unsaved">Unsaved edits</span>}</> }))} onSelect={setSelected} />}
      <CollectionPagination page={result.page} total={result.total} pageSize={PAGE_SIZE} label="Skills" onPageChange={setPage} />
    </Panel></div>
    {selected && <SkillDetail key={selected} domains={domains} id={selected} source={resource.data ? skills.find(row => row.id === selected)?.source ?? null : undefined} draft={drafts[selected] ?? {}} onDraftChange={(update) => updateDraft(selected, update)} onBack={() => { setSelected(null); resource.refresh(); }} onDeleted={afterDelete} />}
  </>;
}
