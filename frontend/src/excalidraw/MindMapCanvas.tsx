import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

export type NodeOffset = { dx: number; dy: number };
export type NodeOffsets = Record<string, NodeOffset>;
export type NodeChange = { id: string; title: string; goal: string };
type Element = Record<string, any>;
type Node = { id: string; businessId: string; x: number; y: number; w: number; h: number; title: string; goal: string; kind: string };
const FONT = '"KaiTi", "STKaiti", "Kaiti SC", "Noto Serif CJK SC", "SimSun", serif';
const EMPTY_OFFSETS: NodeOffsets = {};
let measuringContext: CanvasRenderingContext2D | null = null;
const wrappedText = new Map<string, string[]>();

function wrap(text: string, width: number, fontSize: number, limit: number) {
  const key = JSON.stringify([text, width, fontSize, limit]);
  const cached = wrappedText.get(key);
  if (cached) return cached;
  measuringContext ??= document.createElement("canvas").getContext("2d");
  const context = measuringContext;
  if (context) context.font = `${fontSize}px ${FONT}`;
  const lines: string[] = [];
  let line = "";
  for (const character of text.replace(/[\r\n]+/g, " ")) {
    if (line && (context?.measureText(line + character).width ?? (line.length + 1) * fontSize) > width) {
      lines.push(line); line = "";
    }
    line += character;
  }
  if (line) lines.push(line);
  if (lines.length > limit) {
    lines.length = limit;
    let end = lines[limit - 1];
    while (end && (context?.measureText(end + "…").width ?? 0) > width) end = end.slice(0, -1);
    lines[limit - 1] = end + "…";
  }
  if (wrappedText.size > 1000) wrappedText.clear();
  wrappedText.set(key, lines);
  return lines;
}

export default function MindMapCanvas({ elements, height = 460, ariaLabel = "思维导图", offsets = EMPTY_OFFSETS,
  onBeginEdit, onSave, onModeChange, onSelectNode, readOnly = false }: {
  elements: Element[]; height?: number | "full"; ariaLabel?: string; offsets?: NodeOffsets;
  onBeginEdit?: () => Promise<void>; onSave?: (changes: NodeChange[], offsets: NodeOffsets) => Promise<void>;
  onModeChange?: (editing: boolean) => void;
  readOnly?: boolean;
  onSelectNode?: (path: string) => void;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const dragRef = useRef<{ pointer: number; x: number; y: number; node?: string } | null>(null);
  const fitted = useRef(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [view, setView] = useState({ x: 24, y: 24, zoom: 1 });
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [localOffsets, setLocalOffsets] = useState<NodeOffsets>(offsets);
  const [changes, setChanges] = useState<Record<string, NodeChange>>({});
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => { onModeChange?.(editing); }, [editing, onModeChange]);
  const nodes = useMemo<Node[]>(() => elements.filter(e => e.type === "rectangle" &&
    (e.customData?.role === "node" || e.id?.startsWith("node_"))).map(e => {
      const title = elements.find(t => t.type === "text" && t.x >= e.x && t.x < e.x + e.width && t.y >= e.y && t.y < e.y + e.height);
      return { id: e.customData?.path || e.customData?.business_id || e.id,
        businessId: e.customData?.business_id || e.id, x: e.x, y: e.y, w: e.width, h: e.height,
        title: e.customData?.title ?? title?.text ?? "", goal: e.customData?.goal ?? "", kind: e.customData?.kind || "course" };
    }), [elements]);
  const measuredNodes = useMemo(() => {
    const measured = nodes.map(node => {
      const value = { ...node, ...(changes[node.id] || {}) };
      const title = wrap(value.title, value.w - 24, 16, 2);
      const goal = wrap(value.goal, value.w - 24, 12, 2);
      return { ...value, h: Math.max(value.h, title.length * 20 + (goal.length ? goal.length * 15 + 4 : 0) + 24) };
    });
    const byId = new Map(measured.map(n => [n.businessId, n]));
    const children = new Map<string, Node[]>();
    const childIds = new Set<string>();
    for (const element of elements) {
      const parent = element.customData?.parent_id, child = byId.get(element.customData?.child_id);
      if (element.type === "line" && byId.has(parent) && child) {
        children.set(parent, [...(children.get(parent) || []), child]); childIds.add(child.businessId);
      }
    }
    const visited = new Set<string>();
    function place(node: Node, top: number): number {
      if (visited.has(node.businessId)) return top + node.h;
      visited.add(node.businessId);
      let bottom = top + node.h;
      const branch = (children.get(node.businessId) || []).sort((a, b) => a.y - b.y);
      if (branch.length) {
        let cursor = top;
        for (const child of branch) { bottom = place(child, cursor); cursor = bottom + 18; }
        node.y = Math.round((top + bottom) / 2 - node.h / 2);
        bottom = Math.max(bottom, node.y + node.h);
      } else node.y = top;
      return bottom;
    }
    if (children.size) {
      let cursor = Math.min(...measured.map(n => n.y));
      for (const root of measured.filter(n => !childIds.has(n.businessId)).sort((a, b) => a.y - b.y)) cursor = place(root, cursor) + 18;
    }
    return measured;
  }, [nodes, changes, elements]);
  const visibleNodes = measuredNodes.map(n => ({ ...n,
    x: n.x + (localOffsets[n.id]?.dx || 0), y: n.y + (localOffsets[n.id]?.dy || 0) }));
  const positions = new Map(visibleNodes.flatMap(n => [[n.id, n], [n.businessId, n]] as [string, Node][]));
  const lines = elements.filter(e => e.type === "line");
  const activeNode = visibleNodes.find(n => n.id === selected);
  useEffect(() => { if (!editing) setLocalOffsets(offsets); }, [offsets, editing]);
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(svg); return () => observer.disconnect();
  }, []);
  function fit(readable = false) {
    if (!visibleNodes.length || size.width < 1 || size.height < 1) return;
    const left = Math.min(...visibleNodes.map(n => n.x));
    const top = Math.min(...visibleNodes.map(n => n.y));
    const right = Math.max(...visibleNodes.map(n => n.x + n.w));
    const bottom = Math.max(...visibleNodes.map(n => n.y + n.h));
    const zoom = Math.min(1, Math.max(0.08, Math.min((size.width - 48) / (right - left), (size.height - 48) / (bottom - top))));
    if (readable && zoom < 0.65) {
      const root = visibleNodes.find(n => n.id === "route_root" || n.id === "course_root") || visibleNodes[0];
      setView({ x: 24 - left * 0.85, y: size.height / 2 - (root.y + root.h / 2) * 0.85, zoom: 0.85 });
      return;
    }
    setView({ x: (size.width - (right - left) * zoom) / 2 - left * zoom,
      y: (size.height - (bottom - top) * zoom) / 2 - top * zoom, zoom });
  }
  useEffect(() => {
    if (!fitted.current && nodes.length && size.width > 0 && size.height > 0) { fit(true); fitted.current = true; }
  }, [nodes, size]);
  const previousSize = useRef({ width: 0, height: 0 });
  useEffect(() => {
    const previous = previousSize.current;
    if (fitted.current && previous.width > 0 && previous.height > 0) {
      setView(value => ({ ...value, x: value.x + (size.width - previous.width) / 2,
        y: value.y + (size.height - previous.height) / 2 }));
    }
    previousSize.current = size;
  }, [size]);
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = svg.getBoundingClientRect(); const x = event.clientX - rect.left, y = event.clientY - rect.top;
      setView(v => {
        const zoom = Math.min(3, Math.max(0.08, v.zoom * Math.exp(-event.deltaY * 0.0015)));
        return { x: x - (x - v.x) * zoom / v.zoom, y: y - (y - v.y) * zoom / v.zoom, zoom };
      });
    };
    svg.addEventListener("wheel", wheel, { passive: false });
    return () => svg.removeEventListener("wheel", wheel);
  }, []);
  async function begin() {
    if (busy) return;
    setBusy(true); setError("");
    try { await onBeginEdit?.(); setChanges({}); setEditing(true); }
    catch (e: any) { setError(e.message || "无法进入编辑"); }
    finally { setBusy(false); }
  }
  async function finish() {
    if (!onSave || busy) return;
    setBusy(true); setError("");
    try { await onSave(Object.values(changes), localOffsets); setChanges({}); setEditing(false); setSelected(null); }
    catch (e: any) { setError(e.message || "保存失败，修改已保留，请重试"); }
    finally { setBusy(false); }
  }
  return <div className={`mindmap-shell${height === "full" ? " is-full" : ""}`} style={typeof height === "number" ? { height } : undefined}>
    <div className="mindmap-tools">
      {!readOnly && <button className="btn btn-sm" disabled={busy} onClick={editing ? finish : begin}>{busy ? "保存中…" : editing ? "完成编辑" : "编辑导图"}</button>}
      {editing && <button className="btn btn-sm" disabled={busy} onClick={() => { setEditing(false); setChanges({}); setLocalOffsets(offsets); setSelected(null); setError(""); }}>取消编辑</button>}
      <button className="btn btn-sm" onClick={() => fit(true)} aria-label="回到初始视角">回到初始视角</button>
      <span className="help">{editing ? "拖动节点调整位置，点击节点修改文字" : "拖动画布 · 滚轮缩放"}</span>
    </div>
    {error && <div className="mindmap-error" role="alert">{error}</div>}
    <svg ref={svgRef} className={`mindmap-svg ${editing ? "is-editing" : "is-browsing"}`} role="img" aria-label={ariaLabel}
      data-mode={editing ? "edit" : "browse"} data-zoom={view.zoom} data-pan-x={view.x} data-pan-y={view.y}
      onPointerDown={event => {
        if (busy || event.button !== 0) return;
        const group = (event.target as SVGElement).closest("[data-node-id]");
        const node = editing ? group?.getAttribute("data-node-id") || undefined : undefined;
        if (node) setSelected(node);
        const picked = (event.target as SVGElement).closest?.("[data-node-id]")?.getAttribute("data-node-id");
        if (picked) onSelectNode?.(picked);
        dragRef.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY, node };
        event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault();
      }}
      onPointerMove={event => {
        const drag = dragRef.current;
        if (!drag || drag.pointer !== event.pointerId) return;
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        drag.x = event.clientX; drag.y = event.clientY;
        if (drag.node) {
          const id = drag.node;
          setLocalOffsets(previous => ({ ...previous, [id]: { dx: (previous[id]?.dx || 0) + dx / view.zoom, dy: (previous[id]?.dy || 0) + dy / view.zoom } }));
        } else setView(v => ({ ...v, x: v.x + dx, y: v.y + dy }));
      }}
      onPointerUp={() => { dragRef.current = null; }} onPointerCancel={() => { dragRef.current = null; }}>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.zoom})`}>
        {lines.map(line => {
          const parent = positions.get(line.customData?.parent_id), child = positions.get(line.customData?.child_id);
          const points = parent && child ? [[parent.x + parent.w, parent.y + parent.h / 2],
            [(parent.x + parent.w + child.x) / 2, parent.y + parent.h / 2],
            [(parent.x + parent.w + child.x) / 2, child.y + child.h / 2], [child.x, child.y + child.h / 2]]
            : (line.points || []).map((p: number[]) => [line.x + p[0], line.y + p[1]]);
          return <polyline key={line.id} data-link-child={line.customData?.child_id} points={points.map((p: number[]) => p.join(",")).join(" ")} fill="none" stroke="#000000" strokeWidth="1" />;
        })}
        {visibleNodes.map(node => {
          const title = wrap(node.title, node.w - 24, 16, 2);
          const goal = wrap(node.goal, node.w - 24, 12, 2);
          const block = title.length * 20 + (goal.length ? 4 + goal.length * 15 : 0);
          const y = Math.max(8, (node.h - block) / 2);
          return <g key={node.id} data-node-id={node.id} data-title={node.title} transform={`translate(${node.x} ${node.y})`}>
            <title>{node.title}{node.goal ? `\n${node.goal}` : ""}</title>
            <rect width={node.w} height={node.h} rx="8" fill="#ffffff" stroke="#000000" strokeWidth={selected === node.id && editing ? 2 : 1} />
            <text x="12" y={y + 16} fill="#000000" fontFamily={FONT} fontSize="16">
              {title.map((line, i) => <tspan key={i} x="12" dy={i ? 20 : 0}>{line}</tspan>)}
            </text>
            {goal.length > 0 && <text x="12" y={y + title.length * 20 + 16} fill="#000000" fontFamily={FONT} fontSize="12">
              {goal.map((line, i) => <tspan key={i} x="12" dy={i ? 15 : 0}>{line}</tspan>)}
            </text>}
          </g>;
        })}
      </g>
    </svg>
    {editing && activeNode && <div className="mindmap-node-editor" aria-label="节点编辑">
      <label>标题<input className="inp" aria-label="节点标题" maxLength={120} value={activeNode.title} onChange={event => setChanges(c => ({ ...c,
        [activeNode.id]: { id: activeNode.id, title: event.target.value, goal: activeNode.goal } }))} /></label>
      {activeNode.id !== "course_root" && activeNode.id !== "route_root" && activeNode.kind !== "unit" && activeNode.kind !== "stage" && <label>说明<input className="inp" aria-label="节点说明" maxLength={120} value={activeNode.goal} onChange={event => setChanges(c => ({ ...c,
        [activeNode.id]: { id: activeNode.id, title: activeNode.title, goal: event.target.value } }))} /></label>}
    </div>}
  </div>;
}
