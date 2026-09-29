"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import ApiError from "@/components/ApiError";

export default function QueriesView({ onSelectItem, onHighlight }) {
  const [queries, setQueries] = useState([]);
  const [active, setActive] = useState(null);
  const [result, setResult] = useState(null);
  const [ask, setAsk] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [models, setModels] = useState(null);

  const [loadError, setLoadError] = useState(null);
  useEffect(() => {
    api.stats().then((s) => setQueries(s.queries || [])).catch((e) => setLoadError(String(e)));
    api.models().then(setModels).catch(() => {});
  }, []);

  const run = async (q) => {
    setActive(q.id);
    const r = await api.query(q.id);
    setResult(r);
    onHighlight && onHighlight(r.subgraph || []);
  };

  const runAsk = async () => {
    if (!ask.trim()) return;
    setAsking(true); setAnswer(null);
    try {
      const resp = await api.ask(ask);
      if (resp.error === 'llm_unavailable') {
        setAnswer({ answer: resp.message, citations: [] });
      } else {
        setAnswer(resp);
      }
    }
    catch (e) { setAnswer({ answer: `Error: ${e}`, citations: [] }); }
    finally { setAsking(false); }
  };

  return (
    <div className="pad">
      {loadError && <ApiError message={loadError} onRetry={() => {
        setLoadError(null);
        api.stats().then((s) => setQueries(s.queries || [])).catch((e) => setLoadError(String(e)));
      }} />}
      <CurrencyReview onSelectItem={onSelectItem} />

      <div className="section-h">
        Ask anything {models && <span style={{ textTransform: "none", color: "var(--dim)" }}>· answered by {models.query_model}</span>}
      </div>
      <div style={{ display: "flex", gap: 8, marginBottom: 6 }}>
        <input style={{ flex: 1 }} placeholder="e.g. What did the trip cost and who approved the leave?"
          value={ask} onChange={(e) => setAsk(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runAsk()} />
        <button className="active" disabled={asking} onClick={runAsk}>{asking ? "Thinking…" : "Ask"}</button>
      </div>
      {answer && (
        <div style={{ marginBottom: 16 }}>
          <div className="note" style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</div>
          {answer.citations?.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <span className="meta">sources: </span>
              {answer.citations.map((c) => (
                <span key={c.item_id} className="chip" onClick={() => onSelectItem(c.item_id)}>
                  [{c.source_type}] {c.snippet}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="section-h">Preset cross-source questions</div>
      {queries.map((q) => (
        <button key={q.id} className={"q-btn" + (active === q.id ? " active" : "")} onClick={() => run(q)}>
          {q.q}
        </button>
      ))}

      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="section-h">Answer</div>
          <p><strong>{result.answer}</strong></p>
          {result.caveats?.map((c, i) => (
            <div key={i} className="note">{c}</div>
          ))}
          {result.note && <div className="note">⚖️ {result.note}</div>}

          {result.table && (
            <table className="t">
              <thead><tr>{Object.keys(result.table[0] || {}).map((k) => <th key={k}>{k}</th>)}</tr></thead>
              <tbody>
                {result.table.map((row, i) => (
                  <tr key={i}>{Object.values(row).map((v, j) => (
                    <td key={j} className="mono">{Array.isArray(v) ? v.join(", ") : String(v)}</td>
                  ))}</tr>
                ))}
              </tbody>
            </table>
          )}

          {result.steps && (
            <div style={{ marginTop: 8 }}>
              {result.steps.map((s, i) => (
                <div key={i} className="row" onClick={() => s.item_id && onSelectItem(s.item_id)}>
                  <div className="meta">{s.timestamp?.slice(0, 16).replace("T", " ") || ""} · {s.source_type}</div>
                  <div>{s.text}</div>
                  {s.note && <div className="meta">↳ {s.note}</div>}
                </div>
              ))}
            </div>
          )}

          {result.citations?.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="section-h">Citations — click to open source</div>
              {result.citations.map((c) => (
                <span key={c.item_id} className="chip" onClick={() => onSelectItem(c.item_id)}>
                  [{c.source_type}] {c.snippet}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CurrencyReview({ onSelectItem }) {
  const [data, setData] = useState(null);
  const [picked, setPicked] = useState({});
  const load = () => api.currencyFlags().then(setData).catch(() => { setData({ flagged: [] }); });
  useEffect(() => { load(); }, []);
  if (!data) return null;

  const resolve = async (f) => {
    const cur = picked[f.entity_id];
    if (!cur) return;
    try { await api.resolveCurrency(f.entity_id, cur); load(); }
    catch (e) { /* leave flag in place on error */ }
  };

  return (
    <div style={{ marginBottom: 18 }}>
      <div className="section-h">
        Currency review {data.flagged.length > 0
          ? <span style={{ textTransform: "none", color: "#b45309" }}>· {data.flagged.length} amount(s) need you to confirm the currency</span>
          : <span style={{ textTransform: "none", color: "var(--dim)" }}>· all amounts resolved ✓</span>}
      </div>
      {data.flagged.length > 0 && (
        <div className="note">
          These numbers appeared with <strong>no currency marker</strong> in the source (e.g. bare WhatsApp
          amounts). The system will <strong>not</strong> assume they are dollars — confirm each one; until you do,
          they are excluded from every total.
        </div>
      )}
      {data.flagged.map((f) => (
        <div key={f.entity_id} className="merge" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="mono" style={{ fontWeight: 600, minWidth: 90 }}>{f.amount}</span>
          <span className="snippet" style={{ flex: 1 }}
            onClick={() => f.sources[0] && onSelectItem(f.sources[0].item_id)}>
            <span className="chip">{f.reason}</span> {f.sources[0]?.snippet}
          </span>
          <select value={picked[f.entity_id] || ""}
            onChange={(e) => setPicked({ ...picked, [f.entity_id]: e.target.value })}>
            <option value="">currency…</option>
            {data.currencies.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <button className="active" disabled={!picked[f.entity_id]} onClick={() => resolve(f)}>Resolve</button>
        </div>
      ))}
    </div>
  );
}
