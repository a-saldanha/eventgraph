"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { api, TYPE_COLORS } from "@/lib/api";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

const nid = (v) => (v && typeof v === "object" ? v.id : v);

export default function GraphView({ onSelectEntity, highlight = [] }) {
  const [data, setData] = useState({ nodes: [], links: [] });
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [q, setQ] = useState("");
  const [focus, setFocus] = useState(null); // locally-focused node id (from the list)
  const wrapRef = useRef(null);
  const fgRef = useRef(null);

  useEffect(() => {
    api.graph().then(setData).catch(() => {});
  }, []);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const nodesById = useMemo(() => {
    const m = new Map();
    for (const n of data.nodes) m.set(n.id, n);
    return m;
  }, [data.nodes]);

  // adjacency: id -> [{id, weight}]
  const neighbors = useMemo(() => {
    const m = new Map();
    const add = (a, b, w) => {
      if (!m.has(a)) m.set(a, []);
      m.get(a).push({ id: b, weight: w });
    };
    for (const l of data.links) {
      const s = nid(l.source), t = nid(l.target);
      add(s, t, l.weight || 1);
      add(t, s, l.weight || 1);
    }
    return m;
  }, [data.links]);

  // list of nodes, filtered by search, sorted by degree desc
  const listed = useMemo(() => {
    const ql = q.trim().toLowerCase();
    let ns = data.nodes;
    if (ql) ns = ns.filter((n) => n.label.toLowerCase().includes(ql) ||
      (n.emails || []).some((e) => e.toLowerCase().includes(ql)));
    return [...ns].sort((a, b) => (b.degree || 0) - (a.degree || 0));
  }, [data.nodes, q]);

  const focusNeighbors = useMemo(() => {
    if (!focus) return [];
    const seen = new Set();
    return (neighbors.get(focus) || [])
      .filter((x) => (seen.has(x.id) ? false : seen.add(x.id)))
      .map((x) => ({ ...nodesById.get(x.id), weight: x.weight }))
      .filter((x) => x && x.id)
      .sort((a, b) => (b.weight || 0) - (a.weight || 0));
  }, [focus, neighbors, nodesById]);

  // canvas highlight: local focus (node + its neighbors) wins, else parent highlight
  const hl = useMemo(() => {
    if (focus) return new Set([focus, ...(neighbors.get(focus) || []).map((x) => x.id)]);
    return new Set(highlight);
  }, [focus, neighbors, highlight]);

  const pick = (id) => {
    setFocus(id);
    onSelectEntity && onSelectEntity(id);
    const n = nodesById.get(id);
    if (n && fgRef.current && n.x != null) {
      fgRef.current.centerAt(n.x, n.y, 600);
      fgRef.current.zoom(Math.max(2.5, fgRef.current.zoom()), 600);
    }
  };

  return (
    <div ref={wrapRef} style={{ position: "absolute", inset: 0 }}>
      {/* DOM navigator — always clickable, independent of canvas hit-testing */}
      <div className="graph-nav">
        <input
          className="nav-search"
          placeholder={`Search ${data.nodes.length} entities…`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {focus && (
          <div className="nav-focus">
            <div className="nav-focus-head">
              <span className="dot" style={{ background: TYPE_COLORS[nodesById.get(focus)?.type] || "#888" }} />
              <strong>{nodesById.get(focus)?.label}</strong>
              <button className="nav-clear" onClick={() => setFocus(null)}>clear ✕</button>
            </div>
            <div className="nav-sub">{focusNeighbors.length} connection{focusNeighbors.length === 1 ? "" : "s"}</div>
            <div className="nav-list nav-conns">
              {focusNeighbors.map((n) => (
                <button key={n.id} className="nav-row" onClick={() => pick(n.id)}>
                  <span className="dot" style={{ background: TYPE_COLORS[n.type] || "#888" }} />
                  <span className="nav-label">{n.label}</span>
                  <span className="nav-meta">{n.type}</span>
                </button>
              ))}
              {focusNeighbors.length === 0 && <div className="nav-empty">no linked entities</div>}
            </div>
            <div className="nav-sub">all entities</div>
          </div>
        )}
        <div className="nav-list">
          {listed.map((n) => (
            <button
              key={n.id}
              className={`nav-row${focus === n.id ? " active" : ""}`}
              onClick={() => pick(n.id)}
            >
              <span className="dot" style={{ background: TYPE_COLORS[n.type] || "#888" }} />
              <span className="nav-label">{n.label}</span>
              <span className="nav-meta">{n.degree || 0}</span>
            </button>
          ))}
          {listed.length === 0 && <div className="nav-empty">no matches</div>}
        </div>
      </div>

      <div className="legend">
        {Object.entries(TYPE_COLORS).map(([t, c]) => (
          <div key={t}>
            <span className="dot" style={{ background: c }} />
            {t}
          </div>
        ))}
      </div>
      <ForceGraph2D
        ref={fgRef}
        width={size.w}
        height={size.h}
        graphData={data}
        nodeLabel={(n) => `${n.label} (${n.type}${n.emails?.length ? `, ${n.emails.length} emails` : ""})`}
        nodeVal={(n) => 1 + (n.degree || 0)}
        linkColor={() => "rgba(120,120,120,0.35)"}
        linkWidth={(l) => Math.min(4, 0.5 + (l.weight || 1) * 0.15)}
        cooldownTicks={120}
        onNodeClick={(n) => pick(n.id)}
        onBackgroundClick={() => setFocus(null)}
        onNodeHover={(n) => {
          if (wrapRef.current) wrapRef.current.style.cursor = n ? "pointer" : "default";
        }}
        nodePointerAreaPaint={(node, color, ctx) => {
          const r = Math.max(3, Math.min(11, 3 + (node.degree || 0) * 0.6));
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fill();
        }}
        nodeCanvasObject={(node, ctx, scale) => {
          const r = Math.max(3, Math.min(11, 3 + (node.degree || 0) * 0.6));
          const active = hl.size === 0 || hl.has(node.id);
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fillStyle = TYPE_COLORS[node.type] || "#888";
          ctx.globalAlpha = active ? 1 : 0.15;
          ctx.fill();
          if (hl.has(node.id)) {
            ctx.lineWidth = 2 / scale;
            ctx.strokeStyle = "#111";
            ctx.stroke();
          }
          if (scale > 1.3 || node.degree >= 4 || focus === node.id) {
            ctx.globalAlpha = active ? 1 : 0.2;
            ctx.font = `${11 / scale}px sans-serif`;
            ctx.fillStyle = "#222";
            ctx.fillText(String(node.label).slice(0, 22), node.x + r + 1, node.y + 3);
          }
          ctx.globalAlpha = 1;
        }}
      />
    </div>
  );
}
