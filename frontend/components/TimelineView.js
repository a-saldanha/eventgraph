"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function TimelineView({ onSelectEntity }) {
  const [rows, setRows] = useState([]);
  useEffect(() => { api.timeline().then(setRows).catch(() => {}); }, []);
  if (!rows.length) return <div className="pad">Loading timeline…</div>;

  const times = rows.flatMap((r) => [new Date(r.start), new Date(r.end)]);
  const min = Math.min(...times), max = Math.max(...times);
  const span = Math.max(1, max - min);
  const pct = (t) => ((new Date(t) - min) / span) * 100;

  return (
    <div className="pad">
      <div className="section-h">Reconstructed event timeline ({rows.length} sub-events)</div>
      <p className="snippet">Each bar spans the first→last evidence for that sub-event across all sources. Click a label to inspect it in the graph.</p>
      {rows.map((r) => (
        <div className="timeline-bar" key={r.entity_id}>
          <div className="label" style={{ cursor: "pointer" }} onClick={() => onSelectEntity(r.entity_id)}>
            {r.label}
          </div>
          <div className="track">
            <div className="fill" style={{ left: `${pct(r.start)}%`, width: `${Math.max(1.5, pct(r.end) - pct(r.start))}%` }} />
          </div>
          <div className="meta" style={{ width: 190 }}>{r.start.slice(0, 10)} → {r.end.slice(0, 10)} · {r.count}</div>
        </div>
      ))}
    </div>
  );
}
