"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import ApiError from "@/components/ApiError";

export default function CorpusView({ onSelectItem }) {
  const [data, setData] = useState({ items: [], total: 0 });
  const [filters, setFilters] = useState({ source_type: "", relevant: "", q: "" });
  const [error, setError] = useState(null);

  const load = () => {
    const p = { limit: 200 };
    if (filters.source_type) p.source_type = filters.source_type;
    if (filters.relevant !== "") p.relevant = filters.relevant;
    if (filters.q) p.q = filters.q;
    api.items(p).then((d) => { setData(d); setError(null); }).catch((e) => setError(String(e)));
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filters]);

  return (
    <div>
      {error && <div className="pad"><ApiError message={error} onRetry={() => { setError(null); load(); }} /></div>}
      <div className="pad" style={{ display: "flex", gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)" }}>
        <select value={filters.source_type} onChange={(e) => setFilters({ ...filters, source_type: e.target.value })}>
          <option value="">all sources</option>
          <option value="email">email</option>
          <option value="whatsapp">whatsapp</option>
          <option value="pdf">pdf</option>
          <option value="excel_row">excel_row</option>
        </select>
        <select value={filters.relevant} onChange={(e) => setFilters({ ...filters, relevant: e.target.value })}>
          <option value="">all items</option>
          <option value="true">event-relevant</option>
          <option value="false">noise (scoped out)</option>
        </select>
        <input placeholder="search text…" value={filters.q}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })} style={{ flex: 1 }} />
        <span className="meta mono">{data.total} items</span>
      </div>
      {data.items.map((it) => (
        <div key={it.id} className={"row" + (it.relevant ? "" : " noise")} onClick={() => onSelectItem(it.id)}>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span><span className="badge" style={{ background: "#555" }}>{it.source_type}</span>{" "}
              <strong>{it.sender?.slice(0, 40) || "—"}</strong></span>
            <span className="meta">{it.timestamp?.slice(0, 16).replace("T", " ") || ""}</span>
          </div>
          <div className="snippet">{it.subject ? <b>{it.subject} · </b> : null}{it.preview}</div>
          <div className="meta">{it.relevant ? "✓ relevant" : "✕ noise"} — {it.relevance_rationale}</div>
        </div>
      ))}
    </div>
  );
}
