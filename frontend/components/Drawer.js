"use client";
import { useEffect, useState } from "react";
import { api, TYPE_COLORS } from "@/lib/api";

export default function Drawer({ selection, onClose, onSelectItem, onSelectEntity }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    setData(null);
    if (!selection) return;
    const p = selection.kind === "entity" ? api.entity(selection.id) : api.item(selection.id);
    p.then(setData).catch(() => setData({ error: true }));
  }, [selection]);

  if (!selection) return null;
  return (
    <div className="drawer">
      <div className="pad">
        <button className="close" onClick={onClose}>✕</button>
        {!data && <div>Loading…</div>}
        {data?.error && <div>Not found.</div>}
        {data && selection.kind === "entity" && <EntityDetail data={data} onSelectItem={onSelectItem} />}
        {data && selection.kind === "item" && <ItemDetail data={data} onSelectEntity={onSelectEntity} />}
      </div>
    </div>
  );
}

function EntityDetail({ data, onSelectItem }) {
  const e = data.entity;
  return (
    <div>
      <span className="badge" style={{ background: TYPE_COLORS[e.type] || "#666" }}>{e.type}</span>
      <h3 style={{ margin: "8px 0 4px" }}>{e.label}</h3>
      {e.attrs?.emails?.length > 0 && (
        <div className="mono snippet">{e.attrs.emails.join("  ·  ")}</div>
      )}
      {e.aliases?.length > 1 && (
        <>
          <div className="section-h" style={{ marginTop: 12 }}>Resolved surface forms ({e.aliases.length})</div>
          <div>{e.aliases.map((a, i) => <span key={i} className="chip">{a}</span>)}</div>
        </>
      )}
      <div className="section-h" style={{ marginTop: 12 }}>Provenance — {data.provenance.length} mentions</div>
      {data.provenance.map((p, i) => (
        <div key={i} className="row" onClick={() => onSelectItem(p.item_id)}>
          <div className="meta">[{p.source_type}] {p.item_id}</div>
          <div className="snippet">{p.snippet}</div>
        </div>
      ))}
    </div>
  );
}

function ItemDetail({ data, onSelectEntity }) {
  return (
    <div>
      <span className="badge" style={{ background: "#555" }}>{data.source_type}</span>
      {data.relevance && (
        <span className="badge" style={{ background: data.relevance.relevant ? "#2f855a" : "#a12d2d", marginLeft: 6 }}>
          {data.relevance.relevant ? "relevant" : "noise"}
        </span>
      )}
      <h3 style={{ margin: "8px 0 2px" }}>{data.subject || "(no subject)"}</h3>
      <div className="meta mono">{data.timestamp?.replace("T", " ") || ""} · {data.channel}</div>
      <div className="snippet" style={{ marginTop: 4 }}>
        <b>from:</b> {data.sender}<br />{data.recipients && <><b>to:</b> {data.recipients}</>}
      </div>
      {data.relevance && <div className="note" style={{ marginTop: 8 }}>relevance: {data.relevance.rationale}</div>}
      {data.entities?.length > 0 && (
        <>
          <div className="section-h" style={{ marginTop: 12 }}>Entities in this item</div>
          <div>{data.entities.map((e) => (
            <span key={e.id} className="chip" style={{ borderColor: TYPE_COLORS[e.type] }}
              onClick={() => onSelectEntity(e.id)}>{e.label}</span>
          ))}</div>
        </>
      )}
      <div className="section-h" style={{ marginTop: 12 }}>Body (raw, redacted)</div>
      <div className="snippet mono" style={{ maxHeight: 320, overflow: "auto", border: "1px solid var(--border)", padding: 8, background: "#fff" }}>
        {data.body}
      </div>
      {data.notes && (
        <>
          <div className="section-h" style={{ marginTop: 10 }}>Redactor note (eval oracle — not used by pipeline)</div>
          <div className="snippet">{data.notes}</div>
        </>
      )}
    </div>
  );
}
