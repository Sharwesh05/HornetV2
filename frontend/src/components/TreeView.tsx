import { useMemo, useState } from "react";

import type { TreeNode } from "../lib/types";
import { AttackerText } from "./AttackerText";
import { IconBox, IconDatabase, IconFile, IconFolder, IconGlobe, IconKey, IconSessions } from "./Icons";
import { Term } from "./Term";

interface Item {
  node: TreeNode;
  name: string;
  children: Item[];
  marked: boolean; // visited or changed, itself or below
}

const OVERLAY: Record<string, { label: string; cls: string }> = {
  created: { label: "new", cls: "bg-evidence-soft text-evidence" },
  modified: { label: "changed", cls: "bg-honey-soft text-honey" },
  deleted: { label: "deleted", cls: "bg-danger-soft text-danger" },
};

function parentPath(path: string): string | null {
  if (path === "/") return null;
  const i = path.lastIndexOf("/");
  return i <= 0 ? "/" : path.slice(0, i);
}

/** Build a hierarchy from flat nodes by path (overlay-only nodes have no ids, so ids cannot be used). */
export function buildTree(nodes: TreeNode[]): Item[] {
  const byPath = new Map<string, Item>();
  for (const node of nodes) {
    const name = node.path === "/" ? "/" : node.path.slice(node.path.lastIndexOf("/") + 1);
    byPath.set(node.path, { node, name, children: [], marked: false });
  }
  const roots: Item[] = [];
  for (const item of byPath.values()) {
    let parent = parentPath(item.node.path);
    while (parent !== null && !byPath.has(parent)) parent = parentPath(parent); // attach to nearest known ancestor
    const p = parent === null ? undefined : byPath.get(parent);
    if (p) p.children.push(item);
    else roots.push(item);
  }
  const mark = (item: Item): boolean => {
    const self = (item.node.visits ?? 0) > 0 || Boolean(item.node.overlay);
    const below = item.children.map(mark).some(Boolean);
    item.marked = self || below;
    item.children.sort((a, b) => a.name.localeCompare(b.name));
    return item.marked;
  };
  roots.forEach(mark);
  return roots;
}

function NodeIcon({ node }: { node: TreeNode }) {
  if (node.sensitivity === "synthetic-sensitive") return <IconKey size={16} className="text-honey" />;
  switch (node.type) {
    case "directory": return <IconFolder size={16} className="text-muted" />;
    case "service": return <IconBox size={16} className="text-muted" />;
    case "database": return <IconDatabase size={16} className="text-muted" />;
    case "endpoint": return <IconGlobe size={16} className="text-muted" />;
    case "user": return <IconSessions size={16} className="text-muted" />;
    default: return <IconFile size={16} className="text-muted" />;
  }
}

/**
 * The fake environment's files and services. For a visit it shows three states: the original (unchanged), what
 * the visitor pointed at ("visited": an observation, not proof of a read), and what changed in their copy only.
 */
export function TreeView({ nodes, sessionView = false }: { nodes: TreeNode[]; sessionView?: boolean }) {
  const roots = useMemo(() => buildTree(nodes), [nodes]);
  const [onlyMarked, setOnlyMarked] = useState(sessionView);
  const counts = useMemo(() => ({
    visited: nodes.filter((n) => (n.visits ?? 0) > 0).length,
    changed: nodes.filter((n) => n.overlay).length,
  }), [nodes]);

  return (
    <div className="panel p-4">
      {sessionView && (
        <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line pb-3 text-sm">
          <label className="flex items-center gap-2 font-medium">
            <input type="checkbox" className="h-4 w-4 accent-[var(--honey-fill)]" checked={onlyMarked} onChange={(e) => setOnlyMarked(e.target.checked)} />
            Only show what the visitor touched
          </label>
          <span className="flex items-center gap-1.5"><span className="chip bg-ai-soft text-ai">visited</span> {counts.visited} <Term id="visited">items</Term></span>
          <span className="flex items-center gap-1.5"><span className="chip bg-honey-soft text-honey">new or changed</span> {counts.changed} <Term id="overlay">items</Term></span>
          <span className="flex items-center gap-1.5"><IconKey size={15} className="text-honey" /> holds <Term id="fake-secret">fake passwords</Term></span>
        </div>
      )}
      <ul className="text-sm" data-testid="tree">
        {roots.map((r) => (
          <Row key={r.node.path} item={r} depth={0} onlyMarked={onlyMarked} />
        ))}
      </ul>
    </div>
  );
}

function Row({ item, depth, onlyMarked }: { item: Item; depth: number; onlyMarked: boolean }) {
  const [open, setOpen] = useState(depth < 1 || item.marked);
  if (onlyMarked && !item.marked) return null;
  const n = item.node;
  const hasChildren = item.children.length > 0;
  const ov = n.overlay ? OVERLAY[n.overlay] : undefined;
  return (
    <li>
      <div className="flex items-center gap-1.5 rounded-md py-1 hover:bg-surface-2" style={{ paddingLeft: depth * 18 }}>
        <button
          className="flex h-5 w-5 items-center justify-center rounded text-muted disabled:invisible"
          onClick={() => setOpen(!open)}
          disabled={!hasChildren}
          // never put the (possibly attacker-chosen) name in an attribute: the name is the visible text beside it
          aria-label={open ? "Collapse folder" : "Expand folder"}
          aria-expanded={hasChildren ? open : undefined}
        >
          {open ? "▾" : "▸"}
        </button>
        <NodeIcon node={n} />
        {/* names of new files were chosen by the visitor */}
        <AttackerText value={item.name} className={`font-mono text-[13px] ${n.in_base === false ? "text-evidence" : ""}`} />
        {n.type !== "directory" && n.type !== "file" && <span className="text-xs text-muted">{n.type}</span>}
        {(n.visits ?? 0) > 0 && (
          <span className="chip bg-ai-soft text-ai" title={`Seen through: ${(n.basis ?? []).join(", ")}`}>
            visited {n.visits === 1 ? "once" : `${n.visits} times`}
          </span>
        )}
        {ov && <span className={`chip ${ov.cls}`}>{ov.label}</span>}
      </div>
      {open && hasChildren && (
        <ul>
          {item.children.map((c) => (
            <Row key={c.node.path} item={c} depth={depth + 1} onlyMarked={onlyMarked} />
          ))}
        </ul>
      )}
    </li>
  );
}
