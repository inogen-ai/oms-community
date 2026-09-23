"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphNode, Graph as GraphData } from "@inogen/oms-client";
import { Badge, Button, EmptyState, Markdown, Notice, Panel } from "@inogen/oms-ui-core";
import { GraphExplorer, NODE_LABELS } from "@inogen/oms-ui-core/graph";
import { message, ResourceStatus, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";

const EMPTY_NODES: GraphNode[] = [];
const EMPTY_EDGES: { source: string; target: string; type?: string }[] = [];
const LABELS = new Map(NODE_LABELS.map((label) => [label.toLowerCase(), label]));

function nodeLabel(node: GraphNode) {
  const label = node.kind || node.labels?.[0] || node.label || "Node";
  return LABELS.get(label.toLowerCase()) || label;
}

function nodeName(node: GraphNode) {
  const name = node.name || node.body || node.properties?.name || node.properties?.body;
  return typeof name === "string" && name.trim() ? name : node.id;
}

function propertyText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export default function Graph() {
  const { api } = useWorkspace();
  const [skillId, setSkillId] = useState("");
  const skills = useResource(() => api.skills(), [api]);
  const [view, setView] = useState<GraphData>({ nodes: [], edges: [] });
  const [loading, setLoading] = useState(true);
  const [graphError, setGraphError] = useState("");
  const [pages, setPages] = useState<Record<string, { next: number | null; total: number }>>({});
  const [expanding, setExpanding] = useState<string[]>([]);
  const canvasEpoch = useRef(0);
  const pending = useRef(new Set<string>());
  useEffect(() => { void loadGraph(); return () => { canvasEpoch.current++; }; }, [api]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [error, setError] = useState("");
  const inspection = useRef(0);
  useEffect(() => () => { inspection.current++; }, []);

  const nodes = view.nodes || EMPTY_NODES;
  const edges = view.edges || EMPTY_EDGES;
  const canvasNodes = useMemo(() => nodes.map((node) => ({ id: node.id, label: nodeLabel(node), caption: nodeName(node) })), [nodes]);
  const names = useMemo(() => new Map(canvasNodes.map((node) => [node.id, node.caption])), [canvasNodes]);
  const connections = edges.filter((edge) => edge.source === selectedId || edge.target === selectedId);

  function clearSelection() {
    inspection.current++;
    setSelectedId(null);
    setSelected(null);
    setDetailsLoading(false);
    setError("");
  }

  async function inspect(id: string | null) {
    if (!id) { clearSelection(); return; }
    const request = ++inspection.current;
    setSelectedId(id);
    setSelected(null);
    setError("");
    setDetailsLoading(true);
    try {
      const node = await api.node(id);
      if (request === inspection.current) setSelected(node);
    } catch (failure) {
      if (request === inspection.current) setError(message(failure));
    } finally {
      if (request === inspection.current) setDetailsLoading(false);
    }
  }

  function resetCanvas() {
    canvasEpoch.current++;
    pending.current.clear(); setExpanding([]); setPages({}); setView({ nodes: [], edges: [] });
    setGraphError(""); setLoading(false); clearSelection();
  }

  async function loadGraph(seed = "") {
    resetCanvas();
    const epoch = canvasEpoch.current;
    setLoading(true);
    try {
      const result = await api.graph("", seed || undefined);
      if (epoch === canvasEpoch.current) { setView(result); if (seed) void inspect(seed); }
    } catch (failure) { if (epoch === canvasEpoch.current) setGraphError(message(failure)); }
    finally { if (epoch === canvasEpoch.current) setLoading(false); }
  }

  async function expandNode(id: string) {
    if (pending.current.has(id) || pages[id]?.next === null) return;
    const epoch = canvasEpoch.current;
    pending.current.add(id); setExpanding([...pending.current]); setGraphError("");
    try {
      const result = await api.neighbours(id, pages[id]?.next ?? 0);
      if (epoch !== canvasEpoch.current) return;
      setView(current => ({
        nodes: [...new Map([...current.nodes, ...result.nodes].map(node => [node.id, node])).values()],
        edges: [...new Map([...(current.edges ?? []), ...(result.edges ?? [])].map(edge => [JSON.stringify([edge.source, edge.type, edge.target]), edge])).values()],
      }));
      setPages(current => ({ ...current, [id]: { next: result.next_offset, total: result.total } }));
    } catch (failure) { if (epoch === canvasEpoch.current) setGraphError(message(failure)); }
    finally { if (epoch === canvasEpoch.current) { pending.current.delete(id); setExpanding([...pending.current]); } }
  }

  const properties = selected?.properties || {};
  const description = typeof properties.description === "string" ? properties.description : "";
  const body = selected?.body || (typeof properties.body === "string" ? properties.body : "");
  const propertyEntries = Object.entries(properties).filter(([key]) => !["id", "name", "body", "description"].includes(key));

  return <>
    <PageHeading title="Skill graph" />
    <div className="graph-workspace">
      <Panel title="Graph explorer" className="graph-panel">
        <form className="search-form" onSubmit={(event) => { event.preventDefault(); void loadGraph(skillId); }}>
          <label>Find a skill<select value={skillId} onChange={event => setSkillId(event.target.value)}><option value="">Workspace overview</option>{skills.data?.map(skill => <option key={skill.id} value={skill.id}>{skill.name}</option>)}</select></label>
          <Button type="submit" disabled={loading}>Explore</Button>
          <Button variant="secondary" onClick={resetCanvas}>Clear canvas</Button>
        </form>
        <ResourceStatus resource={skills} />
        {loading && <p role="status">Loading graph…</p>}
        {graphError && <Notice kind="error">{graphError}</Notice>}
        <GraphExplorer nodes={canvasNodes} edges={edges} selectedId={selectedId} onSelectNode={(id) => void inspect(id)} onExpandNode={id => void expandNode(id)} />
        <p className="graph-scope muted">Double-click a node to add its direct connections, including those outside this view. Select it for details and more connections. {nodes.length >= 500 ? "The overview starts with up to 500 nodes; expansion is paged separately." : ""}</p>

      </Panel>
      <Panel title="Node details" className="graph-detail">
        {detailsLoading && <p role="status">Loading node…</p>}
        {error && <><Notice kind="error">{error}</Notice><Button variant="secondary" onClick={() => void inspect(selectedId)}>Retry node details</Button></>}
        {selected ? <>
          <Badge>{nodeLabel(selected)}</Badge>
          <h3 className="graph-detail__name">{nodeLabel(selected) === "Rule" ? "Rule" : nodeName(selected)}</h3>
          <code className="graph-detail__id">{selected.id}</code>
          {description && <Markdown inert body={description} />}
          {body && <Markdown inert body={body} />}
          {nodeLabel(selected) === "Skill" && <Link className="text-link" href={`/skills?skill=${encodeURIComponent(selected.id)}`}>Open skill document →</Link>}
          <div className="graph-connections">
            <h3>Connections</h3>
            <Button variant="secondary" disabled={expanding.includes(selected.id) || pages[selected.id]?.next === null} onClick={() => void expandNode(selected.id)}>{expanding.includes(selected.id) ? "Loading connections…" : pages[selected.id]?.next === null ? "All connections loaded" : pages[selected.id] ? "Load more connections" : "Expand connections"}</Button>
            {pages[selected.id] && <p className="muted" role="status">{Math.min(pages[selected.id].next ?? pages[selected.id].total, pages[selected.id].total)} of {pages[selected.id].total} direct connections loaded.</p>}
            {connections.length ? <ul>{connections.map((edge, index) => {
              const other = edge.source === selectedId ? edge.target : edge.source;
              return <li key={`${edge.source}:${edge.target}:${index}`}><span className="graph-connections__type">{edge.source === selectedId ? "→" : "←"} {edge.type || "RELATED_TO"}</span><button type="button" onClick={() => void inspect(other)}>{names.get(other) || other}</button></li>;
            })}</ul> : <p className="muted">No connections to other nodes in this view.</p>}
          </div>
          {propertyEntries.length > 0 && <details className="source-disclosure"><summary>Node properties</summary><dl className="graph-properties">{propertyEntries.map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{propertyText(value)}</dd></div>)}</dl></details>}
          <details className="source-disclosure"><summary>Full node record</summary><pre className="small-json">{JSON.stringify(selected, null, 2)}</pre></details>
        </> : !detailsLoading && !error && <EmptyState title="Select a node">Choose a node on the graph or use the node selector to inspect its details.</EmptyState>}
      </Panel>
    </div>
  </>;
}
